"""AI runner interface for the P4b revise loop — protocol + stub + P5 runner.

Ground truth: docs/mr-review-pipeline.html §S4② — "단일 claude CLI 세션
(오케스트레이터: 메타프롬프팅 -> 재생성 -> 대조검증 서브에이전트)". The *full*
3a/3b/3c orchestrator chain (meta-prompting -> regeneration -> a separate
대조검증 sub-agent) remains out of scope for this phase per the P5 task
spec — "명령 프롬프트로 클로드코드 실행되고 분석하고 변경점을 정리해라 이정도만".
``ClaudeCliRunner`` below is a single headless ``claude`` CLI invocation with
file-edit tools enabled against the checked-out workspace: it is *not* that
multi-stage orchestrator, only the simplest ``AIRunner`` implementation that
actually touches files. This module defines the ``AIRunner`` protocol that
``app.revise_executor`` depends on, plus:

- ``StubRunner``: makes no file changes and defers every opinion — used to
  exercise the whole rail (queue -> workspace -> runner -> commit/push ->
  re-notify) without any AI chain at all.
- ``ClaudeCliRunner``: the single-call P5 runner described above.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from app.config import Settings, secret_value
from app.git_workspace import _redact

logger = logging.getLogger("uvicorn.error")


@dataclass
class ReviseResult:
    """Outcome of one revise runner invocation.

    ``kind``: ``"ok"`` (the runner completed — even if it made no changes and
    every opinion is unapplied) or ``"failed"`` (the runner itself errored;
    ``app.revise_executor`` folds this into the ``revise_attempts`` rule).
    ``unapplied``: opinions the runner did not resolve this round, each as
    ``{"opinion_id": int, "reason": str}`` — the 3c 대조검증 verdict. Any
    opinion passed to ``run`` that is *not* listed here is treated as applied
    this round.
    """

    kind: str
    unapplied: list[dict[str, Any]] = field(default_factory=list)
    detail: str = ""


class AIRunner(Protocol):
    """Interface the revise executor drives — the P5 orchestrator implements this."""

    def run(
        self,
        workspace: Path,
        opinions: list[dict[str, Any]],
        session_ctx: dict[str, Any],
        timeout_seconds: int,
    ) -> ReviseResult:
        """Attempt to apply ``opinions`` to the checked-out ``workspace``.

        Synchronous by design: ``app.revise_executor`` runs this inside its
        single serialized worker thread (not the FastAPI event loop) and
        enforces the wall-clock ceiling itself, so implementations are free
        to block on subprocess calls exactly like ``app.ai_reviewer``'s
        ``_run_claude`` does today.
        """
        ...


class StubRunner:
    """P4b placeholder ``AIRunner``: no file changes, every opinion deferred.

    This lets the queue/workspace/git/re-notify machinery be exercised in
    full without the P5 claude orchestrator. Every opinion comes back
    ``unapplied`` with a fixed reason, which is exactly the "일부 반영" /
    "⚠️ 미반영" re-notify shape the rail must already support.
    """

    def run(
        self,
        workspace: Path,
        opinions: list[dict[str, Any]],
        session_ctx: dict[str, Any],
        timeout_seconds: int,
    ) -> ReviseResult:
        del workspace, session_ctx, timeout_seconds  # stub: unused
        unapplied = [
            {"opinion_id": opinion["id"], "reason": "AI 체인(P5) 미구현"} for opinion in opinions
        ]
        return ReviseResult(
            kind="ok",
            unapplied=unapplied,
            detail="stub: no file changes made (P5 orchestrator not yet implemented)",
        )


# ---------------------------------------------------------------------------
# P5 — single headless claude CLI runner (file-edit tools enabled)
# ---------------------------------------------------------------------------

# Env keys that could carry credentials into a child process. The GIT_* keys
# are never written to the shared ``os.environ`` (app.git_workspace passes
# them only via a per-subprocess-call env dict), but they — plus the app's
# own real secrets, which *do* live in the inherited ``os.environ`` this
# runner copies from — are stripped defensively before spawning `claude`
# anyway; that process needs none of them. ``ANTHROPIC_API_KEY`` is
# deliberately excluded: the `claude` CLI needs it for auth.
_CREDENTIAL_ENV_KEYS = frozenset(
    {
        "GIT_PASSWORD",
        "GIT_ASKPASS",
        "GIT_TERMINAL_PROMPT",
        "GIT_USERNAME",
        "GITLAB_TOKEN",
        "GITLAB_WEBHOOK_SECRET",
        "SLACK_BOT_TOKEN",
        "SLACK_SIGNING_SECRET",
        "ACTION_TOKEN_SECRET",
    }
)

_REVISE_INSTRUCTION = (
    "이 워크스페이스의 문서에 각 의견을 반영하라. 반영 불가한 의견은 이유와 함께 표기. "
    '작업 후 결과를 JSON으로 출력: {"applied":[opinion_id...], '
    '"unapplied":[{"opinion_id":..,"reason":..}...], "summary":"변경점 정리"}'
)

_REVISE_JSON_SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {
            "applied": {"type": "array", "items": {"type": "integer"}},
            "unapplied": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "opinion_id": {"type": "integer"},
                        "reason": {"type": "string"},
                    },
                    "required": ["opinion_id", "reason"],
                },
            },
            "summary": {"type": "string"},
        },
        "required": ["applied", "unapplied", "summary"],
    },
    ensure_ascii=False,
    separators=(",", ":"),
)


class ClaudeCliRunner:
    """P5 ``AIRunner``: one headless ``claude`` CLI call, file-edit tools on.

    Unlike ``app.ai_reviewer``'s read-only summarizer (``--tools ""``, a
    throwaway tempdir), this runner's whole point is to let the CLI read and
    *edit* files inside the checked-out MR workspace, then report back which
    opinions it applied. It is a single synchronous call — no session
    persistence, no multi-stage 3a/3b/3c orchestrator (that chain is
    explicitly deferred past this phase).

    ``settings`` is accepted via the constructor (not ``run``'s signature,
    which is fixed by the ``AIRunner`` protocol) — ``None`` resolves lazily
    to ``app.config.get_settings()`` at call time.

    Contract: every self-error this runner can hit — CLI not found, a
    timed-out or otherwise unlaunchable subprocess, a non-zero exit, or
    unparsable/incomplete JSON output — is returned as ``ReviseResult(kind=
    "failed", ...)``, never raised. ``run`` itself does not raise.
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
        settings = self._settings or _settings_singleton()

        claude_bin = settings.claude_bin or shutil.which("claude")
        if not claude_bin:
            return ReviseResult(kind="failed", detail="`claude` CLI not found on PATH")

        prompt = _build_revise_prompt(opinions, session_ctx)
        cmd = [
            claude_bin,
            "-p",
            "--permission-mode",
            "acceptEdits",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--output-format",
            "json",
            "--model",
            settings.ai_model,
            "--effort",
            settings.ai_effort,
            "--max-budget-usd",
            str(settings.ai_max_budget_usd),
            "--json-schema",
            _REVISE_JSON_SCHEMA,
        ]

        env = {
            key: value
            for key, value in _process_env().items()
            if key.upper() not in _CREDENTIAL_ENV_KEYS
        }

        secrets = [
            secret_value(value)
            for value in (
                settings.gitlab_token,
                settings.gitlab_webhook_secret,
                settings.slack_bot_token,
                settings.slack_signing_secret,
                settings.action_token_secret,
            )
        ]

        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                cwd=workspace,
                env=env,
                timeout=timeout_seconds,
            )
        except (subprocess.TimeoutExpired, OSError) as error:
            # A self-error launching/running the CLI subprocess itself: a
            # hang (``TimeoutExpired``) or an ``OSError`` — e.g.
            # ``FileNotFoundError`` when ``claude_bin`` resolves to a path
            # that does not actually exist/execute (a misconfigured
            # CLAUDE_BIN, or a stale ``shutil.which`` hit whose target was
            # since removed). Either way this must return ``kind="failed"``
            # exactly like every other failure mode in this method, never
            # propagate and crash the single serialized revise-executor
            # worker thread (see ``AIRunner.run``'s protocol docstring).
            if isinstance(error, subprocess.TimeoutExpired):
                detail = f"claude CLI timed out after {timeout_seconds}s"
            else:
                logger.warning("claude CLI failed to start: %s", type(error).__name__)
                detail = _redact(
                    f"claude CLI failed to start: {type(error).__name__}: {error}", secrets
                )[:1000]
            return ReviseResult(kind="failed", detail=detail)

        if proc.returncode != 0:
            detail = _redact(proc.stderr or proc.stdout or "no error output", secrets)[:1000]
            logger.warning("claude CLI exited %s", proc.returncode)
            return ReviseResult(kind="failed", detail=f"claude CLI exited {proc.returncode}: {detail}")

        return _redact_result(_parse_revise_output(proc.stdout, opinions), secrets)


