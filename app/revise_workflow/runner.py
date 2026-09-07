"""The Python orchestrator — one wave loop, no judgment branches.

Ground truth: docs/revise-workflow.html. `StagedRunner` is an `AIRunner`
(app/ai_runner.py) selected by `AI_RUNNER=staged`, so `app.revise_executor`
drives it exactly like the single-call runner it replaces and the queue,
CAS, commit and re-notify machinery above it is untouched.

**The flow is controlled here, in Python, not by a model.** The loop is the
one mrdoc already proved: look at the artifacts, run what is runnable,
record it, repeat. `workspace.derive_state` answers "what is done" from file
existence alone, so a round that dies at `gate` resumes at `gate` instead of
paying for the two LLM calls before it. There is no place in this module
where a model decides what runs next; the DAG is a fixed chain and the only
branches are counters (`REINJECT_LIMIT`) and emptiness checks.

Three of the seven nodes spawn a `claude` CLI (`intent` / `edit` / `verify`);
the other four are pure functions in `nodes.py`. Each LLM stage returns the
artifact markdown on stdout and this module parses it with the matching
`schema.parse_*` before writing the canonical re-render to disk — **the parse
is the validation**. A stage that answers with prose fails its node instead of
poisoning the next one, which is mrdoc's "산출물이 있어야 성공" rule applied
one level tighter (existence *and* shape).

Two stages are short-circuited deterministically rather than called:

- every opinion `MISSING` at `anchor` → nothing to edit, so `edit` and
  `verify` are synthesized (all unprocessed / all unapplied) and the round
  ends as a 되물음 with no commit. Two calls saved on the commonest failure.
- an empty `git diff` after `edit` → `verify` is synthesized (all unapplied).
  Judging "nothing changed" needs no model.

Re-injection (구현 누락 → 3b, 스펙 모호 → 3a) is budgeted at
`REINJECT_LIMIT` per round and counted **from the ledger**, not memory, so a
resumed round cannot silently double its budget. Cause classification is
deterministic: an opinion's first failure re-injects `edit`, its second
re-injects `intent`; there is no model asked to classify it.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from app import git_workspace
from app.ai_runner import _CREDENTIAL_ENV_KEYS, ReviseResult
from app.config import Settings, get_settings
from app.mrdoc.literals import extract_literals
from app.mrdoc.markdown_tree import parse_sections

from . import nodes, prompts, schema
from .workspace import (
    append_ledger,
    artifact_paths,
    derive_state,
    runnable_nodes,
    work_dir,
)

logger = logging.getLogger("uvicorn.error")

MD_SUFFIXES = (".md", ".mdx")

#: 라운드당 서브에이전트 재투입 총량 (3a·3b 합산). 사람 라운드는 소모하지 않는다 —
#: docs/mr-review-pipeline.html §S4② 수렴 규칙을 그대로 유지한다.
REINJECT_LIMIT = 2

#: Ledger marker the re-injection budget is counted from. Counting the ledger
#: rather than an in-memory tally is what makes the budget survive a resume.
_REINJECT_MARK = "reinject"

_FENCE = re.compile(r"^```[a-zA-Z]*\n|\n?```$")


class StageError(RuntimeError):
    """One node failed — the round is `kind=failed` and the executor retries."""


# ---------------------------------------------------------------------------
# Round context
# ---------------------------------------------------------------------------
@dataclass
class _Round:
    settings: Settings
    workspace: Path
    directory: Path
    opinions: list[dict[str, Any]]
    mr_iid: int
    project_id: str
    repo_slug: str
    round_number: int
    base_sha: str

    @property
    def paths(self) -> dict[str, Path]:
        return artifact_paths(self.directory)

    @property
    def base_dir(self) -> Path:
        """Pre-edit md tree, materialized so `gate` survives a resume.

        `build_gate` needs the documents as they were before 3b touched them.
        Keeping that in memory would be lost on the resume this whole design
        exists to support, so the tree is written next to the artifacts (mrdoc
        materializes `base/` for the same reason).
        """

        return self.directory / "base"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
class StagedRunner:
    """`AIRunner` for `AI_RUNNER=staged` — drives the fixed 7-node DAG.

    Every failure mode is returned as `ReviseResult(kind="failed", ...)`,
    never raised: `app.revise_executor` folds that into its `revise_attempts`
    rule exactly as it does for the single-call runner.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings

    def run(
        self,
        workspace: Path,
        opinions: list[dict[str, Any]],
        session_ctx: dict[str, Any],
        timeout_seconds: int,
    ) -> ReviseResult:
        settings = self._settings or get_settings()
        del timeout_seconds  # the executor enforces the wall clock; stages have their own

        try:
            round_ = _prepare_round(settings, workspace, opinions, session_ctx)
        except Exception as error:  # noqa: BLE001 — never raise into the worker thread
            return _failed(settings, f"round setup failed: {type(error).__name__}: {error}")

        try:
            _drive(round_)
        except StageError as error:
            append_ledger(round_.directory, [f"abort: {error}"])
            return _failed(settings, str(error))
        except Exception as error:  # noqa: BLE001
            logger.exception("Staged revise: unhandled error (mr=%s)", round_.mr_iid)
            append_ledger(round_.directory, [f"abort: {type(error).__name__}"])
            return _failed(settings, f"{type(error).__name__}: {error}")

        try:
            return _assemble(round_)
        except Exception as error:  # noqa: BLE001
            return _failed(settings, f"result assembly failed: {type(error).__name__}: {error}")


