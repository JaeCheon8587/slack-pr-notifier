"""Drive `StagedRunner`'s wave loop with a scripted CLI and a faked git.

The `claude` subprocess and every `app.git_workspace` call are replaced; the
loop, the node handlers, the re-injection budget and the result assembly are
the real ones. The fake stage writer builds its stdout with the *production*
renderers (`schema.render_intent` / `render_edit` / `render_verify`) exactly
as `tests/test_mrdoc_pipeline.py` does, so a template drift would fail these
tests rather than hide behind hand-written strings.

Asserted here: a full round completes and yields `commit_paths`/`unapplied`;
a stage answering prose instead of the template fails the round rather than
poisoning the next node; artifacts already on disk are not recomputed
(resume); an all-MISSING anchor short-circuits both remaining LLM calls and
comes back as 되물음; a gate literal failure re-injects `edit` within budget;
and an opinion 3c judged `unapplied` gets its edit reverted instead of
committed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from app.config import Settings
from app.revise_workflow import runner, schema

# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------
DOC = "docs/install.md"
DOC_TEXT = "# 설치\n\n## 2. 설치 절차\n\n대상 버전은 3.2 이다.\n다음 명령을 실행한다.\n"

SESSION = {
    "session_id": 1,
    "project_id": "1009",
    "mr_iid": 11,
    "round": 1,
    "repo_slug": "product-common",
}
OPINIONS = [
    {"id": 41, "body": "설치 절차의 3.2 버전 표기를 4.0으로 고쳐 주세요.", "question_refs": None}
]


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        workspace_root=str(tmp_path / "ws"),
        claude_bin="claude",
        ai_model="m",
        ai_effort="high",
        revise_stage_budget_usd=0.5,
        revise_stage_timeout_seconds=60,
        revise_max_changed_files=10,
        revise_max_changed_lines=400,
    )


def _required() -> schema.Required:
    return schema.Required(
        STATUS="OK", UNCOVERED="none", UNCERTAIN="none", CONFIDENCE="high — 테스트"
    )


def _intent() -> schema.Intent:
    return schema.Intent(
        mr_iid=11,
        project_id="1009",
        round=1,
        base_sha="abc1234",
        opinions=(
            schema.Opinion(
                opinion_id=41,
                대상문서=(DOC,),
                범위="local",
                대상주장="대상 버전은 3.2 이다",
                수정방향="3.2 → 4.0",
                근거="사람 의견 원문",
                통합=(),
                충돌해소="",
                search_terms=("대상 버전은 3.2 이다",),
            ),
        ),
        required=_required(),
    )


def _receipt(*, files: tuple[str, ...] = (DOC,)) -> schema.EditReceipt:
    return schema.EditReceipt(
        mr_iid=11,
        round=1,
        processed=(41,),
        unprocessed=(),
        files_touched=files,
        entries=(
            schema.EditEntry(
                opinion_id=41,
                result="applied",
                edits=(schema.EditOp(file=DOC, line=5, from_value="3.2", to_value="4.0"),),
            ),
        ),
        required=_required(),
    )


def _verify(verdict: str = "applied") -> schema.Verify:
    return schema.Verify(
        mr_iid=11,
        round=1,
        verdicts=(
            schema.Verdict(
                opinion_id=41,
                verdict=verdict,
                evidence=(schema.Evidence(file=DOC, line=5, before="3.2", after="4.0"),),
                판정문="설치 절차의 버전 표기가 4.0 으로 바뀌었다",
            ),
        ),
        summary="의견 1건 반영",
        required=_required(),
    )


class _Proc:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def _stage_of(cmd: list[str]) -> str:
    """Which stage a command line belongs to, read from its system prompt."""

    system = cmd[cmd.index("--system-prompt") + 1]
    if "revise-intent" in system:
        return "intent"
    if "revise-edit" in system:
        return "edit"
    return "verify"


def _script(
    monkeypatch, workspace: Path, *, replies: dict[str, str], calls: list[str], edited=True
):
    """Replace the CLI: record the stage, optionally edit the doc, reply."""

    def fake_run(cmd, **kwargs):
        stage = _stage_of(cmd)
        calls.append(stage)
        if stage == "edit" and edited:
            (workspace / DOC).write_text(DOC_TEXT.replace("3.2", "4.0"), encoding="utf-8")
        return _Proc(replies[stage])

    monkeypatch.setattr(runner.subprocess, "run", fake_run)


_DIFF = "--- a/docs/install.md\n+++ b/docs/install.md\n-3.2\n+4.0\n"


def _fake_git(
    monkeypatch,
    workspace: Path,
    tmp_path: Path,
    *,
    changed: list[str] | None = None,
    diff: str = _DIFF,
):
    """Stub every git call the runner makes; track reverts for assertions."""

    reverted: list[list[str]] = []
    state = {"changed": changed if changed is not None else [DOC]}

    monkeypatch.setattr(runner.git_workspace, "_workspace_root", lambda settings: tmp_path / "ws")
    monkeypatch.setattr(runner.git_workspace, "current_sha", lambda s, w: "abc1234")
    monkeypatch.setattr(runner.git_workspace, "changed_paths", lambda s, w: list(state["changed"]))
    monkeypatch.setattr(runner.git_workspace, "untracked_paths", lambda s, w: [])

    def fake_restore(settings, ws, paths):
        reverted.append(list(paths))
        for path in paths:
            state["changed"] = [p for p in state["changed"] if p != path]

    monkeypatch.setattr(runner.git_workspace, "restore_paths", fake_restore)
    monkeypatch.setattr(runner, "_diff", lambda r: diff)
    monkeypatch.setattr(runner, "_changed_lines", lambda r: 2 if diff else 0)
    return reverted


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws" / "1009" / "11"
    (workspace / "docs").mkdir(parents=True)
    (workspace / DOC).write_text(DOC_TEXT, encoding="utf-8")
    return workspace


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------
def test_full_round_completes_and_commits_only_the_edited_file(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path)
    reverted = _fake_git(monkeypatch, workspace, tmp_path)
    calls: list[str] = []
    _script(
        monkeypatch,
        workspace,
        calls=calls,
        replies={
            "intent": schema.render_intent(_intent()),
            "edit": schema.render_edit(_receipt()),
            "verify": schema.render_verify(_verify()),
        },
    )

    result = runner.StagedRunner(_settings(tmp_path)).run(workspace, OPINIONS, SESSION, 900)

    assert result.kind == "ok"
    assert calls == ["intent", "edit", "verify"]
    assert result.commit_paths == [DOC]
    assert result.unapplied == []
    assert result.clarify == []
    assert reverted == []

    directory = tmp_path / "ws" / ".revise" / "1009" / "11" / "r1"
    for name in (
        "00-intent.md",
        "10-anchor.md",
        "20-impact.md",
        "30-edit.md",
        "35-gate.md",
        "40-verify.md",
        "50-summary.md",
    ):
        assert (directory / name).is_file(), name
    assert "pipeline complete" in (directory / "ledger.md").read_text(encoding="utf-8")


def test_artifacts_live_outside_the_git_clone(monkeypatch, tmp_path):
    """`git add -A` would sweep an artifact written inside the workspace."""

    workspace = _workspace(tmp_path)
    _fake_git(monkeypatch, workspace, tmp_path)
    calls: list[str] = []
    _script(
        monkeypatch,
        workspace,
        calls=calls,
        replies={
            "intent": schema.render_intent(_intent()),
            "edit": schema.render_edit(_receipt()),
            "verify": schema.render_verify(_verify()),
        },
    )

    runner.StagedRunner(_settings(tmp_path)).run(workspace, OPINIONS, SESSION, 900)

    assert not list(workspace.rglob("00-intent.md"))
    assert (tmp_path / "ws" / ".revise" / "1009" / "11" / "r1" / "00-intent.md").is_file()


def test_a_stage_answering_prose_fails_the_round(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path)
    _fake_git(monkeypatch, workspace, tmp_path)
    calls: list[str] = []
    _script(
        monkeypatch,
        workspace,
        calls=calls,
        replies={"intent": "네, 의견을 잘 정리했습니다!", "edit": "", "verify": ""},
    )

    result = runner.StagedRunner(_settings(tmp_path)).run(workspace, OPINIONS, SESSION, 900)

    assert result.kind == "failed"
    assert calls == ["intent"]


def test_nonzero_exit_fails_the_round(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path)
    _fake_git(monkeypatch, workspace, tmp_path)
    monkeypatch.setattr(runner.subprocess, "run", lambda cmd, **kw: _Proc("boom", returncode=2))

    result = runner.StagedRunner(_settings(tmp_path)).run(workspace, OPINIONS, SESSION, 900)

    assert result.kind == "failed"
    assert "exited 2" in result.detail


def test_timeout_fails_the_round(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path)
    _fake_git(monkeypatch, workspace, tmp_path)

    def boom(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 60)

    monkeypatch.setattr(runner.subprocess, "run", boom)
    result = runner.StagedRunner(_settings(tmp_path)).run(workspace, OPINIONS, SESSION, 900)

    assert result.kind == "failed"
    assert "timed out" in result.detail


def test_missing_anchor_short_circuits_both_calls_and_asks_back(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path)
    _fake_git(monkeypatch, workspace, tmp_path, changed=[], diff="")
    unfindable = schema.Intent(
        mr_iid=11,
        project_id="1009",
        round=1,
        base_sha="abc1234",
        opinions=(
            schema.Opinion(
                opinion_id=41,
                대상문서=(DOC,),
                범위="local",
                대상주장="존재하지 않는 절",
                수정방향="A → B",
                근거="",
                통합=(),
                충돌해소="",
                search_terms=("이 문장은 문서에 없다",),
            ),
        ),
        required=_required(),
    )
    calls: list[str] = []
    _script(
        monkeypatch,
        workspace,
        calls=calls,
        replies={"intent": schema.render_intent(unfindable), "edit": "", "verify": ""},
        edited=False,
    )

    result = runner.StagedRunner(_settings(tmp_path)).run(workspace, OPINIONS, SESSION, 900)

    assert result.kind == "ok"
    assert calls == ["intent"], "edit and verify must be synthesized, not called"
    assert result.commit_paths == []
    assert [row["opinion_id"] for row in result.clarify] == [41]
    assert result.clarify[0]["candidates"]
    assert [row["opinion_id"] for row in result.unapplied] == [41]


def test_completed_nodes_are_not_recomputed_on_resume(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path)
    _fake_git(monkeypatch, workspace, tmp_path)
    directory = tmp_path / "ws" / ".revise" / "1009" / "11" / "r1"
    directory.mkdir(parents=True)
    (directory / "00-intent.md").write_text(schema.render_intent(_intent()), encoding="utf-8")

    calls: list[str] = []
    _script(
        monkeypatch,
        workspace,
        calls=calls,
        replies={
            "intent": "prose that would fail if it were called",
            "edit": schema.render_edit(_receipt()),
            "verify": schema.render_verify(_verify()),
        },
    )

    result = runner.StagedRunner(_settings(tmp_path)).run(workspace, OPINIONS, SESSION, 900)

    assert result.kind == "ok"
    assert calls == ["edit", "verify"], "intent was already on disk"


def test_unapplied_verdict_reverts_the_edit_instead_of_committing_it(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path)
    reverted = _fake_git(monkeypatch, workspace, tmp_path)
    calls: list[str] = []
    _script(
        monkeypatch,
        workspace,
        calls=calls,
        replies={
            "intent": schema.render_intent(_intent()),
            "edit": schema.render_edit(_receipt()),
            "verify": schema.render_verify(_verify("unapplied")),
        },
    )

    result = runner.StagedRunner(_settings(tmp_path)).run(workspace, OPINIONS, SESSION, 900)

    assert result.kind == "ok"
    assert result.commit_paths == [], "an unapplied opinion's edit must not be committed"
    assert [DOC] in reverted
    assert [row["opinion_id"] for row in result.unapplied] == [41]


def test_reinjection_is_budgeted_and_counted_from_the_ledger(monkeypatch, tmp_path):
    """3c 미반영 → edit 재투입 → 두 번째 실패 → intent 재투입 → 예산 소진 후 전진."""

    workspace = _workspace(tmp_path)
    _fake_git(monkeypatch, workspace, tmp_path)
    calls: list[str] = []
    _script(
        monkeypatch,
        workspace,
        calls=calls,
        replies={
            "intent": schema.render_intent(_intent()),
            "edit": schema.render_edit(_receipt()),
            "verify": schema.render_verify(_verify("unapplied")),
        },
    )

    result = runner.StagedRunner(_settings(tmp_path)).run(workspace, OPINIONS, SESSION, 900)

    assert result.kind == "ok", "budget exhaustion goes forward with an honest note, never loops"
    ledger_path = tmp_path / "ws" / ".revise" / "1009" / "11" / "r1" / "ledger.md"
    ledger = ledger_path.read_text(encoding="utf-8")
    marks = [line for line in ledger.splitlines() if line.startswith("reinject ")]
    assert len(marks) == runner.REINJECT_LIMIT + 1, marks
    assert marks[0].endswith("(3c 미반영 판정; opinions 41)") and "-> edit" in marks[0]
    assert "-> intent" in marks[1]
    assert "budget spent" in marks[2]
    assert calls.count("edit") == 3, "one initial call plus two re-injections"


def test_child_env_carries_no_app_secret(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path)
    _fake_git(monkeypatch, workspace, tmp_path)
    monkeypatch.setattr(
        runner,
        "_process_env",
        lambda: {
            "PATH": "/usr/bin",
            "GITLAB_TOKEN": "glpat-secret",
            "ANTHROPIC_API_KEY": "sk-keep",
        },
    )
    seen: dict[str, dict[str, str]] = {}

    def fake_run(cmd, **kwargs):
        seen["env"] = kwargs["env"]
        return _Proc(schema.render_intent(_intent()))

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    runner.StagedRunner(_settings(tmp_path)).run(workspace, OPINIONS, SESSION, 900)

    assert "GITLAB_TOKEN" not in seen["env"]
    assert seen["env"]["ANTHROPIC_API_KEY"] == "sk-keep"


def test_read_only_stages_get_no_edit_permission(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path)
    _fake_git(monkeypatch, workspace, tmp_path)
    commands: dict[str, list[str]] = {}

    def fake_run(cmd, **kwargs):
        stage = _stage_of(cmd)
        commands[stage] = cmd
        if stage == "edit":
            (workspace / DOC).write_text(DOC_TEXT.replace("3.2", "4.0"), encoding="utf-8")
        return _Proc(
            {
                "intent": schema.render_intent(_intent()),
                "edit": schema.render_edit(_receipt()),
                "verify": schema.render_verify(_verify()),
            }[stage]
        )

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    runner.StagedRunner(_settings(tmp_path)).run(workspace, OPINIONS, SESSION, 900)

    assert "--permission-mode" not in commands["intent"]
    assert commands["intent"][commands["intent"].index("--tools") + 1] == ""
    # 3c holds nothing either: its own hard limits say the diff is the whole of
    # its evidence, and measured on identical input the Read grant cost
    # $1.2174 / 2 turns against $0.0287 / 1 turn for the same verdict.
    assert commands["verify"][commands["verify"].index("--tools") + 1] == ""
    assert "acceptEdits" in commands["edit"]
    # The edit stage is whitelisted too: acceptEdits alone would leave it
    # Grep/Glob/Bash/Task, and the design forbids giving any stage a search.
    assert commands["edit"][commands["edit"].index("--tools") + 1 :] == ["Read", "Edit", "Write"]
    for stage, cmd in commands.items():
        assert cmd[cmd.index("--setting-sources") + 1] == "", stage


def test_intent_prompt_shows_headings_but_no_document_body(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path)
    _fake_git(monkeypatch, workspace, tmp_path)
    seen: dict[str, str] = {}

    def fake_run(cmd, **kwargs):
        seen.setdefault(_stage_of(cmd), kwargs["input"])
        return _Proc(schema.render_intent(_intent()))

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    runner.StagedRunner(_settings(tmp_path)).run(workspace, OPINIONS, SESSION, 900)

    prompt = seen["intent"]
    assert "2. 설치 절차" in prompt, "the table of contents is the grounding"
    assert "대상 버전은 3.2 이다" not in prompt, "document bodies must not reach 3a"
    assert OPINIONS[0]["body"] in prompt


def test_verify_prompt_never_carries_the_edit_receipt(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path)
    _fake_git(monkeypatch, workspace, tmp_path)
    seen: dict[str, str] = {}

    def fake_run(cmd, **kwargs):
        stage = _stage_of(cmd)
        seen[stage] = kwargs["input"]
        if stage == "edit":
            (workspace / DOC).write_text(DOC_TEXT.replace("3.2", "4.0"), encoding="utf-8")
        return _Proc(
            {
                "intent": schema.render_intent(_intent()),
                "edit": schema.render_edit(_receipt()),
                "verify": schema.render_verify(_verify()),
            }[stage]
        )

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    runner.StagedRunner(_settings(tmp_path)).run(workspace, OPINIONS, SESSION, 900)

    prompt = seen["verify"]
    assert OPINIONS[0]["body"] in prompt, "the opinion original is the judging axis"
    assert "## RECEIPT" not in prompt and "files_touched" not in prompt