def _redact_result(result: ReviseResult, secrets: list[str]) -> ReviseResult:
    """Redact any app secret out of a successful runner result before it is
    returned — the `claude` CLI's own text output (summary / unapplied
    reasons) is not otherwise constrained and could echo an env value it saw
    before ``_CREDENTIAL_ENV_KEYS`` stripped it from its child env, or one it
    picked up from a file it read in the workspace.
    """

    result.detail = _redact(result.detail, secrets)
    for entry in result.unapplied:
        reason = entry.get("reason")
        if isinstance(reason, str):
            entry["reason"] = _redact(reason, secrets)
    return result


def _process_env() -> dict[str, str]:
    """A copy of the current process environment — split out so tests can patch it."""

    return dict(os.environ)


def _build_revise_prompt(opinions: list[dict[str, Any]], session_ctx: dict[str, Any]) -> str:
    lines = ["# 리뷰 의견 반영 요청\n"]
    repo_slug = session_ctx.get("repo_slug")
    mr_iid = session_ctx.get("mr_iid")
    if repo_slug or mr_iid:
        lines.append(f"- 저장소: {repo_slug}\n- MR: !{mr_iid}\n\n")

    lines.append("## 의견 목록\n")
    for opinion in opinions:
        refs = opinion.get("question_refs") or ""
        lines.append(
            f"\n### opinion_id={opinion.get('id')}\n"
            f"본문: {opinion.get('body')}\n"
            f"question_refs: {refs}\n"
        )

    lines.append(f"\n---\n{_REVISE_INSTRUCTION}\n")
    return "".join(lines)