# ---------------------------------------------------------------------------
# The loop — this is the whole of the orchestration
# ---------------------------------------------------------------------------
def _drive(round_: _Round) -> None:
    """Run runnable nodes until the DAG completes; abort on no progress.

    No node decides what comes next. `runnable_nodes` reads file existence,
    the first runnable node runs, and the loop looks again. A node that
    claims success without writing its artifact leaves the state unchanged
    and trips the no-progress abort rather than crashing a later node on a
    missing dependency (mrdoc's contract, same wording).
    """

    handlers = {
        "intent": _node_intent,
        "anchor": _node_anchor,
        "impact": _node_impact,
        "edit": _node_edit,
        "gate": _node_gate,
        "verify": _node_verify,
        "summary": _node_summary,
    }

    while True:
        before = derive_state(round_.directory)
        runnable = runnable_nodes(before)
        if not runnable:
            append_ledger(round_.directory, ["pipeline complete"])
            return

        node = runnable[0]
        handlers[node](round_)

        after = derive_state(round_.directory)
        if after == before:
            raise StageError(f"no progress after node {node}")


# ---------------------------------------------------------------------------
# Nodes — LLM
# ---------------------------------------------------------------------------
def _parse_or_fail(stage: str, parse: Callable[[str], Any], body: str) -> Any:
    """Parse a stage's artifact; a codec refusal is that stage's failure.

    Without this the codec's bare `ValueError` reached `run`'s catch-all,
    which logged it as an unhandled error and wrote only `abort: ValueError`
    to the ledger — no stage, no message. Both measured rounds that died in
    the codec had to be triaged from the driver's traceback instead of from
    the artifact trail, which is the one thing the trail exists to prevent.
    """

    try:
        return parse(body)
    except ValueError as error:
        raise StageError(f"stage {stage} returned an unparseable artifact: {error}") from error


def _stamp_identity[ArtifactT: (schema.Intent, schema.EditReceipt, schema.Verify)](
    round_: _Round, artifact: ArtifactT
) -> ArtifactT:
    """Overwrite the artifact's identity fields with what Python already knows.

    The three LLM stages each echo `mr_iid`/`round` (3a also `project_id` and
    `base_sha`) back in their frontmatter, and a stage has no reason to get
    those right: a smoke measurement caught 3b writing `mr_iid: 9999` (the
    *project* id) and `round: 11` (the *mr* iid) while otherwise producing a
    perfectly correct receipt. Nothing in the orchestrator keys off these
    fields -- every join is by `opinion_id` -- but they are stamped into the
    artifact a human reads and into the report, so they are not allowed to be
    the model's guess. Python is the authority; the parse only proves the
    field was present and well-typed.
    """

    fields = {"mr_iid": round_.mr_iid, "round": round_.round_number}
    if hasattr(artifact, "project_id"):
        fields["project_id"] = round_.project_id
    if hasattr(artifact, "base_sha"):
        fields["base_sha"] = round_.base_sha
    return replace(artifact, **fields)


