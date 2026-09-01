"""Revise executor — the S4 revise loop's serialization queue + workspace +
runner + re-notify (P4b).

Ground truth: docs/mr-review-pipeline.html §S4① step 4 / §S4② and the
credential decision in
``.orchestration/reports/p4b-git-credential-tradeoff.md`` (option (b2)).
``app.slack_actions`` calls ``enqueue_revise`` immediately after the
``reviewing -> revising`` CAS transition succeeds and depends only on this
function's signature — it never reaches into the internals below.

Design notes carried over from P4a's interface stub, now resolved:

- **Serialization**: a single process-wide ``queue.Queue`` drained by exactly
  one daemon worker thread (uvicorn --workers 1, global concurrency ceiling
  of 1 per §⑦) — never a second thread/process.
- **``revising_since`` reset (M12)**: reset to "now" the moment a queued item
  is actually dequeued and execution starts, not at CAS-lock time. Because of
  this, the CAS-lock timestamp needed for ``app.db.unapplied_opinions``'s
  ``created_at <= 잠금 시각`` cutoff cannot be read from ``revising_since``
  (it drifts forward on every retry) — it is instead read from the latest
  ``event_log`` row with ``kind='opinion'`` for the session, which
  ``app.state_machine.cas_transition`` already writes on the
  ``reviewing -> revising`` transition and which is stable across retries.
- **Branch name**: ``review_session`` has no ``head_ref`` column (schema
  frozen this phase) — the source/target branch, title, url, and author are
  fetched from GitLab's ``get_merge_request`` at execution time instead, and
  reused for both the git checkout and the re-notify's action-token payload.
- **AI runner**: the actual claude orchestrator is P5 — this module only
  drives the ``app.ai_runner.AIRunner`` protocol (``StubRunner`` by default).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import queue
import sqlite3
import threading
import time
from types import SimpleNamespace
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app import git_workspace
from app.action_token import create_action_token
from app.ai_runner import AIRunner, ClaudeCliRunner, ReviseResult, StubRunner
from app.config import Settings, get_settings, secret_value
from app.db import get_connection, init_db, unapplied_opinions
from app.gitlab_client import GitLabClient
from app.ingest import _deliver_review_report
from app.slack_client import SlackClient
from app.state_machine import (
    MANUAL,
    REVIEWING,
    REVISING,
    advance_round_and_reset_attempts,
    cas_transition,
    record_revise_failure,
)

logger = logging.getLogger("uvicorn.error")

DEFAULT_RUNNER: AIRunner = StubRunner()


def _select_runner(settings: Settings) -> AIRunner:
    """Pick the ``AIRunner`` per ``settings.ai_runner`` ("stub" default, "claude" → P5)."""

    if settings.ai_runner == "claude":
        return ClaudeCliRunner(settings)
    return DEFAULT_RUNNER


_queue: "queue.Queue[QueueItem]" = queue.Queue()
_worker_thread: threading.Thread | None = None
_worker_lock = threading.Lock()


@dataclass
class QueueItem:
    """One serialized unit of revise work."""

    session_id: int
    enqueued_at: float


# ---------------------------------------------------------------------------
# Public interface — signature owned by app.slack_actions (P4a contract)
# ---------------------------------------------------------------------------
async def enqueue_revise(session_id: int) -> None:
    """Non-blocking enqueue of a revise-executor run for ``session_id``.

    Logs an ``event_log`` "enqueued" row, pushes onto the in-process queue,
    and ensures the single worker thread is running. Never awaits the actual
    revise work — the caller's ``reviewing -> revising`` CAS transition and
    Slack "개선 작업 진행 중" update happen independently of when the worker
    actually drains this item.
    """

    settings = get_settings()
    conn = get_connection(settings.db_path)
    init_db(conn)
    try:
        conn.execute(
            "INSERT INTO event_log (session_id, kind, detail) VALUES (?, 'enqueued', NULL)",
            (session_id,),
        )
        conn.commit()
    finally:
        conn.close()

    _queue.put_nowait(QueueItem(session_id=session_id, enqueued_at=time.time()))
    _ensure_worker_started()


def _ensure_worker_started() -> None:
    global _worker_thread
    with _worker_lock:
        if _worker_thread is None or not _worker_thread.is_alive():
            _worker_thread = threading.Thread(
                target=_worker_loop, name="revise-executor", daemon=True
            )
            _worker_thread.start()


def _worker_loop() -> None:
    while True:
        item = _queue.get()
        try:
            process_one(item)
        except Exception:
            logger.exception(
                "Revise executor: unhandled error processing session=%s", item.session_id
            )
        finally:
            _queue.task_done()


# ---------------------------------------------------------------------------
# Per-item processing — synchronous so both the worker thread and tests can
# drive it directly (no event-loop juggling required from callers).
# ---------------------------------------------------------------------------
def process_one(
    item: QueueItem,
    *,
    settings: Settings | None = None,
    runner: AIRunner | None = None,
) -> None:
    """Process one queued revise item end to end (steps (a)-(e) of §S4②)."""

    settings = settings or get_settings()
    runner = runner or _select_runner(settings)

    conn = get_connection(settings.db_path)
    init_db(conn)
    try:
        session = _load_session(conn, item.session_id)
        if session is None or session["status"] != REVISING:
            # Session already left `revising` by some other path (external
            # merge/close, a timeout sweeper, an operator) — nothing to do.
            return

        # (a) queue-wait ceiling — a stale item is treated as `kind=failed`
        # without ever starting work (no workspace/runner invocation).
        wait_seconds = time.time() - item.enqueued_at
        if wait_seconds > settings.revise_queue_wait_limit:
            _handle_failed(
                conn,
                settings,
                session,
                f"queue wait exceeded ({wait_seconds:.0f}s > {settings.revise_queue_wait_limit}s)",
            )
            return

        # (b) revising_since reset — execution is actually starting now (M12).
        _reset_revising_since(conn, session["id"])

        # (c) workspace preparation (GitLab MR fetch for branch/title/url/
        # author + git clone-or-fetch + checkout).
        try:
            mr_full, workspace = asyncio.run(_prepare_workspace(settings, session))
        except Exception as error:
            logger.exception("Revise workspace preparation failed: session=%s", session["id"])
            # Re-mask (some paths through _prepare_workspace — the two bare
            # RuntimeErrors, or a GitLabClient/httpx exception — never passed
            # through app.git_workspace's own _redact at all) with the same
            # 5-secret list app.git_workspace._git uses, then truncate, before
            # this ever reaches event_log/DB or (via _notify_manual) Slack.
            detail = git_workspace._redact(
                f"workspace preparation failed: {type(error).__name__}: {error}",
                git_workspace._redaction_secrets(settings),
            )[:1000]
            _handle_failed(conn, settings, session, detail)
            return

        # (d) unapplied opinions as of the CAS-lock time.
        locked_at = _lock_timestamp(conn, session["id"])
        opinions = [dict(row) for row in unapplied_opinions(conn, session["id"], locked_at)]

        # (e) runner.run, wall-clock bounded.
        try:
            result = _run_with_wall_clock(
                runner, workspace, opinions, _session_ctx(session), settings.revise_wall_clock_seconds
            )
        except Exception as error:
            _handle_failed(
                conn, settings, session, f"revise runner failed: {type(error).__name__}: {error}"
            )
            return

        if result.kind == "ok":
            _handle_ok(conn, settings, session, mr_full, workspace, opinions, result)
        else:
            _handle_failed(conn, settings, session, result.detail or "revise runner reported failure")
    finally:
        conn.close()


async def _prepare_workspace(settings: Settings, session: sqlite3.Row) -> tuple[dict[str, Any], Any]:
    if not settings.gitlab_token:
        raise RuntimeError("GITLAB_TOKEN is not configured")
    gitlab = GitLabClient(
        settings.gitlab_url, secret_value(settings.gitlab_token), verify_ssl=settings.gitlab_verify_ssl
    )
    mr_full = await gitlab.get_merge_request(session["project_id"], session["mr_iid"])
    branch = mr_full.get("source_branch")
    if not branch:
        raise RuntimeError("GitLab MR response is missing source_branch")
    workspace = git_workspace.ensure_workspace(
        settings, session["project_id"], session["mr_iid"], session["repo_slug"] or ""
    )
    git_workspace.checkout(settings, workspace, branch)
    return mr_full, workspace


def _run_with_wall_clock(
    runner: AIRunner,
    workspace: Any,
    opinions: list[dict[str, Any]],
    session_ctx: dict[str, Any],
    timeout_seconds: int,
) -> ReviseResult:
    """Enforce a hard wall-clock ceiling around a (synchronous) runner call.

    Runs the runner in a dedicated thread so a hang cannot block the single
    executor worker thread past the ceiling; the ceiling is on the executor
    side, independent of whatever internal timeout (if any) the runner itself
    applies to its own subprocess calls.
    """

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(runner.run, workspace, opinions, session_ctx, timeout_seconds)
    try:
        return future.result(timeout=timeout_seconds)
    except concurrent.futures.TimeoutError as error:
        raise TimeoutError(f"revise runner exceeded the {timeout_seconds}s wall clock") from error
    finally:
        executor.shutdown(wait=False)


def _compare_url(
    settings: Settings, repo_slug: str, old_sha: str | None, new_sha: str | None
) -> str | None:
    """Build a GitLab compare URL for ``old_sha...new_sha``, or ``None``.

    ``None`` when ``repo_slug`` is blank, either sha is blank, or the two
    shas are equal (nothing to compare — a commit-less round). Also
    ``None`` if ``GITLAB_URL``'s netloc cannot be safely stripped of any
    embedded userinfo (``user:pw@host``) — this link is posted to a public
    Slack channel, so a missing link is far safer than one that leaks
    credentials there (unlike ``git_workspace``'s subprocess-output
    redaction, there is no way to retract a posted Slack message).
    """

    if not repo_slug or not old_sha or not new_sha or old_sha == new_sha:
        return None
    try:
        parts = urlsplit(settings.gitlab_url)
        netloc = parts.netloc.rsplit("@", 1)[-1]
        if not netloc or "@" in netloc:
            return None
        base = urlunsplit((parts.scheme, netloc, parts.path, "", "")).rstrip("/")
    except Exception:
        return None
    return f"{base}/{repo_slug}/-/compare/{old_sha}...{new_sha}"


# ---------------------------------------------------------------------------
# Result handling
# ---------------------------------------------------------------------------
def _handle_ok(
    conn: sqlite3.Connection,
    settings: Settings,
    session: sqlite3.Row,
    mr_full: dict[str, Any],
    workspace: Any,
    opinions: list[dict[str, Any]],
    result: ReviseResult,
) -> None:
    session_id = session["id"]
    unapplied_ids = {entry.get("opinion_id") for entry in result.unapplied}
    reason_by_id = {entry.get("opinion_id"): entry.get("reason") for entry in result.unapplied}
    # Captured before any `review_session.mr_sha` write below — the pre-round sha.
    old_sha = session["mr_sha"]

    try:
        changed = git_workspace.has_changes(settings, workspace)
        if changed:
            git_workspace.commit_all(
                settings, workspace, f"revise: MR !{session['mr_iid']} round {session['round']}"
            )
            git_workspace.push(settings, workspace, mr_full.get("source_branch") or "")
    except Exception as error:
        _handle_failed(conn, settings, session, f"git commit/push failed: {type(error).__name__}")
        return

    new_sha = mr_full.get("sha")
    if changed:
        try:
            new_sha = git_workspace.current_sha(settings, workspace)
        except Exception:
            logger.exception(
                "Revise executor: could not read new HEAD sha after push (session=%s)", session_id
            )

    # The revising -> reviewing CAS is the leading precondition (F1): it runs
    # before any round/applied_round/mr_sha write, so a rejected CAS (someone
    # else already moved the session out of `revising` — e.g. a racing
    # human_push/timeout/manual transition) leaves the round counter,
    # opinion.applied_round, and review_session.mr_sha completely untouched.
    # Only an *accepted* CAS unlocks the rest of this round's bookkeeping.
    # CAS 패배 시에도 commit/push는 이미 원격 브랜치에 반영돼 있음(DB 불변 ≠
    # 원격 불변) — 세션은 merged/manual로 종료되므로 잔여 커밋은 사람 확인
    # 대상.
    accepted = cas_transition(conn, session_id, REVISING, REVIEWING, reason="revise_success")
    if not accepted:
        conn.execute(
            "INSERT INTO event_log (session_id, kind, detail) VALUES (?, 'revise_success_raced', ?)",
            (session_id, "session left `revising` before the revise->reviewing CAS could apply"),
        )
        conn.commit()
        return

    current_round = session["round"]
    for opinion in opinions:
        opinion_id = opinion["id"]
        if opinion_id in unapplied_ids:
            conn.execute(
                "UPDATE opinion SET last_verdict = ? WHERE id = ?",
                (reason_by_id.get(opinion_id) or result.detail or "unapplied", opinion_id),
            )
        else:
            conn.execute(
                "UPDATE opinion SET applied_round = ? WHERE id = ?", (current_round, opinion_id)
            )
    conn.commit()

    new_round = advance_round_and_reset_attempts(conn, session_id)

    if new_sha:
        conn.execute("UPDATE review_session SET mr_sha = ? WHERE id = ?", (new_sha, session_id))
        conn.commit()

    unapplied_render = [
        {"reason": reason_by_id.get(entry.get("opinion_id")) or "(사유 없음)"}
        for entry in result.unapplied
    ]

    summary = result.detail or None

    # 커밋 없는 라운드(new_sha 없음 또는 old_sha와 동일)에는 stat을 만들지 않는다
    # — diff_stat 실패는 best-effort: 라운드 진행/알림 발송을 절대 막지 않는다.
    stat_text: str | None = None
    if new_sha and new_sha != old_sha:
        try:
            stat_text = git_workspace.diff_stat(settings, workspace, old_sha, new_sha)
        except Exception:
            logger.warning(
                "Revise executor: diff stat failed (session=%s)", session_id, exc_info=True
            )

    compare = _compare_url(settings, session["repo_slug"], old_sha, new_sha)

    try:
        asyncio.run(
            _notify_revise_success(
                conn,
                settings,
                session,
                mr_full,
                new_sha or session["mr_sha"],
                new_round,
                unapplied_render,
                summary=summary,
                diff_stat=stat_text,
                compare_url=compare,
            )
        )
    except Exception:
        logger.exception("Revise executor: Slack re-notify failed (session=%s)", session_id)


def _handle_failed(
    conn: sqlite3.Connection, settings: Settings, session: sqlite3.Row, detail: str
) -> None:
    session_id = session["id"]
    attempts = record_revise_failure(conn, session_id, detail=detail)

    if attempts < 2:
        conn.execute(
            "INSERT INTO event_log (session_id, kind, detail) VALUES (?, 'requeued', ?)",
            (session_id, detail),
        )
        conn.commit()
        _queue.put_nowait(QueueItem(session_id=session_id, enqueued_at=time.time()))
        _ensure_worker_started()
        return

    accepted = cas_transition(
        conn, session_id, REVISING, MANUAL, reason="revise_attempts_exceeded", detail=detail
    )
    if accepted:
        try:
            asyncio.run(_notify_manual(settings, session, detail))
        except Exception:
            logger.exception("Revise executor: Slack manual-notify failed (session=%s)", session_id)


# ---------------------------------------------------------------------------
# Slack notification helpers
# ---------------------------------------------------------------------------
async def _notify_revise_success(
    conn: sqlite3.Connection,
    settings: Settings,
    session: sqlite3.Row,
    mr_full: dict[str, Any],
    sha: str,
    round_number: int,
    unapplied: list[dict[str, Any]],
    *,
    summary: str | None = None,
    diff_stat: str | None = None,
    compare_url: str | None = None,
) -> None:
    """Re-notify a completed revise round with a brand-new Slack message.

    사용자 결정: 라운드마다 새 알림 — unlike the other re-notify helpers in
    this module (``_notify_manual``), a completed round posts a *new*
    ``chat.postMessage`` (step b) rather than editing the session's existing
    message in place, moves ``review_session.slack_ts`` to it as soon as the
    post succeeds (step c), and then best-effort withdraws the previous
    message's buttons (step d) so only the new message stays actionable.
    """
    if not settings.slack_bot_token or not settings.action_token_secret:
        return
    session_id = session["id"]
    channel = session["slack_channel"]
    old_ts = session["slack_ts"]
    if not channel:
        return
    mr = _mr_from_session_and_details(session, mr_full, sha)
    token = create_action_token(mr, secret_value(settings.action_token_secret))
    client = SlackClient(secret_value(settings.slack_bot_token))

    posted = await client.post_revise_result(
        channel,
        mr,
        token,
        round_number=round_number,
        unapplied=unapplied,
        summary=summary,
        diff_stat=diff_stat,
        compare_url=compare_url,
    )
    new_ts = posted.get("ts") if isinstance(posted, dict) else None
    if new_ts:
        _update_slack_coordinates(conn, session_id, channel, new_ts)

    # Same delivery experience as the open notification: a best-effort HTML
    # report attached to the new round message's thread (report-...-rN-....html).
    # The round summary/diff stat/unapplied reasons ride in the report's
    # summary/key-changes/points-to-watch slots; the file context is fetched
    # fresh from GitLab at the new head sha. Every failure is swallowed --
    # the round notification itself is already final at this point.
    if settings.report_html_enabled and new_ts:
        try:
            context = None
            if settings.gitlab_token:
                try:
                    context = await GitLabClient(
                        settings.gitlab_url,
                        secret_value(settings.gitlab_token),
                        verify_ssl=settings.gitlab_verify_ssl,
                    ).fetch_mr_context(mr["project_id"], mr["iid"], mr["sha"])
                except Exception:
                    logger.warning(
                        "Revise executor: round report context fetch failed (session=%s)",
                        session_id,
                        exc_info=True,
                    )
            review_like = SimpleNamespace(
                summary=f"라운드 {round_number} 개선 완료 — {summary or '수정 요약 없음'}",
                key_changes=diff_stat.splitlines() if diff_stat else [],
                points_to_watch=[
                    item.get("reason") or "(사유 없음)" for item in unapplied
                ],
            )
            await _deliver_review_report(
                settings,
                mr,
                review_like,
                context,
                {"channel": channel, "ts": new_ts},
                session_id,
                round_number=round_number,
                client=client,
            )
        except Exception:
            logger.warning(
                "Revise executor: round report delivery failed (session=%s)",
                session_id,
                exc_info=True,
            )

    if not old_ts or old_ts == new_ts:
        return
    try:
        summary = f"!{session['mr_iid']} ({session['repo_slug'] or session['project_id']})"
        await client.withdraw_buttons(
            channel,
            old_ts,
            summary,
            f"라운드 {round_number} 개선 완료 — 새 알림을 확인해 주세요.",
        )
    except Exception:
        logger.warning(
            "Revise executor: old message button withdrawal failed (session=%s)",
            session_id,
            exc_info=True,
        )


_SLACK_DETAIL_LIMIT = 300


def _truncate_for_slack(detail: str) -> str:
    """Cap a ``detail`` string at ``_SLACK_DETAIL_LIMIT`` chars for Slack exposure.

    Slack is a broadly-visible channel; the DB-persisted ``detail``
    (event_log / review_session, already masked and capped at 1000 chars
    upstream) is not — this asymmetry is deliberate
    (.orchestration/reports/revise-error-surfacing-audit.md §5 GAP #5) and is
    applied only to the copy handed to ``_notify_manual``, never to the
    ``detail`` value written to the DB earlier in ``_handle_failed``.
    """

    if len(detail) <= _SLACK_DETAIL_LIMIT:
        return detail
    return detail[: _SLACK_DETAIL_LIMIT - 1] + "…"


async def _notify_manual(settings: Settings, session: sqlite3.Row, detail: str) -> None:
    if not settings.slack_bot_token:
        return
    channel = session["slack_channel"]
    message_ts = session["slack_ts"]
    if not channel or not message_ts:
        return
    summary = f"!{session['mr_iid']} ({session['repo_slug'] or session['project_id']})"
    client = SlackClient(secret_value(settings.slack_bot_token))
    reason = _truncate_for_slack(detail)
    await client.withdraw_buttons(
        channel, message_ts, summary, f"⚠️ 자동개선 재시도 한도 도달 — 수동 확인 필요 ({reason})"
    )


def _mr_from_session_and_details(
    session: sqlite3.Row, mr_full: dict[str, Any], sha: str
) -> dict[str, Any]:
    """Build the re-notify action-token payload from a fresh GitLab MR fetch.

    Unlike ``app.gitlab_webhook._mr_from_session`` (which has no MR fetch
    available and falls back to placeholders — a known, separately-tracked
    gap), this path always has ``mr_full`` from step (c)'s
    ``get_merge_request`` call, so title/url/branches/author are real values.
    """

    project_id: Any = session["project_id"]
    try:
        project_id = int(project_id)
    except (TypeError, ValueError):
        pass
    author = mr_full.get("author") if isinstance(mr_full.get("author"), dict) else {}
    return {
        "project_id": project_id,
        "repository": session["repo_slug"] or "",
        "iid": session["mr_iid"],
        "title": mr_full.get("title") or "",
        "url": mr_full.get("web_url") or "",
        "sha": sha,
        "head_ref": mr_full.get("source_branch") or "",
        "base_ref": mr_full.get("target_branch") or "",
        "author": author.get("username") or "",
    }


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
def _load_session(conn: sqlite3.Connection, session_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM review_session WHERE id = ?", (session_id,)).fetchone()


def _reset_revising_since(conn: sqlite3.Connection, session_id: int) -> None:
    conn.execute(
        "UPDATE review_session SET revising_since = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
        "WHERE id = ?",
        (session_id,),
    )
    conn.commit()


def _update_slack_coordinates(
    conn: sqlite3.Connection, session_id: int, channel: str, message_ts: str
) -> None:
    """Move ``review_session.slack_ts`` to a freshly posted re-notify message.

    Mirrors ``app.ingest._save_slack_coordinates``'s UPDATE statement exactly;
    duplicated here (rather than imported) to avoid a revise_executor <->
    ingest import cycle.
    """
    conn.execute(
        "UPDATE review_session SET slack_channel = ?, slack_ts = ?, "
        "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
        (channel, message_ts, session_id),
    )
    conn.commit()


def _lock_timestamp(conn: sqlite3.Connection, session_id: int) -> str:
    """The CAS-lock timestamp for this round's unapplied-opinion cutoff.

    Read from the latest ``event_log`` row with ``kind='opinion'`` (written
    automatically by ``cas_transition`` on the ``reviewing -> revising``
    edge) rather than ``revising_since``, because ``revising_since`` is reset
    forward on every retry (M12) and would otherwise let opinions submitted
    *during* a retry leak into the current round.
    """

    row = conn.execute(
        "SELECT created_at FROM event_log WHERE session_id = ? AND kind = 'opinion' "
        "ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    if row is not None:
        return str(row["created_at"])
    # Should not happen in practice (every `revising` session was entered via
    # an "opinion" CAS transition) — fail safe toward "now" so no opinion is
    # silently dropped from consideration.
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _session_ctx(session: sqlite3.Row) -> dict[str, Any]:
    return {
        "session_id": session["id"],
        "project_id": session["project_id"],
        "mr_iid": session["mr_iid"],
        "round": session["round"],
        "repo_slug": session["repo_slug"],
    }
