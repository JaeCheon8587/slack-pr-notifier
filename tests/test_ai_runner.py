"""Tests for app/ai_runner.py — StubRunner regression + P5 ClaudeCliRunner.

ClaudeCliRunner's tests monkeypatch ``subprocess.run`` (no real `claude`
process spawned) and assert: normal JSON output splits applied/unapplied and
back-fills any opinion id missing from both lists, a non-zero exit or
timeout or broken JSON all produce ``kind="failed"``, the credential env
keys never reach the child process, and the prompt sent over stdin actually
contains each opinion's body.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.ai_runner import ClaudeCliRunner, ReviseResult, StubRunner
from app.config import Settings


def _settings(**overrides: object) -> Settings:
    base = {
        "_env_file": None,
        "claude_bin": "claude",
        "ai_model": "claude-opus-4-8",
        "ai_effort": "high",
        "ai_max_budget_usd": 0.5,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _settings_with_secrets(**overrides: object) -> Settings:
    """Settings with every app secret populated, for the credential-strip and
    redaction tests below."""

    base = {
        "gitlab_token": "glpat-super-secret-value",
        "gitlab_webhook_secret": "gl-webhook-super-secret",
        "slack_bot_token": "xoxb-slack-bot-super-secret",
        "slack_signing_secret": "slack-signing-super-secret",
        "action_token_secret": "action-token-super-secret",
    }
    base.update(overrides)
    return _settings(**base)


def _opinions() -> list[dict[str, object]]:
    return [
        {"id": 1, "body": "이 함수에 null 체크를 추가하세요", "question_refs": ""},
        {"id": 2, "body": "테스트 케이스를 추가하세요", "question_refs": "Q1"},
        {"id": 3, "body": "변수명을 명확히 하세요", "question_refs": ""},
    ]


def test_stub_runner_defers_every_opinion() -> None:
    opinions = _opinions()
    result = StubRunner().run(Path("."), opinions, {}, timeout_seconds=60)

    assert result.kind == "ok"
    assert {entry["opinion_id"] for entry in result.unapplied} == {1, 2, 3}


class _Result:
    def __init__(self, returncode: int, stdout: str, stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_claude_cli_runner_splits_applied_and_unapplied_and_backfills_missing(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        payload = {
            "applied": [1],
            "unapplied": [{"opinion_id": 2, "reason": "관련 파일을 찾을 수 없음"}],
            "summary": "1개 반영, 1개 보류",
        }
        return _Result(0, json.dumps(payload, ensure_ascii=False))

    monkeypatch.setattr("app.ai_runner.subprocess.run", fake_run)
    monkeypatch.setenv("GIT_PASSWORD", "super-secret-pat")
    monkeypatch.setenv("GIT_ASKPASS", "C:/workspaces/git-askpass.bat")

    runner = ClaudeCliRunner(_settings())
    opinions = _opinions()  # ids 1, 2, 3 — 3 is missing from both lists
    result = runner.run(Path("workspace"), opinions, {"repo_slug": "g/p", "mr_iid": 5}, 120)

    assert result.kind == "ok"
    unapplied_by_id = {entry["opinion_id"]: entry["reason"] for entry in result.unapplied}
    assert unapplied_by_id[2] == "관련 파일을 찾을 수 없음"
    assert unapplied_by_id[3] == "러너 응답에 누락"
    assert 1 not in unapplied_by_id
    assert result.detail == "1개 반영, 1개 보류"

    # env passed to the child process excludes git credential keys.
    env = captured["kwargs"]["env"]
    assert "GIT_PASSWORD" not in env
    assert "GIT_ASKPASS" not in env

    # cwd is the workspace (file-edit tools operate there), not a tempdir.
    assert captured["kwargs"]["cwd"] == Path("workspace")

    # prompt (stdin) includes each opinion's body text and opinion_id.
    prompt = captured["kwargs"]["input"]
    for opinion in opinions:
        assert opinion["body"] in prompt
        assert f"opinion_id={opinion['id']}" in prompt


def test_claude_cli_runner_nonzero_exit_is_failed(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        return _Result(1, stdout="", stderr="fatal: some git-adjacent error")

    monkeypatch.setattr("app.ai_runner.subprocess.run", fake_run)

    runner = ClaudeCliRunner(_settings())
    result = runner.run(Path("workspace"), _opinions(), {}, 60)

    assert result.kind == "failed"


def test_claude_cli_runner_timeout_is_failed(monkeypatch) -> None:
    import subprocess

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr("app.ai_runner.subprocess.run", fake_run)

    runner = ClaudeCliRunner(_settings())
    result = runner.run(Path("workspace"), _opinions(), {}, 5)

    assert result.kind == "failed"
    assert "timed out" in result.detail


def test_claude_cli_runner_file_not_found_error_is_failed(monkeypatch) -> None:
    """subprocess.run raising FileNotFoundError (e.g. a stale/misconfigured
    CLAUDE_BIN whose target no longer exists) must fold into kind="failed" —
    same self-error contract as a timeout, never propagated out of run()."""

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        raise FileNotFoundError("[Errno 2] No such file or directory: 'claude'")

    monkeypatch.setattr("app.ai_runner.subprocess.run", fake_run)

    runner = ClaudeCliRunner(_settings())
    result = runner.run(Path("workspace"), _opinions(), {}, 60)

    assert result.kind == "failed"


def test_claude_cli_runner_broken_json_is_failed(monkeypatch) -> None:
    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        return _Result(0, stdout="not json at all {{{")

    monkeypatch.setattr("app.ai_runner.subprocess.run", fake_run)

    runner = ClaudeCliRunner(_settings())
    result = runner.run(Path("workspace"), _opinions(), {}, 60)

    assert result.kind == "failed"


def test_claude_cli_runner_missing_claude_bin_is_failed(monkeypatch) -> None:
    monkeypatch.setattr("app.ai_runner.shutil.which", lambda _name: None)

    runner = ClaudeCliRunner(_settings(claude_bin=None))
    result = runner.run(Path("workspace"), _opinions(), {}, 60)

    assert result.kind == "failed"


def test_claude_cli_runner_strips_all_app_secret_env_keys_but_keeps_anthropic_key(
    monkeypatch,
) -> None:
    """The child `claude` process must not inherit any of the app's real
    secrets (GITLAB_TOKEN, GITLAB_WEBHOOK_SECRET, SLACK_BOT_TOKEN,
    SLACK_SIGNING_SECRET, ACTION_TOKEN_SECRET) or the GIT_* credential vars —
    but ANTHROPIC_API_KEY must survive, since the `claude` CLI needs it for
    auth."""

    captured: dict[str, object] = {}

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        captured["kwargs"] = kwargs
        payload = {"applied": [], "unapplied": [], "summary": ""}
        return _Result(0, json.dumps(payload))

    monkeypatch.setattr("app.ai_runner.subprocess.run", fake_run)

    removed_keys = [
        "GIT_PASSWORD",
        "GIT_ASKPASS",
        "GIT_TERMINAL_PROMPT",
        "GIT_USERNAME",
        "GITLAB_TOKEN",
        "GITLAB_WEBHOOK_SECRET",
        "SLACK_BOT_TOKEN",
        "SLACK_SIGNING_SECRET",
        "ACTION_TOKEN_SECRET",
    ]
    for key in removed_keys:
        monkeypatch.setenv(key, "leaked-value")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key-value")

    runner = ClaudeCliRunner(_settings_with_secrets())
    result = runner.run(Path("workspace"), _opinions(), {}, 60)

    assert result.kind == "ok"
    env = captured["kwargs"]["env"]
    for key in removed_keys:
        assert key not in env
    assert env["ANTHROPIC_API_KEY"] == "anthropic-key-value"


def test_claude_cli_runner_redacts_secrets_from_success_summary_and_reasons(
    monkeypatch,
) -> None:
    """Even on the success path, the runner must redact any configured app
    secret that shows up verbatim in the claude CLI's own `summary`/`reason`
    text before returning it."""

    gitlab_token = "glpat-super-secret-value"
    action_secret = "action-token-super-secret"

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        payload = {
            "applied": [1],
            "unapplied": [
                {
                    "opinion_id": 2,
                    "reason": f"인증 실패 ({action_secret}) — 반영 불가",
                }
            ],
            "summary": f"토큰 {gitlab_token} 발견, 조치 필요",
        }
        return _Result(0, json.dumps(payload, ensure_ascii=False))

    monkeypatch.setattr("app.ai_runner.subprocess.run", fake_run)

    settings = _settings_with_secrets(
        gitlab_token=gitlab_token, action_token_secret=action_secret
    )
    runner = ClaudeCliRunner(settings)
    result = runner.run(Path("workspace"), _opinions(), {}, 60)

    assert result.kind == "ok"
    assert gitlab_token not in result.detail
    assert "***" in result.detail

    reason = next(entry["reason"] for entry in result.unapplied if entry["opinion_id"] == 2)
    assert action_secret not in reason
    assert "***" in reason


# ---------------------------------------------------------------------------
# ReviseResult's two optional staged-workflow fields (docs/revise-workflow.html
# Part 4): both default to an empty list, and empty is what every existing
# runner produces — that is the whole no-regression contract.
# ---------------------------------------------------------------------------
def test_revise_result_new_fields_default_to_empty_lists() -> None:
    result = ReviseResult(kind="ok")

    assert result.commit_paths == []
    assert result.clarify == []
    # Still constructible positionally/by keyword exactly as before.
    legacy = ReviseResult(kind="ok", unapplied=[{"opinion_id": 1, "reason": "x"}], detail="d")
    assert legacy.commit_paths == []
    assert legacy.clarify == []


def test_revise_result_default_lists_are_not_shared_between_instances() -> None:
    """``field(default_factory=list)``, not a mutable class-level default —
    one result appending to ``commit_paths`` must not touch another's."""

    first = ReviseResult(kind="ok")
    second = ReviseResult(kind="ok")
    first.commit_paths.append("docs/a.md")
    first.clarify.append({"opinion_id": 1, "question": "?", "candidates": []})

    assert second.commit_paths == []
    assert second.clarify == []