def _node_intent(round_: _Round) -> None:
    tree = _read_md_tree(round_.workspace)
    toc = {
        path: [section.heading_path for section in parse_sections(text)]
        for path, text in tree.items()
    }
    body = _stage(
        round_,
        stage="intent",
        system=prompts.INTENT_SYSTEM,
        prompt=prompts.intent_prompt(
            mr_iid=round_.mr_iid,
            project_id=round_.project_id,
            round_number=round_.round_number,
            base_sha=round_.base_sha,
            repo_slug=round_.repo_slug,
            opinions=round_.opinions,
            changed_files=(),
            toc=toc,
        ),
        tools=(),
        edit=False,
    )
    intent = _stamp_identity(round_, _parse_or_fail("intent", schema.parse_intent, body))
    _write(round_, "intent", schema.render_intent(intent))
    append_ledger(round_.directory, [f"intent done (opinions={len(intent.opinions)})"])


def _node_edit(round_: _Round) -> None:
    intent = schema.parse_intent(_read(round_, "intent"))
    anchor = schema.parse_anchor(_read(round_, "anchor"))
    impact = schema.parse_impact(_read(round_, "impact"))

    # The pre-edit tree is the gate's `base_tree`. Materialize before 3b runs,
    # once per round — a resumed round must not re-snapshot an edited tree.
    _materialize_base(round_)

    if anchor.resolved == 0:
        # Nothing was located, so there is nothing to edit. Synthesize the
        # receipt instead of paying for a call that can only report failure.
        receipt = schema.EditReceipt(
            mr_iid=round_.mr_iid,
            round=round_.round_number,
            processed=(),
            unprocessed=tuple(
                schema.Unprocessed(opinion_id=entry.opinion_id, reason="대상 미확인 — 앵커 0건")
                for entry in anchor.entries
            ),
            files_touched=(),
            entries=(),
            required=_required(
                "OK", uncovered=schema.join_ids(e.opinion_id for e in anchor.entries)
            ),
        )
        _write(round_, "edit", schema.render_edit(receipt))
        append_ledger(round_.directory, ["edit skipped (anchor resolved=0)"])
        return

    allowed = tuple(sorted(set(anchor.files) | set(impact.files)))
    body = _stage(
        round_,
        stage="edit",
        system=prompts.EDIT_SYSTEM,
        prompt=prompts.edit_prompt(
            intent=intent,
            anchor=anchor,
            impact=impact,
            windows=_windows(round_, anchor),
            allowed_files=allowed,
            workspace_posix=round_.workspace.as_posix(),
            retry_note=_retry_note(round_),
        ),
        tools=("Read", "Edit", "Write"),
        edit=True,
    )
    receipt = _stamp_identity(round_, _parse_or_fail("edit", schema.parse_edit, body))
    _write(round_, "edit", schema.render_edit(receipt))
    append_ledger(
        round_.directory,
        [f"edit done (processed={len(receipt.processed)} touched={len(receipt.files_touched)})"],
    )


def _node_verify(round_: _Round) -> None:
    intent = schema.parse_intent(_read(round_, "intent"))
    diff_text = _diff(round_)

    if not diff_text.strip():
        verify = schema.Verify(
            mr_iid=round_.mr_iid,
            round=round_.round_number,
            verdicts=tuple(
                schema.Verdict(
                    opinion_id=int(opinion["id"]),
                    verdict="unapplied",
                    evidence=(),
                    판정문="변경분 없음 — 대조할 diff 가 없다",
                )
                for opinion in round_.opinions
            ),
            summary="이 라운드는 문서를 바꾸지 않았다",
            required=_required("OK"),
        )
        _write(round_, "verify", schema.render_verify(verify))
        append_ledger(round_.directory, ["verify skipped (empty diff)"])
        return

    body = _stage(
        round_,
        stage="verify",
        system=prompts.VERIFY_SYSTEM,
        prompt=prompts.verify_prompt(
            intent=intent,
            opinions=round_.opinions,
            diff_text=diff_text,
        ),
        tools=(),
        edit=False,
    )
    verify = _stamp_identity(round_, _parse_or_fail("verify", schema.parse_verify, body))
    _write(round_, "verify", schema.render_verify(verify))
    counts = verify.counts()
    append_ledger(
        round_.directory,
        [
            f"verify done (applied={counts['applied']} "
            f"partial={counts['partial']} unapplied={counts['unapplied']})"
        ],
    )


