import json
from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    app_name: str = "Slack MR Notifier"
    app_env: str = "local"

    # Master switch for application logging (app/logging_setup.py). Every
    # module in this package logs through the single "uvicorn.error" logger,
    # so one flag covers all of them plus uvicorn's own request access log.
    # Off by default: the middleware runs silent unless someone asks for
    # logs, and app/main.py applies the switch at import time. Turning it on
    # does not pin a level — uvicorn's --log-level still decides that.
    log_enabled: bool = False

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
    #
    # 타이머 불변식 (docs/revise-workflow.html Part 4 "설정"):
    #     revise_queue_wait_limit < revise_wall_clock_seconds < revising_timeout
    # i.e. 큐 대기 < 세션 벽시계 < 스위퍼 임계값. The staged runner turns one
    # revise round into three separate headless CLI calls plus deterministic
    # nodes, so the session wall clock is raised 900 -> 2400s; the sweeper
    # threshold (``revising_timeout`` below) is raised in lockstep to 3000s so
    # it still sits above the wall clock plus margin. Raising one of the three
    # without the others breaks the invariant — a sweeper that fires *below*
    # the wall clock force-transitions a still-running session to `manual`.
    workspace_root: str = "workspaces"
    revise_queue_wait_limit: int = 600
    revise_wall_clock_seconds: int = 2400

    # P5-staged revise workflow (app/revise_workflow/, selected by
    # ``ai_runner = "staged"``): the per-stage ceilings for one of the three
    # headless CLI calls (3a intent / 3b edit / 3c verify), plus the
    # deterministic gate's change-size limits and the per-session cap on
    # 되물음(clarify) rounds.
    #
    # NOTE: ``ai_max_budget_usd`` below is applied *per CLI invocation*, not
    # per revise round — with three staged calls its effective ceiling for one
    # round is 3x its nominal value. ``revise_stage_budget_usd`` exists to
    # carry a separate, explicitly per-stage budget for the staged runner
    # instead of relying on that (docs/revise-workflow.html Part 4 미결 5).
    revise_stage_timeout_seconds: int = 600
    # Measured, not guessed: a real 3b edit call on a 3-file docs repo cost
    # $0.247 over 5 turns / 32s (one opinion, one cross-doc companion edit).
    # The first smoke run of the same stage took 76s and died on a $0.5
    # ceiling, so turn count -- and therefore cost -- varies by roughly 2x
    # between runs of identical input. A ceiling exists to stop a runaway
    # loop, not to fail ordinary work, so it sits ~6x over the measurement.
    # Nominal worst case is 3x this per round; actual observed spend for a
    # full round is ~$0.75, comparable to the $0.42 of the single-call runner
    # it replaces (.orchestration/reports/smoke-claude-runner.md).
    # Retained for configuration compatibility — the staged runner's stages
    # are now `codex exec` calls, which have no per-call budget flag.
    revise_stage_budget_usd: float = 1.5
    # The staged runner's three LLM calls are `codex exec` invocations
    # (revise_workflow.runner._stage): empty model -> the CLI's configured
    # default, effort -> `-c model_reasoning_effort=...` on every stage.
    revise_stage_model: str = ""
    revise_stage_effort: str = "high"
    revise_max_changed_files: int = 10
    revise_max_changed_lines: int = 400
    revise_clarify_limit: int = 2

    # SQLite DB path for the MR review pipeline (app/db.py). Relative paths
    # are resolved from the process's working directory.
    db_path: str = "mr_review.db"

    # P6 timeout sweeper (app/sweeper.py): periodic sweep interval, plus the
    # `merging`/`revising` per-state timeouts before a stuck session is
    # force-transitioned to `manual`. Per docs §⑦ "스위퍼 임계 > 세션 상한 +
    # 여유", revising_timeout (3000s) is set above the revise executor's own
    # wall-clock ceiling (revise_wall_clock_seconds, default 2400s) plus
    # margin — 불변식: 스위퍼 임계값(revising_timeout) > 세션 벽시계
    # (revise_wall_clock_seconds) + 여유. Both were raised together (1200 ->
    # 3000 / 900 -> 2400) for the staged runner's three-call round; see the
    # revise-executor block above for the full three-term invariant.
    sweep_interval: int = 60
    merging_timeout: int = 300
    revising_timeout: int = 3000

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
    # Applied per *CLI invocation*, not per revise round or per stage —
    # a staged round makes three calls and so can spend up to 3x this
    # value. See ``revise_stage_budget_usd`` above.
    ai_max_budget_usd: float = 1.0
    claude_bin: str | None = None  # override the `claude` executable path; None → PATH lookup
    codex_bin: str | None = None  # override the `codex` executable path; None → PATH lookup

    # P5 revise runner selection (app/ai_runner.py, app/revise_executor.py):
    # "stub" (default — no file changes, every opinion deferred), "claude"
    # (ClaudeCliRunner: single headless claude CLI call with file-edit tools
    # enabled against the checked-out workspace), or "staged"
    # (app.revise_workflow.runner.StagedRunner: the 3a/3b/3c staged workflow
    # of docs/revise-workflow.html, imported lazily by
    # ``app.revise_executor._select_runner`` so this module stays importable
    # while that package does not exist yet). Reuses `codex_bin` above for
    # the executable path — no separate setting needed. The default stays
    # "stub" so nothing about the current rail changes by adding "staged".
    ai_runner: str = "stub"

    # mrdoc pipeline (app/mrdoc/ — deterministic doc-MR review, Phase 1):
    # disabled until the satellite agents land; doc_ratio routes an MR into
    # the mrdoc pipeline when >=80% of changed files are md/mdx.
    mrdoc_enabled: bool = False
    mrdoc_doc_ratio_threshold: float = 0.8
    mrdoc_satellite_model: str = ""  # empty → the Codex CLI default model
    mrdoc_satellite_budget_usd: float = 1.0
    mrdoc_satellite_enabled: bool = False
    mrdoc_satellite_timeout_seconds: int = 600
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