def _parse_revise_output(raw: str, opinions: list[dict[str, Any]]) -> ReviseResult:
    envelope = _extract_json_object(raw)
    if envelope is None:
        return ReviseResult(kind="failed", detail="claude CLI did not return valid JSON")

    payload = envelope
    structured_output = envelope.get("structured_output")
    if isinstance(structured_output, dict):
        payload = structured_output
    elif "applied" not in envelope and "unapplied" not in envelope:
        # Envelope form (subtype/result) rather than a bare result object.
        result_text = envelope.get("result")
        if isinstance(result_text, str):
            inner = _extract_json_object(result_text)
            if inner is not None:
                payload = inner

    applied_raw = payload.get("applied")
    unapplied_raw = payload.get("unapplied")
    summary = payload.get("summary")
    if not isinstance(applied_raw, list) or not isinstance(unapplied_raw, list):
        return ReviseResult(kind="failed", detail="claude CLI JSON missing applied/unapplied lists")

    applied_ids = {item for item in applied_raw}
    unapplied: list[dict[str, Any]] = []
    unapplied_ids: set[Any] = set()
    for entry in unapplied_raw:
        if not isinstance(entry, dict):
            continue
        opinion_id = entry.get("opinion_id")
        reason = entry.get("reason") or "(사유 없음)"
        unapplied.append({"opinion_id": opinion_id, "reason": reason})
        unapplied_ids.add(opinion_id)

    for opinion in opinions:
        opinion_id = opinion["id"]
        if opinion_id in applied_ids or opinion_id in unapplied_ids:
            continue
        unapplied.append({"opinion_id": opinion_id, "reason": "러너 응답에 누락"})

    detail = summary if isinstance(summary, str) else ""
    return ReviseResult(kind="ok", unapplied=unapplied, detail=detail)


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of CLI output, tolerating code fences."""

    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _settings_singleton() -> Settings:
    from app.config import get_settings

    return get_settings()