# ---------------------------------------------------------------------------
# Nodes — deterministic
# ---------------------------------------------------------------------------
def _node_anchor(round_: _Round) -> None:
    intent = schema.parse_intent(_read(round_, "intent"))
    anchor = nodes.build_anchor(intent, _read_md_tree(round_.workspace))
    _write(round_, "anchor", schema.render_anchor(anchor))
    append_ledger(
        round_.directory, [f"anchor done (found={anchor.resolved} missing={anchor.missing})"]
    )


def _node_impact(round_: _Round) -> None:
    anchor = schema.parse_anchor(_read(round_, "anchor"))
    impact = nodes.build_impact(anchor, _read_md_tree(round_.workspace))
    _write(round_, "impact", schema.render_impact(impact))
    append_ledger(round_.directory, [f"impact done (candidates={impact.candidate_count})"])


def _node_gate(round_: _Round) -> None:
    """Build the gate, *act on it*, and re-inject when it found 구현 누락.

    The gate node is the only deterministic node with a side effect: reverting
    is the design's point — "프롬프트로 금지하는 것과 기계가 되돌리는 것은
    다르다". `build_gate` decides nothing about acting; it hands back the
    complete revert set and this function performs it.
    """

    settings = round_.settings
    intent = schema.parse_intent(_read(round_, "intent"))
    anchor = schema.parse_anchor(_read(round_, "anchor"))
    impact = schema.parse_impact(_read(round_, "impact"))

    changed = git_workspace.changed_paths(settings, round_.workspace)
    untracked = git_workspace.untracked_paths(settings, round_.workspace)
    gate = nodes.build_gate(
        intent,
        anchor,
        impact,
        changed,
        _read_md_tree(round_.base_dir),
        _read_md_tree(round_.workspace),
        max_changed_files=settings.revise_max_changed_files,
        max_changed_lines=settings.revise_max_changed_lines,
        changed_lines=_changed_lines(round_),
        untracked=untracked,
    )

    if gate.reverted:
        git_workspace.restore_paths(settings, round_.workspace, list(gate.reverted))

    _write(round_, "gate", schema.render_gate(gate))
    append_ledger(
        round_.directory,
        [
            f"gate done (kind={gate.kind} reverted={len(gate.reverted)} "
            f"literal_fail={len(gate.failed_opinion_ids)})"
        ],
    )

    if gate.failed_opinion_ids:
        _reinject(round_, gate.failed_opinion_ids, cause="게이트 리터럴 검증 실패")


def _node_summary(round_: _Round) -> None:
    """Narrow the commit to 3c's verdict, revert the rest, then fill the slots.

    `build_summary` derives `commit_paths` from the gate alone, which would
    commit an edit 3c judged `unapplied` as long as it landed in an allowed
    file. The design's answer to a partial round is 「성공분만 커밋, 실패분은
    사유로 남김」, so the attribution happens here: the 30-edit.md receipt says
    which opinion touched which file, and a file survives only if some opinion
    that touched it came back `applied` or `partial`.

    A file both a passing and a failing opinion edited is kept whole — file is
    the finest granularity git offers here, and that limit is recorded as open
    question 2 in docs/revise-workflow.html.
    """

    verify = schema.parse_verify(_read(round_, "verify"))
    gate = schema.parse_gate(_read(round_, "gate"))
    receipt = schema.parse_edit(_read(round_, "edit"))
    anchor = schema.parse_anchor(_read(round_, "anchor"))

    # Only an opinion whose target was actually located can be a 구현 누락.
    # One that never anchored is a 되물음 — re-injecting it would spend the
    # round's budget re-asking a model to edit something nobody can point at.
    anchored = {entry.opinion_id for entry in anchor.entries if entry.status == "FOUND"}
    open_ids = tuple(
        row.opinion_id
        for row in verify.verdicts
        if row.verdict != "applied" and row.opinion_id in anchored
    )
    if open_ids and _reinject(round_, open_ids, cause="3c 미반영 판정"):
        return

    kept_ids = {row.opinion_id for row in verify.verdicts if row.verdict in ("applied", "partial")}
    kept_files = {
        op.file
        for entry in receipt.entries
        if entry.opinion_id in kept_ids
        for op in entry.edits
    }
    live = git_workspace.changed_paths(round_.settings, round_.workspace)
    dropped = sorted(path for path in live if path not in kept_files)
    if dropped:
        git_workspace.restore_paths(round_.settings, round_.workspace, dropped)
        append_ledger(
            round_.directory, [f"revert (미반영 판정) {len(dropped)}건: {', '.join(dropped)}"]
        )

    before = _read_md_tree(round_.base_dir)
    after = _read_md_tree(round_.workspace)
    summary = nodes.build_summary(
        verify,
        gate,
        {path: extract_literals(text) for path, text in before.items()},
        {path: extract_literals(text) for path, text in after.items()},
    )
    committable = tuple(sorted(set(live) & kept_files))
    watch = list(summary.points_to_watch)
    if dropped:
        watch.append(f"3c 미반영 판정으로 되돌린 편집: {', '.join(dropped)}")
    summary = replace(summary, commit_paths=committable, points_to_watch=tuple(watch))

    _write(round_, "summary", schema.render_summary(summary))
    append_ledger(round_.directory, [f"summary done (commit_paths={len(summary.commit_paths)})"])


