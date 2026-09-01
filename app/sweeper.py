"""Timeout sweeper (P6) — recovers sessions stuck in `merging`/`revising`.

Ground truth: docs/mr-review-pipeline.html §⑦ ("스위퍼 임계 > 세션 상한 + 여유")
and §③ (state machine edges ``merging -> manual`` reason ``merge_poll_failed``,
``revising -> manual`` reason ``revise_timeout``).

Every timeout is enforced only in this module and only via
``app.state_machine.cas_transition`` — a racing external event (approve,
merge, close, revise success) that already moved the session out of
``merging``/``revising`` simply loses the CAS (rowcount 0) and the sweeper
moves on silently. ``sweep_once`` is a pure, synchronous function taking an
explicit ``now`` so it is fully unit-testable without real sleeps; the
background thread wrapper below is a thin periodic caller around it.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import UTC, datetime

from app.config import Settings, get_settings
from app.db import get_connection, init_db
from app.state_machine import MANUAL, MERGING, REVISING, cas_transition

logger = logging.getLogger("uvicorn.error")

_MERGING_TIMEOUT_REASON = "merge_poll_failed"
_REVISING_TIMEOUT_REASON = "revise_timeout"


def _now_str(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _threshold(now: datetime, timeout_seconds: int) -> str:
    """The ISO timestamp string below which a `*_since` column is stale.

    ``review_session.merging_since``/``revising_since`` are stored via
    ``strftime('%Y-%m-%dT%H:%M:%fZ', 'now')`` (see app/state_machine.py),
    which sorts identically to a plain lexicographic string comparison, so no
    parsing is needed on the SQL side.
    """

    from datetime import timedelta

    return _now_str(now - timedelta(seconds=timeout_seconds))


def sweep_once(conn: sqlite3.Connection, now: datetime, settings: Settings | None = None) -> dict[str, int]:
    """Run one sweep pass. Returns counts of sessions force-transitioned.

    (a) ``merging`` sessions whose ``merging_since`` is older than
        ``settings.merging_timeout`` -> CAS to ``manual`` (reason
        ``merge_poll_failed``).
    (b) ``revising`` sessions whose ``revising_since`` is older than
        ``settings.revising_timeout`` -> CAS to ``manual`` (reason
        ``revise_timeout``).

    Sessions in any other status (``reviewing``, ``manual``, ``merged``) are
    never selected by these queries and are therefore untouched.
    """

    settings = settings or get_settings()
    merging_swept = _sweep_stuck(
        conn,
        from_status=MERGING,
        since_column="merging_since",
        threshold=_threshold(now, settings.merging_timeout),
        reason=_MERGING_TIMEOUT_REASON,
        detail=f"merging_since timeout ({settings.merging_timeout}s)",
        header_text="⏱️ 머지 대기 시간 초과 — 수동 확인 필요",
        settings=settings,
    )
    revising_swept = _sweep_stuck(
        conn,
        from_status=REVISING,
        since_column="revising_since",
        threshold=_threshold(now, settings.revising_timeout),
        reason=_REVISING_TIMEOUT_REASON,
        detail=f"revising_since timeout ({settings.revising_timeout}s)",
        header_text="⏱️ 개선 작업 시간 초과 — 수동 확인 필요",
        settings=settings,
    )
    return {"merging_timeout": merging_swept, "revising_timeout": revising_swept}


def _sweep_stuck(
    conn: sqlite3.Connection,
    *,
    from_status: str,
    since_column: str,
    threshold: str,
    reason: str,
    detail: str,
    header_text: str,
    settings: Settings,
) -> int:
    rows = conn.execute(
        f"SELECT * FROM review_session WHERE status = ? AND {since_column} IS NOT NULL "
        f"AND {since_column} <= ?",
        (from_status, threshold),
    ).fetchall()

    swept = 0
    for row in rows:
        accepted = cas_transition(conn, row["id"], from_status, MANUAL, reason=reason, detail=detail)
        if not accepted:
            # Lost the race to some other transition already in flight — the
            # sweeper never overrides an already-resolved session.
            continue
        swept += 1
        _enqueue_manual_notice(conn, settings, row, header_text, reason)
    return swept


def _enqueue_manual_notice(
    conn: sqlite3.Connection, settings: Settings, session: sqlite3.Row, header_text: str, reason: str
) -> None:
    """Route the "사유 갱신" Slack update through the notify outbox so a
    transient Slack failure cannot silently drop the reason update (CHANGE
    SPEC [2]: 메인 메시지 갱신 must not be lost).
    """

    channel = session["slack_channel"]
    message_ts = session["slack_ts"]
    if not channel or not message_ts:
        logger.warning(
            "Sweeper: session %s has no Slack message to update (reason=%s)", session["id"], reason
        )
        return

    from app import notify_queue  # local import: avoids a hard import cycle at module load time

    summary = f"!{session['mr_iid']} ({session['repo_slug'] or session['project_id']})"
    notify_queue.enqueue(
        conn,
        settings,
        session_id=session["id"],
        method="withdraw_buttons",
        kwargs={
            "channel": channel,
            "message_ts": message_ts,
            "header_text": summary,
            "reason": header_text,
        },
    )


# ---------------------------------------------------------------------------
# Background thread wrapper
# ---------------------------------------------------------------------------
_sweeper_thread: threading.Thread | None = None
_sweeper_lock = threading.Lock()
_stop_event = threading.Event()


def start_sweeper(settings: Settings | None = None) -> threading.Thread:
    """Start the periodic sweeper as a daemon thread (idempotent)."""

    global _sweeper_thread
    settings = settings or get_settings()
    with _sweeper_lock:
        if _sweeper_thread is None or not _sweeper_thread.is_alive():
            _stop_event.clear()
            _sweeper_thread = threading.Thread(target=_sweeper_loop, args=(settings,), name="sweeper", daemon=True)
            _sweeper_thread.start()
        return _sweeper_thread


def stop_sweeper(timeout: float = 5.0) -> None:
    """Signal the sweeper loop to stop and join it (used at app shutdown)."""

    _stop_event.set()
    thread = _sweeper_thread
    if thread is not None:
        thread.join(timeout=timeout)


def _sweeper_loop(settings: Settings) -> None:
    while not _stop_event.is_set():
        try:
            conn = get_connection(settings.db_path)
            init_db(conn)
            try:
                sweep_once(conn, datetime.now(UTC), settings)
            finally:
                conn.close()
        except Exception:
            logger.exception("Sweeper: unhandled error during sweep_once")
        _stop_event.wait(settings.sweep_interval)
