"""Tests for app/git_workspace.py (P4b §S4②).

Covers the clone/checkout/commit/push round trip against a local bare
repository (no real GitLab host needed — ``_remote_url`` is monkeypatched to
point at the bare repo's filesystem path instead of an ``oauth2@host`` URL,
since that constructor is orthogonal to the git plumbing under test), plus
two hardening properties the module docstring promises:

* the PAT is delivered to a network git call only via that *specific*
  ``subprocess.run(..., env=...)`` call — the real process-wide
  ``os.environ`` is never mutated.
* ``_redact`` (and, by extension, ``GitWorkspaceError`` messages built from
  git's stderr) strips the PAT out of anything about to be logged/raised.

Skipped entirely if no system ``git`` executable is available.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from app.config import get_settings
from app.git_workspace import (
    ASKPASS_SCRIPT_NAME,
    GitWorkspaceError,
    _ASKPASS_SCRIPT_BODY,
    _git,
    _redact,
    checkout,
    commit_all,
    current_sha,
    ensure_askpass_script,
    ensure_workspace,
    has_changes,
    push,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="system git not available")

PROJECT_ID = 99
MR_IID = 7
REPO_SLUG = "group/project"
FAKE_PAT = "glpat-fake-secret-value-000"


def _run(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture()
def bare_repo(tmp_path: Path) -> Path:
    """A local bare repo seeded with one commit on branch ``main``."""

    bare = tmp_path / "origin.git"
    bare.mkdir()
    _run("init", "--bare", "-b", "main", cwd=bare)

    seed = tmp_path / "seed"
    seed.mkdir()
    _run("init", "-b", "main", cwd=seed)
    _run("-c", "user.name=seed", "-c", "user.email=seed@localhost", "commit", "--allow-empty", "-m", "seed", cwd=seed)
    (seed / "file.txt").write_text("initial\n", encoding="utf-8")
    _run("add", "-A", cwd=seed)
    _run("-c", "user.name=seed", "-c", "user.email=seed@localhost", "commit", "-m", "add file", cwd=seed)
    _run("remote", "add", "origin", str(bare), cwd=seed)
    _run("push", "origin", "main", cwd=seed)
    return bare


@pytest.fixture()
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "workspace_root", str(tmp_path / "workspaces"))
    monkeypatch.setattr(settings, "gitlab_token", FAKE_PAT)
    monkeypatch.setattr(settings, "bot_git_name", "mr-review-bot")
    monkeypatch.setattr(settings, "bot_git_email", "mr-review-bot@localhost")
    return settings


def test_clone_checkout_commit_push_round_trip(monkeypatch, settings, bare_repo: Path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("app.git_workspace._remote_url", lambda settings, repo_slug: str(bare_repo))

    workspace = ensure_workspace(settings, PROJECT_ID, MR_IID, REPO_SLUG)
    assert (workspace / "file.txt").read_text(encoding="utf-8") == "initial\n"

    checkout(settings, workspace, "main")
    assert not has_changes(settings, workspace)

    (workspace / "file.txt").write_text("revised\n", encoding="utf-8")
    assert has_changes(settings, workspace)

    commit_all(settings, workspace, "revise: apply opinions")
    assert not has_changes(settings, workspace)

    push(settings, workspace, "main")
    new_sha = current_sha(settings, workspace)

    # Re-clone into a fresh location to prove the push actually landed on the
    # bare repo (not just the local workspace's own history).
    verify_dir = workspace.parent / "verify"
    _run("clone", str(bare_repo), str(verify_dir), cwd=workspace.parent)
    assert (verify_dir / "file.txt").read_text(encoding="utf-8") == "revised\n"
    verify_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=verify_dir, check=True, capture_output=True, text=True
    ).stdout.strip()
    assert verify_sha == new_sha

    # ensure_workspace called again on the already-cloned path takes the
    # fetch branch instead of re-cloning.
    ensure_workspace(settings, PROJECT_ID, MR_IID, REPO_SLUG)


def test_ensure_workspace_calls_do_not_leak_pat_into_process_environment(monkeypatch, settings, bare_repo: Path) -> None:  # type: ignore[no-untyped-def]
    import os

    monkeypatch.setattr("app.git_workspace._remote_url", lambda settings, repo_slug: str(bare_repo))

    before_keys = set(os.environ.keys())
    workspace = ensure_workspace(settings, PROJECT_ID, MR_IID, REPO_SLUG)
    checkout(settings, workspace, "main")
    push_capable_workspace = workspace
    (push_capable_workspace / "file.txt").write_text("leak-check\n", encoding="utf-8")
    commit_all(settings, push_capable_workspace, "revise: leak check")
    push(settings, push_capable_workspace, "main")

    after_keys = set(os.environ.keys())
    assert after_keys == before_keys
    assert "GIT_PASSWORD" not in os.environ
    assert "GIT_ASKPASS" not in os.environ
    assert FAKE_PAT not in "".join(f"{k}={v}" for k, v in os.environ.items())


def test_ensure_askpass_script_writes_current_platform_variant(settings) -> None:  # type: ignore[no-untyped-def]
    script_path = ensure_askpass_script(settings)

    assert script_path.name == ASKPASS_SCRIPT_NAME
    assert script_path.read_bytes().decode("utf-8") == _ASKPASS_SCRIPT_BODY
    if os.name == "nt":
        assert ASKPASS_SCRIPT_NAME.endswith(".bat")
        assert "@echo off" in _ASKPASS_SCRIPT_BODY
    else:
        assert ASKPASS_SCRIPT_NAME.endswith(".sh")
        assert _ASKPASS_SCRIPT_BODY.startswith("#!/bin/sh")
        assert oct(script_path.stat().st_mode)[-3:] == "700"


def test_ensure_askpass_script_overwrites_stale_content(settings) -> None:  # type: ignore[no-untyped-def]
    script_path = Path(settings.workspace_root) / ASKPASS_SCRIPT_NAME
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text("stale-content-from-a-different-platform", encoding="utf-8")

    ensure_askpass_script(settings)

    assert script_path.read_bytes().decode("utf-8") == _ASKPASS_SCRIPT_BODY


def test_redact_masks_secret_from_text() -> None:
    text = f"remote rejected: bad credentials for oauth2:{FAKE_PAT}@gitlab.example.com"
    redacted = _redact(text, [FAKE_PAT])
    assert FAKE_PAT not in redacted
    assert "***" in redacted


def test_git_error_message_never_contains_raw_pat_from_stderr(monkeypatch, settings, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    workspace = tmp_path / "err-workspace"
    workspace.mkdir()

    class _FakeCompletedProcess:
        returncode = 1
        stdout = ""
        stderr = f"fatal: authentication failed for https://oauth2:{FAKE_PAT}@gitlab.example.com/x.git"

    monkeypatch.setattr(
        "app.git_workspace.subprocess.run",
        lambda *args, **kwargs: _FakeCompletedProcess(),
    )

    with pytest.raises(GitWorkspaceError) as excinfo:
        _git(["fetch", "origin", "main"], cwd=workspace, settings=settings, network=True)

    assert FAKE_PAT not in str(excinfo.value)


def test_relative_workspace_root_yields_absolute_workspace_and_askpass_paths(
    monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    """A relative ``workspace_root`` (the config default, e.g. ``"workspaces"``)
    must still resolve to an absolute workspace directory *and* an absolute
    ``GIT_ASKPASS`` value in the env handed to the network git call — the
    exact defect reproduced live in
    ``.orchestration/reports/revise-workspace-fix.md`` §0: a relative value
    is silently re-resolved by the *child* git process against its own cwd
    (which can differ from the parent Python process's), so the askpass
    script's actual on-disk location and the value the child would resolve
    stop matching (§0's raw capture literally asks "do these match?" and
    answers "False"). ``subprocess.run`` is monkeypatched to capture its call
    args and fake a clean exit — no real git/network call is made. The test
    itself runs from ``tmp_path`` (via ``monkeypatch.chdir``), not the repo's
    own cwd, both to make the relative root unambiguous and so nothing lands
    under the repo working tree.
    """

    monkeypatch.chdir(tmp_path)
    settings = get_settings()
    monkeypatch.setattr(settings, "workspace_root", "workspaces")  # relative, like the config default
    monkeypatch.setattr(settings, "gitlab_token", FAKE_PAT)
    monkeypatch.setattr(
        "app.git_workspace._remote_url",
        lambda settings, repo_slug: "https://oauth2@gitlab.example.com/x.git",
    )

    captured: list[dict[str, Any]] = []

    class _FakeCompletedProcess:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        captured.append({"cmd": cmd, "cwd": kwargs.get("cwd"), "env": kwargs.get("env")})
        return _FakeCompletedProcess()

    monkeypatch.setattr("app.git_workspace.subprocess.run", _fake_run)

    workspace = ensure_workspace(settings, PROJECT_ID, MR_IID, REPO_SLUG)

    assert workspace.is_absolute()
    assert workspace == tmp_path / "workspaces" / str(PROJECT_ID) / str(MR_IID)

    assert len(captured) == 1  # clone only — no ".git" exists yet, so no fetch branch
    env = captured[0]["env"]
    assert env is not None
    askpass_in_env = Path(env["GIT_ASKPASS"])
    assert askpass_in_env.is_absolute()

    # The invariant the §0 repro found broken: the path handed to the child
    # via env must be the *same* absolute path the script actually gets
    # written to, regardless of the child's own cwd (clone's cwd is
    # workspace.parent, not the settings.workspace_root directory itself).
    actual_script_path = ensure_askpass_script(settings)
    assert askpass_in_env == actual_script_path


def test_git_error_message_masks_non_pat_app_secret_from_stderr(monkeypatch, settings, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """``_redaction_secrets`` scrubs *all 5* configured app secrets from git's
    stderr, not just the GitLab PAT this module uses for auth (the fix in
    ``.orchestration/reports/revise-workspace-fix.md`` §2) — a fake
    ``slack_bot_token`` value that happens to leak into git's stderr (e.g.
    echoed from a file under the checked-out workspace) must be masked out
    of the raised ``GitWorkspaceError`` message just as thoroughly as the PAT.
    """

    fake_slack_token = "xoxb-fake-slack-secret-000"
    monkeypatch.setattr(settings, "slack_bot_token", fake_slack_token)

    workspace = tmp_path / "err-workspace-non-pat"
    workspace.mkdir()

    class _FakeCompletedProcess:
        returncode = 1
        stdout = ""
        stderr = f"fatal: unexpected content leaked into stderr: {fake_slack_token}"

    monkeypatch.setattr(
        "app.git_workspace.subprocess.run",
        lambda *args, **kwargs: _FakeCompletedProcess(),
    )

    with pytest.raises(GitWorkspaceError) as excinfo:
        _git(["fetch", "origin", "main"], cwd=workspace, settings=settings, network=True)

    assert fake_slack_token not in str(excinfo.value)