# ---------------------------------------------------------------------------
# Re-injection — deterministic cause classification, ledger-counted budget
# ---------------------------------------------------------------------------
def _reinject(round_: _Round, opinion_ids: Sequence[int], *, cause: str) -> bool:
    """Roll the DAG back to `edit` (or `intent`) for another attempt.

    Returns True when a re-injection was actually started, False when the
    budget is spent and the round must go forward with an honest 표기 —
    "모든 경로가 성공 또는 표기 후 진행으로 끝나 무한 루프가 없다".

    Classification carries no judgment: an opinion failing for the first time
    is 구현 누락 (re-inject `edit`); failing again is 스펙 모호 (re-inject
    `intent`, which re-runs everything downstream with a fresh spec).
    """

    spent = _reinject_count(round_)
    if spent >= REINJECT_LIMIT:
        append_ledger(round_.directory, [f"{_REINJECT_MARK} budget spent — 표기 후 진행 ({cause})"])
        return False

    target = "intent" if spent >= 1 else "edit"
    ids = schema.join_ids(opinion_ids)
    append_ledger(
        round_.directory,
        [f"{_REINJECT_MARK} {spent + 1}/{REINJECT_LIMIT} -> {target} ({cause}; opinions {ids})"],
    )

    # Undo the round's edits before re-running: 3b must start from the same
    # base every attempt, or attempt N+1 sees attempt N's half-done work.
    changed = git_workspace.changed_paths(round_.settings, round_.workspace)
    if changed:
        git_workspace.restore_paths(round_.settings, round_.workspace, changed)

    _rollback(round_, target)
    return True


def _rollback(round_: _Round, node: str) -> None:
    """Delete `node`'s artifact and everything downstream of it in the chain."""

    from .workspace import NODES

    paths = round_.paths
    for name in NODES[NODES.index(node) :]:
        paths[name].unlink(missing_ok=True)


def _reinject_count(round_: _Round) -> int:
    ledger = round_.paths["ledger"]
    if not ledger.exists():
        return 0
    text = ledger.read_text(encoding="utf-8")
    return sum(
        1
        for line in text.splitlines()
        if line.startswith(f"{_REINJECT_MARK} ") and "budget spent" not in line
    )


def _retry_note(round_: _Round) -> str:
    """The evidence a re-injected 3b is answering — the gate's, never 3b's own."""

    gate_path = round_.paths["gate"]
    if gate_path.exists():
        return ""
    ledger = round_.paths["ledger"]
    if not ledger.exists():
        return ""
    marks = [
        line
        for line in ledger.read_text(encoding="utf-8").splitlines()
        if line.startswith(f"{_REINJECT_MARK} ") and "budget spent" not in line
    ]
    return marks[-1] if marks else ""


