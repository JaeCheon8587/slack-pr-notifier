"""Slack notify outbox (P6) — a durable retry queue for Slack sends that must
not be silently lost (e.g. a state-transition's "사유 갱신" main-message
update).

Ground truth: ``app.db``'s ``event_log`` table already carries the outbox
columns this module needs (``send_state`` pending/sent/failed, ``attempts``)
— no schema change. A pending row is durable (survives a process restart);
``requeue_pending_on_start`` re-loads any row still ``pending`` back onto the
in-process queue at startup.

This module is intentionally *not* wired into every existing Slack call
site — CHANGE SPEC forbids a wholesale rewrite of existing callers
(app/revise_executor.py, app/gitlab_webhook.py, app/slack_actions.py keep
sending directly). It is provided as an opt-in path, used so far only by
app/sweeper.py's own manual-transition notices.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

from app.config import Settings, get_settings, secret_value
from app.db import get_connection, init_db
from app.slack_client import SlackClient

logger = logging.getLogger("uvicorn.error")

NOTIFY_KIND = "notify"

_queue: "queue.Queue[NotifyItem]" = queue.Queue()
_worker_thread: threading.Thread | None = None
_worker_lock = threading.Lock()
_stop_event = threading.Event()

SlackClientFactory = Callable[[str], Any]


@dataclass
class NotifyItem:
    """One outbox item — the durable state lives in event_log, this is only
    the in-process handle used to route it to the worker."""

    event_log_id: int


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------
def enqueue(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    session_id: int,
    method: str,
    kwargs: dict[str, Any],
) -> int:
    """Persist a Slack-send request and push it onto the in-process queue.

    Returns the ``event_log.id`` of the pending outbox row. Non-blocking:
    the actual Slack call happens on the worker thread (or synchronously via
    ``process_one`` in tests).
    """

    detail = json.dumps({"method": method, "kwargs": kwargs}, ensure_ascii=False)
    cur = conn.execute(
        "INSERT INTO event_log (session_id, kind, detail, send_state, attempts) "
        "VALUES (?, ?, ?, 'pending', 0)",
        (session_id, NOTIFY_KIND, detail),
    )
    conn.commit()
    event_log_id = int(cur.lastrowid)

    _queue.put_nowait(NotifyItem(event_log_id=event_log_id))
    _ensure_worker_started(settings)
    return event_log_id


def process_one(
    conn: sqlite3.Connection,
    settings: Settings,
    event_log_id: int,
    *,
    slack_client_factory: SlackClientFactory | None = None,
) -> str:
    """Attempt one Slack send for a pending outbox row. Synchronous — usable
    directly from tests as well as from the worker thread.

    Returns the resulting ``send_state``: ``"sent"``, ``"failed"``, or
    ``"pending"`` (retryable — attempts was incremented but the max-attempts
    ceiling was not reached).  Returns the row's current ``send_state``
    unchanged (a no-op) if the row is missing or already resolved
    (``sent``/``failed``), so a duplicate/late requeue is harmless.
    """

    row = conn.execute("SELECT * FROM event_log WHERE id = ?", (event_log_id,)).fetchone()
    if row is None:
        return "missing"
    if row["send_state"] != "pending":
        return str(row["send_state"])

    try:
        payload = json.loads(row["detail"] or "{}")
    except json.JSONDecodeError:
        _mark_failed(conn, event_log_id, "invalid outbox payload (unparsable JSON)")
        return "failed"

    method = payload.get("method")
    kwargs = payload.get("kwargs") or {}

    if not settings.slack_bot_token:
        _mark_failed(conn, event_log_id, "SLACK_BOT_TOKEN is not configured")
        return "failed"

    factory = slack_client_factory or SlackClient
    client = factory(secret_value(settings.slack_bot_token))
    send_fn = getattr(client, method, None) if method else None
    if send_fn is None:
        _mark_failed(conn, event_log_id, f"unknown Slack client method: {method!r}")
        return "failed"

    try:
        asyncio.run(send_fn(**kwargs))
    except Exception as error:
        attempts = int(row["attempts"]) + 1
        if attempts >= settings.notify_max_attempts:
            _mark_failed(
                conn, event_log_id, f"{type(error).__name__} (attempts={attempts})", attempts=attempts
            )
            return "failed"
        conn.execute(
            "UPDATE event_log SET attempts = ?, updated_at = ? WHERE id = ?",
            (attempts, _now(), event_log_id),
        )
        conn.commit()
        return "pending"

    conn.execute(
        "UPDATE event_log SET send_state = 'sent', updated_at = ? WHERE id = ?",
        (_now(), event_log_id),
    )
    conn.commit()
    return "sent"


def _mark_failed(
    conn: sqlite3.Connection, event_log_id: int, detail: str, *, attempts: int | None = None
) -> None:
    if attempts is None:
        conn.execute(
            "UPDATE event_log SET send_state = 'failed', detail = ?, updated_at = ? WHERE id = ?",
            (detail, _now(), event_log_id),
        )
    else:
        conn.execute(
            "UPDATE event_log SET send_state = 'failed', attempts = ?, detail = ?, updated_at = ? "
            "WHERE id = ?",
            (attempts, detail, _now(), event_log_id),
        )
    conn.commit()


def requeue_pending_on_start(settings: Settings | None = None) -> int:
    """Re-load any still-``pending`` outbox row back onto the queue.

    Called at process startup (app.main lifespan) so a pending Slack send
    that was interrupted by a restart is retried instead of silently lost.
    Returns the number of rows requeued.
    """

    settings = settings or get_settings()
    conn = get_connection(settings.db_path)
    init_db(conn)
    try:
        rows = conn.execute(
            "SELECT id FROM event_log WHERE kind = ? AND send_state = 'pending'", (NOTIFY_KIND,)
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        _queue.put_nowait(NotifyItem(event_log_id=int(row["id"])))
    return len(rows)


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------
def _ensure_worker_started(settings: Settings) -> None:
    global _worker_thread
    with _worker_lock:
        if _worker_thread is None or not _worker_thread.is_alive():
            _stop_event.clear()
            _worker_thread = threading.Thread(
                target=_worker_loop, args=(settings,), name="notify-queue", daemon=True
            )
            _worker_thread.start()


def start_worker(settings: Settings | None = None) -> threading.Thread:
    """Start the notify worker and requeue any pending outbox rows (idempotent).

    ``requeue_pending_on_start`` is a best-effort convenience, not a
    precondition for the worker to run — a transient DB issue at the exact
    moment of process startup (e.g. another connection briefly holding the
    file, see app.db.get_connection) must never abort ``app.main``'s
    ``lifespan`` and take down the whole app. Same fail-soft contract as
    ``app.sweeper``'s background loop: log a warning and continue: any
    outbox rows that could not be requeued here stay durably ``pending`` in
    ``event_log`` and are simply picked up on the *next* process start (or
    whenever something else re-enqueues that session).
    """

    settings = settings or get_settings()
    try:
        requeue_pending_on_start(settings)
    except Exception:
        logger.warning(
            "Notify queue: requeue_pending_on_start failed at startup — "
            "continuing without requeue (worker still starts)",
            exc_info=True,
        )
    _ensure_worker_started(settings)
    assert _worker_thread is not None
    return _worker_thread


def stop_worker(timeout: float = 5.0) -> None:
    """Signal the notify worker to stop and join it (used at app shutdown)."""

    _stop_event.set()
    thread = _worker_thread
    if thread is not None:
        thread.join(timeout=timeout)


def _worker_loop(settings: Settings) -> None:
    while not _stop_event.is_set():
        try:
            item = _queue.get(timeout=1.0)
        except queue.Empty:
            continue
        try:
            conn = get_connection(settings.db_path)
            init_db(conn)
            try:
                state = process_one(conn, settings, item.event_log_id)
            finally:
                conn.close()
            if state == "pending":
                _schedule_retry(item, settings)
        except Exception:
            logger.exception(
                "Notify queue: unhandled error processing event_log_id=%s", item.event_log_id
            )
        finally:
            _queue.task_done()


def _schedule_retry(item: NotifyItem, settings: Settings) -> None:
    """Re-enqueue a retryable item after a linear backoff (base * attempts)."""

    conn = get_connection(settings.db_path)
    init_db(conn)
    try:
        row = conn.execute(
            "SELECT attempts FROM event_log WHERE id = ?", (item.event_log_id,)
        ).fetchone()
    finally:
        conn.close()
    attempts = int(row["attempts"]) if row is not None else 1
    delay = settings.notify_retry_base * max(attempts, 1)

    timer = threading.Timer(delay, _requeue_if_not_stopped, args=(item,))
    timer.daemon = True
    timer.start()


def _requeue_if_not_stopped(item: NotifyItem) -> None:
    if not _stop_event.is_set():
        _queue.put_nowait(item)
