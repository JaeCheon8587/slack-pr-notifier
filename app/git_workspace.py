"""Git workspace management for the P4b revise executor (§S4②).

Ground truth: docs/mr-review-pipeline.html §S4② "git workspace fetch/checkout
of the MR branch" + the adopted credential decision
(.orchestration/reports/p4b-git-credential-tradeoff.md, option (b2)): a
tokenless remote URL (username ``oauth2`` embedded, no PAT) + a static
askpass script whose file body carries no secret + the PAT delivered only via
the ``env`` of the specific git subprocess call that needs it
(clone/fetch/push). The PAT never touches argv, ``.git/config``, or
``os.environ`` (each git call gets its own env dict copy — the process-wide
environment is never mutated, so a later ``claude`` subprocess spawn, which
does not pass ``env=`` per ``app/ai_reviewer.py``, cannot inherit it).
"""

from __future__ import annotations

import logging
import os
import stat
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from app.config import Settings, secret_value

logger = logging.getLogger("uvicorn.error")

ASKPASS_SCRIPT_NAME = "git-askpass.bat" if os.name == "nt" else "git-askpass.sh"

# No secret in this file body — it only echoes whatever the *child process's*
# env happens to hold under GIT_PASSWORD. The remote URL always embeds the
# username (`oauth2@host`, see `_remote_url`) so git only ever prompts for
# the password, never the username, keeping this script single-purpose.
# Windows uses a `.bat` batch script; POSIX uses a `#!/bin/sh` script (must
# be executable, hence the ``chmod 0700`` in ``ensure_askpass_script``).
_ASKPASS_SCRIPT_BODY = (
    "@echo off\r\necho %GIT_PASSWORD%\r\n"
    if os.name == "nt"
    else '#!/bin/sh\nprintf \'%s\\n\' "$GIT_PASSWORD"\n'
)

_NETWORK_TIMEOUT_SECONDS = 120
_LOCAL_TIMEOUT_SECONDS = 30


class GitWorkspaceError(RuntimeError):
    """Raised when a git subprocess call fails or times out."""


def _workspace_root(settings: Settings) -> Path:
    """Resolve ``settings.workspace_root`` to an absolute path.

    A relative ``workspace_root`` (e.g. the ``"workspaces"`` default) is
    ambiguous across the process boundaries this module crosses: the askpass
    script is written relative to *this* Python process's cwd
    (``ensure_askpass_script``), but the value handed to a git child process
    via ``GIT_ASKPASS`` is then resolved by *that child's own* cwd (which is
    the workspace's parent directory for clone, or the workspace itself for
    fetch/checkout) — a different directory. A relative value is therefore
    silently wrong from the child's perspective (``cannot spawn ...: No such
    file or directory``), which git treats as "no askpass available" and
    falls back to an interactive prompt that ``GIT_TERMINAL_PROMPT=0``
    immediately rejects — an authentication failure in well under a second,
    confirmed by a live repro (see
    ``.orchestration/reports/revise-workspace-fix.md`` §0). Resolving here
    once, and reusing this helper for both the workspace path and the
    askpass script path, keeps every path built from ``workspace_root``
    absolute and therefore stable regardless of which directory a given git
    subprocess call happens to run from.

    ``resolve()`` defaults to non-strict (no ``strict=True``), so this is
    safe to call even before ``workspace_root`` exists on disk.
    """

    return Path(settings.workspace_root).expanduser().resolve()


def workspace_path(settings: Settings, project_id: object, mr_iid: object) -> Path:
    """The MR-scoped workspace directory: ``workspace_root/{project_id}/{mr_iid}``."""

    return _workspace_root(settings) / str(project_id) / str(mr_iid)


def ensure_askpass_script(settings: Settings) -> Path:
    """Write the static, platform-appropriate askpass script (no secret in
    the body). Overwrites any existing file whose content does not match the
    current platform's variant — e.g. a stale `.bat`-style body left over
    from a prior run on a different OS — rather than trusting a bare
    ``exists()`` check to mean "already correct".

    Called lazily from every network git call rather than at FastAPI startup,
    so this module has no app-wiring dependency — it is self-contained.
    """

    root = _workspace_root(settings)
    root.mkdir(parents=True, exist_ok=True)
    script_path = root / ASKPASS_SCRIPT_NAME
    # newline="" disables universal-newline translation on both ends: the
    # body's line endings are platform-specific by construction (CRLF for the
    # .bat variant, LF for the POSIX shell variant), so writing/reading in
    # text mode with the default translation would otherwise mangle CRLF into
    # CRCRLF on Windows (each embedded "\n" gets re-translated to os.linesep).
    if (
        not script_path.exists()
        or script_path.read_text(encoding="utf-8", newline="") != _ASKPASS_SCRIPT_BODY
    ):
        script_path.write_text(_ASKPASS_SCRIPT_BODY, encoding="utf-8", newline="")
    if os.name != "nt":
        script_path.chmod(stat.S_IRWXU)
    return script_path


def _remote_url(settings: Settings, repo_slug: str) -> str:
    """Build a tokenless remote URL — ``oauth2`` username embedded, no PAT.

    Embedding the (non-secret) literal username means git only ever prompts
    for the password, so the askpass script never needs to distinguish
    prompt kinds.
    """

    parts = urlsplit(settings.gitlab_url)
    return f"{parts.scheme}://oauth2@{parts.netloc}/{repo_slug}.git"


def _redact(text: str, secrets: list[str]) -> str:
    """Defensive PAT redaction for anything about to be logged.

    The PAT is only ever passed via a per-call ``env`` dict (never argv,
    never ``.git/config``), so git's own stderr should not contain it — this
    is the second layer of defense the credential tradeoff doc calls for
    (§2 "stderr 레닥션"), in case a future git error message ever echoes it.
    """

    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "***")
    return redacted