# ---------------------------------------------------------------------------
# Result assembly
# ---------------------------------------------------------------------------
def _assemble(round_: _Round) -> ReviseResult:
    anchor = schema.parse_anchor(_read(round_, "anchor"))
    verify = schema.parse_verify(_read(round_, "verify"))
    summary = schema.parse_summary(_read(round_, "summary"))
    secrets = git_workspace._redaction_secrets(round_.settings)

    verdicts = {row.opinion_id: row for row in verify.verdicts}
    unapplied: list[dict[str, Any]] = []
    for opinion in round_.opinions:
        opinion_id = int(opinion["id"])
        row = verdicts.get(opinion_id)
        if row is not None and row.verdict == "applied":
            continue
        reason = row.판정문 if row is not None else "러너 응답에 누락"
        unapplied.append(
            {
                "opinion_id": opinion_id,
                "reason": git_workspace._redact(reason or "(사유 없음)", secrets),
            }
        )

    clarify: list[dict[str, Any]] = []
    for entry in anchor.entries:
        if entry.status == "FOUND":
            continue
        clarify.append(
            {
                "opinion_id": entry.opinion_id,
                "question": "문서에서 대상을 찾지 못했습니다. 혹시 이 중 하나인가요?",
                "candidates": [
                    f"{candidate.file} § {candidate.heading}" for candidate in entry.candidates
                ],
            }
        )

    return ReviseResult(
        kind="ok",
        unapplied=unapplied,
        detail=git_workspace._redact(summary.summary, secrets),
        commit_paths=list(summary.commit_paths),
        clarify=clarify,
    )


def _failed(settings: Settings, detail: str) -> ReviseResult:
    masked = git_workspace._redact(detail, git_workspace._redaction_secrets(settings))
    return ReviseResult(kind="failed", detail=masked[:1000])


# ---------------------------------------------------------------------------
# Stage invocation — one headless `claude` per LLM node
# ---------------------------------------------------------------------------
def _stage(
    round_: _Round,
    *,
    stage: str,
    system: str,
    prompt: str,
    tools: tuple[str, ...],
    edit: bool,
) -> str:
    """Run one CLI stage and return its stdout, fences stripped.

    Two flags here are load-bearing and both were established by measurement,
    not preference (see `.orchestration/reports/smoke-staged-runner.md`):

    `--setting-sources ""` isolates the run from the operator's own Claude
    configuration. Without it the machine's user/plugin settings load into
    every stage: the first smoke run died with `Exceeded USD budget` on a
    *trivial* prompt because ~20 plugin agents and skills were being pulled
    into the system prompt, and a plugin `SessionEnd` hook was failing on top
    of that. With it, the same prompt finishes inside the smallest budget and
    stderr is empty. A production rail must not depend on whose laptop it runs
    on.

    `--tools` is an explicit whitelist on every stage, `edit` included.
    `--permission-mode acceptEdits` alone would leave 3b holding Grep, Glob,
    Bash and Task — it could search (the design forbids it: an invented search
    term has nothing to compare against) and could even spawn sub-agents.
    3a gets nothing, 3b gets `Read Edit Write`, and 3c gets nothing either:
    measured against identical input it cost $1.2174 over 2 turns holding
    `Read` and $0.0287 over 1 turn without it, for the same verdict, and its
    own hard limits already say the diff is the whole of its evidence.
    """

    settings = round_.settings
    claude_bin = settings.claude_bin or shutil.which("claude")
    if not claude_bin:
        raise StageError("`claude` CLI not found on PATH")

    cmd = [
        claude_bin,
        "-p",
        "--setting-sources",
        "",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--model",
        settings.ai_model,
        "--effort",
        settings.ai_effort,
        "--max-budget-usd",
        str(settings.revise_stage_budget_usd),
        "--system-prompt",
        system,
    ]
    if edit:
        cmd += ["--permission-mode", "acceptEdits"]
    cmd += ["--tools", *(tools or ("",))]

    env = {k: v for k, v in _process_env().items() if k.upper() not in _CREDENTIAL_ENV_KEYS}
    timeout = settings.revise_stage_timeout_seconds
    secrets = git_workspace._redaction_secrets(settings)

    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=round_.workspace,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise StageError(f"stage {stage} timed out after {timeout}s") from error
    except OSError as error:
        raise StageError(
            git_workspace._redact(f"stage {stage} failed to start: {type(error).__name__}", secrets)
        ) from error

    if proc.returncode != 0:
        detail = git_workspace._redact(proc.stderr or proc.stdout or "no error output", secrets)
        raise StageError(f"stage {stage} exited {proc.returncode}: {detail[:600]}")

    body = _strip_fence(proc.stdout)
    if not body.strip():
        raise StageError(f"stage {stage} returned nothing")
    return body