def test_stub_runner_still_returns_empty_commit_paths_and_clarify() -> None:
    """The default runner is untouched by the new fields — no behavior change
    for AI_RUNNER=stub, which is still the config default."""

    result = StubRunner().run(Path("."), _opinions(), {}, timeout_seconds=60)

    assert result.commit_paths == []
    assert result.clarify == []


# ---------------------------------------------------------------------------
# Runner selection: "staged" picks app.revise_workflow.runner.StagedRunner.
#
# That package does not exist yet (it is another work item), so the branch is
# deliberately a *lazy* import inside ``_select_runner`` and this test stands a
# fake module in for it via sys.modules — which also proves the import really
# is deferred to call time rather than executed at module load.
# ---------------------------------------------------------------------------
def _install_fake_staged_runner(monkeypatch):  # type: ignore[no-untyped-def]
    import sys
    import types

    class FakeStagedRunner:
        def __init__(self, settings: Settings) -> None:
            self.settings = settings

        def run(self, workspace, opinions, session_ctx, timeout_seconds):  # type: ignore[no-untyped-def]
            return ReviseResult(kind="ok")

    package = types.ModuleType("app.revise_workflow")
    package.__path__ = []  # type: ignore[attr-defined]
    module = types.ModuleType("app.revise_workflow.runner")
    module.StagedRunner = FakeStagedRunner  # type: ignore[attr-defined]
    package.runner = module  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "app.revise_workflow", package)
    monkeypatch.setitem(sys.modules, "app.revise_workflow.runner", module)
    return FakeStagedRunner