def _redaction_secrets(settings: Settings) -> list[str]:
    """The full set of app secrets to scrub from git subprocess output.

    Matches ``app.ai_runner.ClaudeCliRunner``'s 5-secret list (``gitlab_token``,
    ``gitlab_webhook_secret``, ``slack_bot_token``, ``slack_signing_secret``,
    ``action_token_secret``) rather than just the GitLab PAT this module uses
    for git auth: git's stderr is free-form text and, however unlikely, could
    echo any of the app's other configured secrets (e.g. one present in a
    file under the checked-out workspace). Unset/empty secrets are dropped —
    ``_redact`` doing ``str.replace("", "***")`` against an empty secret
    would otherwise corrupt every character boundary of the text.
    """

    return [
        value
        for value in (
            secret_value(settings.gitlab_token),
            secret_value(settings.gitlab_webhook_secret),
            secret_value(settings.slack_bot_token),
            secret_value(settings.slack_signing_secret),
            secret_value(settings.action_token_secret),
        )
        if value
    ]


def _git(
    args: list[str],
    cwd: Path,
    settings: Settings,
    *,
    network: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run one git subprocess call with the shared hardening.

    Every call disables any inherited credential helper and applies the
    configured SSL-verify toggle. Only ``network`` calls (clone/fetch/push)
    get the askpass env — built as a *copy* of the parent environment plus
    the auth vars, passed to this ``subprocess.run`` call only; ``os.environ``
    itself is never written to.
    """

    cmd = [
        "git",
        "-c",
        "credential.helper=",
        "-c",
        f"http.sslVerify={'true' if settings.gitlab_verify_ssl else 'false'}",
        *args,
    ]

    pat = secret_value(settings.gitlab_token)
    env: dict[str, str] | None = None
    timeout = _LOCAL_TIMEOUT_SECONDS
    if network:
        askpass = ensure_askpass_script(settings)
        env = {
            **os.environ,
            "GIT_ASKPASS": str(askpass),
            "GIT_PASSWORD": pat,
            "GIT_TERMINAL_PROMPT": "0",
        }
        timeout = _NETWORK_TIMEOUT_SECONDS

    label = args[0] if args else "git"
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        # `from error` keeps `__cause__`, so TimeoutExpired's own str() (the
        # full argv) DOES reach the traceback wherever the caller logs this
        # via logger.exception (app.revise_executor.process_one's workspace-
        # prep failure path). Safety here relies solely on the invariant that
        # argv itself never carries a secret — the tokenless remote URL
        # assembly (`_remote_url`) and the `cmd` assembly above never embed
        # the PAT, which is delivered only via this call's `env`.
        raise GitWorkspaceError(f"git {label} timed out after {timeout}s") from error

    if proc.returncode != 0:
        secrets = _redaction_secrets(settings)
        details = _redact(proc.stderr or proc.stdout or "no output", secrets)[:1000]
        raise GitWorkspaceError(f"git {label} failed (exit {proc.returncode}): {details}")
    return proc


def ensure_workspace(
    settings: Settings, project_id: object, mr_iid: object, repo_slug: str
) -> Path:
    """Clone the MR's repo into its workspace dir, or fetch if already cloned."""

    path = workspace_path(settings, project_id, mr_iid)
    if (path / ".git").exists():
        _git(["fetch", "origin"], cwd=path, settings=settings, network=True)
        return path

    path.parent.mkdir(parents=True, exist_ok=True)
    remote = _remote_url(settings, repo_slug)
    _git(["clone", remote, str(path)], cwd=path.parent, settings=settings, network=True)
    return path


def checkout(settings: Settings, workspace: Path, branch: str) -> None:
    """Fetch and hard-checkout the MR's source branch, discarding stale local state."""

    _git(["fetch", "origin", branch], cwd=workspace, settings=settings, network=True)
    _git(["checkout", "-B", branch, f"origin/{branch}"], cwd=workspace, settings=settings)


def has_changes(settings: Settings, workspace: Path) -> bool:
    """True if the working tree has any uncommitted change (tracked or untracked)."""

    proc = _git(["status", "--porcelain"], cwd=workspace, settings=settings)
    return bool(proc.stdout.strip())


def commit_all(settings: Settings, workspace: Path, message: str) -> None:
    """Stage and commit all changes under the bot's git identity."""

    _git(["add", "-A"], cwd=workspace, settings=settings)
    name = settings.bot_git_name or settings.bot_username or "mr-review-bot"
    email = settings.bot_git_email or settings.bot_email or "mr-review-bot@localhost"
    _git(
        ["-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-m", message],
        cwd=workspace,
        settings=settings,
    )


def push(settings: Settings, workspace: Path, branch: str) -> None:
    """Push the current HEAD to the MR's source branch."""

    _git(["push", "origin", f"HEAD:{branch}"], cwd=workspace, settings=settings, network=True)


def current_sha(settings: Settings, workspace: Path) -> str:
    """Return the workspace's current HEAD sha."""

    proc = _git(["rev-parse", "HEAD"], cwd=workspace, settings=settings)
    return proc.stdout.strip()


def diff_stat(settings: Settings, workspace: Path, old_sha: str, new_sha: str) -> str:
    """Return ``git diff --stat`` between two shas (caller does the try/except)."""

    proc = _git(["diff", "--stat", f"{old_sha}..{new_sha}"], cwd=workspace, settings=settings)
    return proc.stdout.strip()