def _process_env() -> dict[str, str]:
    """A copy of the current environment — split out so tests can patch it."""

    return dict(os.environ)


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = _FENCE.sub("", stripped).strip()
    return stripped


# ---------------------------------------------------------------------------
# Workspace helpers
# ---------------------------------------------------------------------------
def _prepare_round(
    settings: Settings,
    workspace: Path,
    opinions: list[dict[str, Any]],
    session_ctx: Mapping[str, Any],
) -> _Round:
    directory = work_dir(
        git_workspace._workspace_root(settings),
        session_ctx.get("project_id", "0"),
        session_ctx.get("mr_iid", 0),
        int(session_ctx.get("round", 0) or 0),
    )
    directory.mkdir(parents=True, exist_ok=True)
    return _Round(
        settings=settings,
        workspace=workspace,
        directory=directory,
        opinions=list(opinions),
        mr_iid=int(session_ctx.get("mr_iid", 0) or 0),
        project_id=str(session_ctx.get("project_id", "")),
        repo_slug=str(session_ctx.get("repo_slug") or ""),
        round_number=int(session_ctx.get("round", 0) or 0),
        base_sha=git_workspace.current_sha(settings, workspace),
    )


def _read_md_tree(root: Path) -> dict[str, str]:
    """`{posix path: text}` for every md/mdx under `root`, `.git` excluded.

    Deliberately local rather than `app.mrdoc.workspace.read_md_tree`: that
    module is under active change for mrdoc v2 and this is six lines.
    """

    tree: dict[str, str] = {}
    if not root.exists():
        return tree
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in MD_SUFFIXES:
            continue
        if ".git" in path.relative_to(root).parts:
            continue
        try:
            tree[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return tree


def _materialize_base(round_: _Round) -> None:
    """Snapshot the pre-edit md tree once per round (idempotent)."""

    base = round_.base_dir
    if base.exists():
        return
    for rel, text in _read_md_tree(round_.workspace).items():
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def _windows(round_: _Round, anchor: schema.Anchor) -> dict[str, str]:
    """The ±20-line slices 3b is allowed to read, keyed `path:start-end`."""

    tree = _read_md_tree(round_.workspace)
    windows: dict[str, str] = {}
    for entry in anchor.entries:
        window = entry.window
        if entry.status != "FOUND" or window is None:
            continue
        text = tree.get(window.file)
        if text is None:
            continue
        lines = text.splitlines()
        slice_ = lines[max(window.start - 1, 0) : window.end]
        numbered = "\n".join(
            f"{number:>5} | {line}"
            for number, line in enumerate(slice_, start=max(window.start, 1))
        )
        windows[f"{window.file}:{window.start}-{window.end}"] = numbered
    return windows


def _diff(round_: _Round) -> str:
    proc = git_workspace._git(["diff", "HEAD"], cwd=round_.workspace, settings=round_.settings)
    return proc.stdout


def _changed_lines(round_: _Round) -> int:
    """Added + deleted lines in the working tree — the gate's size input."""

    proc = git_workspace._git(
        ["diff", "--numstat", "HEAD"], cwd=round_.workspace, settings=round_.settings
    )
    total = 0
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        for value in parts[:2]:
            if value.isdigit():
                total += int(value)
    return total


def _required(status: str, *, uncovered: str = "none") -> schema.Required:
    """The four mandatory fields for a machine-written artifact."""

    return schema.Required(
        STATUS=status,
        UNCOVERED=uncovered or "none",
        UNCERTAIN="none",
        CONFIDENCE="high — 결정론 산출",
    )


def _read(round_: _Round, key: str) -> str:
    return round_.paths[key].read_text(encoding="utf-8")


def _write(round_: _Round, key: str, text: str) -> None:
    path = round_.paths[key]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
