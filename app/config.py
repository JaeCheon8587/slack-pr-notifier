import json
from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    app_name: str = "Slack MR Notifier"
    app_env: str = "local"
    gitlab_url: str = "https://gitlab.com"
    gitlab_webhook_secret: SecretStr | None = None
    gitlab_token: SecretStr | None = None
    gitlab_verify_ssl: bool = True
    slack_bot_token: SecretStr | None = None
    slack_signing_secret: SecretStr | None = None
    slack_channel_id: str | None = None

    # Socket Mode inbound path (app/slack_socket.py) — an outbound-only
    # WebSocket connection used to receive [승인]/[의견] button and modal
    # interactions when Slack -> this host inbound HTTP is blocked by network
    # policy (same class of constraint as the P.2 GitLab poller). Off by
    # default: app.main's lifespan calls app.slack_socket.start_socket_mode
    # unconditionally (same pattern as app.gitlab_poller.start_poller), which
    # is a no-op unless slack_socket_mode is True *and* slack_app_token is
    # configured (see start_socket_mode's own activation gate).
    slack_app_token: SecretStr | None = None
    slack_socket_mode: bool = False

    # Session reviewer mapping (v4.1 — replaces the old global
    # slack_allowed_user_ids allowlist). JSON object mapping repo_slug
    # (review_session.repo_slug, e.g. "group/project") to either a
    # comma-separated string or a list of Slack user IDs, e.g.:
    #   {"group/project": "U111,U222", "group/other": ["U333"]}
    # A repo with no entry (or an empty entry) has an empty reviewer set —
    # fail-closed: no Slack user is authorized to approve/merge that
    # session's MRs (see gitlab_webhook.py's manual-branch-on-empty-mapping
    # notification guard and slack_actions.py's step-0 authorization check).
    reviewer_map: str = ""

    action_token_secret: SecretStr | None = None

    # Bot account identity for the GitLab webhook rail's human-push policy
    # (app/gitlab_webhook.py): a push whose actor matches either field is the
    # middleware's own revise-commit echo and must not re-trigger anything.
    bot_username: str | None = None
    bot_email: str | None = None

    # Bot git commit identity for the P4b revise executor's own commits
    # (app/git_workspace.py commit_all). Distinct from bot_username/bot_email
    # above since GitLab commit author name/email need not match the bot's
    # Slack/GitLab account fields exactly; falls back to bot_username/
    # bot_email when unset.
    bot_git_name: str | None = None
    bot_git_email: str | None = None

    # P4b revise executor (app/revise_executor.py, app/git_workspace.py):
    # per-MR git workspace root directory, the queue-wait ceiling before a
    # dequeued item is treated as `kind=failed` (§S4② / docs §⑦ "MR 단위
    # 직렬화 큐 대기 상한 = 10분"), and the wall-clock ceiling for one runner
    # invocation.
    workspace_root: str = "workspaces"
    revise_queue_wait_limit: int = 600
    revise_wall_clock_seconds: int = 900

    # SQLite DB path for the MR review pipeline (app/db.py). Relative paths
    # are resolved from the process's working directory.
    db_path: str = "mr_review.db"

    # P6 timeout sweeper (app/sweeper.py): periodic sweep interval, plus the
    # `merging`/`revising` per-state timeouts before a stuck session is
    # force-transitioned to `manual`. Per docs §⑦ "스위퍼 임계 > 세션 상한 +
    # 여유", revising_timeout (1200s) is set above the revise executor's own
    # wall-clock ceiling (revise_wall_clock_seconds, default 900s) plus margin.
    sweep_interval: int = 60
    merging_timeout: int = 300
    revising_timeout: int = 1200

    # P6 Slack notify retry queue (app/notify_queue.py): linear backoff
    # (notify_retry_base * attempts seconds) between retries, up to
    # notify_max_attempts before an outbox item is marked `failed`.
    notify_retry_base: int = 30
    notify_max_attempts: int = 5

    # Polling mode (P.2 폴링 수집 경로 — app/gitlab_poller.py; used when inbound
    # webhooks are blocked by firewall/network policy). Off by default: the
    # background poller thread (app.main's lifespan, via start_poller) only
    # starts when poll_enabled is True *and* at least one target project id
    # is configured (see poll_project_ids_parsed below).
    poll_enabled: bool = False
    poll_interval: int = 60
    # Comma-separated GitLab project ids to poll for open MRs, e.g. "12,918".
    poll_project_ids: str = ""
    # Deprecated single-project setting, absorbed as a fallback by
    # poll_project_ids_parsed below when poll_project_ids is left empty.
    poll_project_id: int | str | None = None  # GitLab numeric project id to poll for open MRs

    # AI review (Claude Code CLI, invoked headlessly)
    ai_enabled: bool = True
    ai_model: str = "claude-opus-4-8"
    ai_effort: str = "high"
    ai_max_input_chars: int = 240000
    ai_timeout_seconds: int = 180
    ai_max_budget_usd: float = 1.0
    claude_bin: str | None = None  # override the `claude` executable path; None → PATH lookup

    # P5 revise runner selection (app/ai_runner.py, app/revise_executor.py):
    # "stub" (default — no file changes, every opinion deferred) or "claude"
    # (ClaudeCliRunner: single headless claude CLI call with file-edit tools
    # enabled against the checked-out workspace). Reuses `claude_bin` above
    # for the executable path — no separate setting needed.
    ai_runner: str = "stub"

    # mrdoc pipeline (app/mrdoc/ — deterministic doc-MR review, Phase 1):
    # disabled until the satellite agents land; doc_ratio routes an MR into
    # the mrdoc pipeline when >=80% of changed files are md/mdx.
    mrdoc_enabled: bool = False
    mrdoc_doc_ratio_threshold: float = 0.8
    mrdoc_satellite_model: str = "claude-sonnet-4-5"
    mrdoc_satellite_budget_usd: float = 1.0
    mrdoc_max_files: int = 40
    mrdoc_fanout: int = 5

    # Review report delivery (app/report_html.py, app/ingest.py): render the
    # AI review + MR diffs into a standalone HTML file, archive it under
    # report_html_dir, and upload it to the Slack notification's thread via
    # the Slack files API v2 (requires the files:write bot scope).
    report_html_enabled: bool = True
    report_html_dir: str = "reports"

    @property
    def reviewer_map_parsed(self) -> dict[str, frozenset[str]]:
        """Parse ``reviewer_map`` into repo_slug -> frozenset(Slack user IDs).

        Malformed JSON, or a non-object top level value, is treated as an
        empty mapping (fail-closed — every repo has an empty reviewer set).
        """

        if not self.reviewer_map:
            return {}
        try:
            raw = json.loads(self.reviewer_map)
        except (json.JSONDecodeError, TypeError):
            return {}
        if not isinstance(raw, dict):
            return {}

        result: dict[str, frozenset[str]] = {}
        for repo, ids in raw.items():
            if isinstance(ids, str):
                id_list = [user_id.strip() for user_id in ids.split(",") if user_id.strip()]
            elif isinstance(ids, list):
                id_list = [str(user_id).strip() for user_id in ids if str(user_id).strip()]
            else:
                id_list = []
            result[str(repo)] = frozenset(id_list)
        return result

    def reviewers_for(self, repo_slug: str | None) -> frozenset[str]:
        """Return the Slack reviewer set mapped to ``repo_slug`` (empty if unmapped)."""

        if not repo_slug:
            return frozenset()
        return self.reviewer_map_parsed.get(repo_slug, frozenset())

    @property
    def poll_project_ids_parsed(self) -> list[str]:
        """Parse ``poll_project_ids`` (comma-separated) into project id strings.

        Falls back to the deprecated single-project ``poll_project_id`` when
        ``poll_project_ids`` is empty, for backward compatibility. An empty
        result means the poller (app.gitlab_poller.start_poller) has nothing
        configured to poll and stays off regardless of ``poll_enabled``.
        """

        if self.poll_project_ids:
            return [pid.strip() for pid in self.poll_project_ids.split(",") if pid.strip()]
        if self.poll_project_id not in (None, ""):
            return [str(self.poll_project_id)]
        return []

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


def secret_value(value: SecretStr | str | None) -> str:
    """Return the plaintext secret, or "" if unset.

    Accepts a plain ``str`` too so call sites remain safe if a test or caller
    assigns a raw string to a ``SecretStr`` field (pydantic does not coerce
    attribute assignment outside of validation).
    """

    if value is None:
        return ""
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    return value