def test_select_runner_staged_picks_staged_runner(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.revise_executor import _select_runner

    fake_cls = _install_fake_staged_runner(monkeypatch)
    settings = _settings(ai_runner="staged")

    runner = _select_runner(settings)

    assert isinstance(runner, fake_cls)
    assert runner.settings is settings


def test_select_runner_defaults_are_unchanged_by_the_staged_branch(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """No regression: "stub" (the default), "claude", and anything unknown
    resolve exactly as they did before "staged" existed."""

    from app.revise_executor import DEFAULT_RUNNER, _select_runner

    _install_fake_staged_runner(monkeypatch)

    assert _select_runner(_settings()) is DEFAULT_RUNNER  # ai_runner defaults to "stub"
    assert _select_runner(_settings(ai_runner="stub")) is DEFAULT_RUNNER
    assert isinstance(_select_runner(_settings(ai_runner="claude")), ClaudeCliRunner)
    assert _select_runner(_settings(ai_runner="nonsense")) is DEFAULT_RUNNER


def test_select_runner_does_not_import_revise_workflow_unless_staged(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The staged import must stay *inside* the branch.

    ``app.revise_executor`` is imported at app startup no matter which runner
    is configured, so it has to stay importable — and every non-staged
    selection has to keep working — even when ``app.revise_workflow.runner``
    cannot be imported at all. A ``None`` entry in ``sys.modules`` is the
    standard way to make an import raise, and it simulates exactly that.
    """

    import sys

    from app.revise_executor import DEFAULT_RUNNER, _select_runner

    monkeypatch.setitem(sys.modules, "app.revise_workflow.runner", None)

    # Unaffected: nothing outside the "staged" branch touches that module.
    assert _select_runner(_settings()) is DEFAULT_RUNNER
    assert isinstance(_select_runner(_settings(ai_runner="claude")), ClaudeCliRunner)

    # And the branch itself is the only thing that pays for the import.
    with pytest.raises(ImportError):
        _select_runner(_settings(ai_runner="staged"))
