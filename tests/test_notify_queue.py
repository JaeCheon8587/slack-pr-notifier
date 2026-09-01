"""Tests for the P6 Slack notify outbox (app/notify_queue.py).

Drives ``process_one`` directly — the synchronous per-item unit shared by the
real worker thread and these tests (mirrors the pattern in
tests/test_revise_executor.py) — with a fake Slack client factory so no
network call is made. The real worker thread/timer machinery is never
started in these tests (``_ensure_worker_started`` is monkeypatched to a
no-op where relevant).
"""

from __future__ import annotations

import json

import pytest

import app.notify_queue as notify_queue
from app.config import get_settings
from app.db import get_connection, init_db


def configure(monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "slack_bot_token", "xoxb-test")
    monkeypatch.setattr(settings, "notify_retry_base", 30)
    monkeypatch.setattr(settings, "notify_max_attempts", 3)
    return settings


@pytest.fixture(autouse=True)
def _drain_global_queue():
    """The notify queue is process-global — keep tests hermetic."""

    while not notify_queue._queue.empty():
        notify_queue._queue.get_nowait()
    yield
    while not notify_queue._queue.empty():
        notify_queue._queue.get_nowait()


def make_conn(settings):
    conn = get_connection(settings.db_path)
    init_db(conn)
    return conn


def seed_session(settings) -> int:
    conn = make_conn(settings)
    conn.execute(
        "INSERT INTO review_session (project_id, mr_iid, mr_sha, repo_slug) VALUES (?, ?, ?, ?)",
        ("1", 1, "sha1", "group/project"),
    )
    conn.commit()
    session_id = conn.execute("SELECT id FROM review_session").fetchone()["id"]
    conn.close()
    return session_id


def insert_pending_row(
    settings, session_id: int, *, method: str = "withdraw_buttons", kwargs: dict | None = None, attempts: int = 0
) -> int:
    kwargs = kwargs or {"channel": "C123", "message_ts": "111.222", "header_text": "hdr", "reason": "이유"}
    detail = json.dumps({"method": method, "kwargs": kwargs})
    conn = make_conn(settings)
    cur = conn.execute(
        "INSERT INTO event_log (session_id, kind, detail, send_state, attempts) "
        "VALUES (?, 'notify', ?, 'pending', ?)",
        (session_id, detail, attempts),
    )
    conn.commit()
    event_log_id = int(cur.lastrowid)
    conn.close()
    return event_log_id


def event_log_row(settings, event_log_id: int):
    conn = get_connection(settings.db_path)
    row = conn.execute("SELECT * FROM event_log WHERE id = ?", (event_log_id,)).fetchone()
    conn.close()
    return row


class FakeSlackClientOk:
    def __init__(self, token: str) -> None:
        self.token = token

    async def withdraw_buttons(self, channel, message_ts, header_text, reason) -> None:
        return None


class FakeSlackClientFails:
    def __init__(self, token: str) -> None:
        self.token = token

    async def withdraw_buttons(self, channel, message_ts, header_text, reason) -> None:
        raise RuntimeError("slack API down")


# ---------------------------------------------------------------------------
# success -> sent
# ---------------------------------------------------------------------------
def test_process_one_success_marks_sent(monkeypatch, tmp_path):
    settings = configure(monkeypatch, tmp_path)
    session_id = seed_session(settings)
    event_log_id = insert_pending_row(settings, session_id)

    conn = make_conn(settings)
    state = notify_queue.process_one(
        conn, settings, event_log_id, slack_client_factory=FakeSlackClientOk
    )
    conn.close()

    assert state == "sent"
    row = event_log_row(settings, event_log_id)
    assert row["send_state"] == "sent"
    assert row["attempts"] == 0


# ---------------------------------------------------------------------------
# failure -> attempts increments, stays pending (retryable)
# ---------------------------------------------------------------------------
def test_process_one_failure_increments_attempts_and_stays_pending(monkeypatch, tmp_path):
    settings = configure(monkeypatch, tmp_path)
    session_id = seed_session(settings)
    event_log_id = insert_pending_row(settings, session_id)

    conn = make_conn(settings)
    state = notify_queue.process_one(
        conn, settings, event_log_id, slack_client_factory=FakeSlackClientFails
    )
    conn.close()

    assert state == "pending"
    row = event_log_row(settings, event_log_id)
    assert row["send_state"] == "pending"
    assert row["attempts"] == 1


# ---------------------------------------------------------------------------
# repeated failure until notify_max_attempts -> failed confirmed
# ---------------------------------------------------------------------------
def test_process_one_reaches_max_attempts_marks_failed(monkeypatch, tmp_path):
    settings = configure(monkeypatch, tmp_path)  # notify_max_attempts = 3
    session_id = seed_session(settings)
    event_log_id = insert_pending_row(settings, session_id)

    conn = make_conn(settings)
    state = None
    for _ in range(settings.notify_max_attempts):
        state = notify_queue.process_one(
            conn, settings, event_log_id, slack_client_factory=FakeSlackClientFails
        )
    conn.close()

    assert state == "failed"
    row = event_log_row(settings, event_log_id)
    assert row["send_state"] == "failed"
    assert row["attempts"] == settings.notify_max_attempts


# ---------------------------------------------------------------------------
# a resolved row (sent/failed) is a no-op on re-processing
# ---------------------------------------------------------------------------
def test_process_one_is_a_noop_for_already_resolved_row(monkeypatch, tmp_path):
    settings = configure(monkeypatch, tmp_path)
    session_id = seed_session(settings)
    event_log_id = insert_pending_row(settings, session_id)

    conn = make_conn(settings)
    notify_queue.process_one(conn, settings, event_log_id, slack_client_factory=FakeSlackClientOk)
    # Second call: row is already `sent`; a fails-factory must never be invoked.
    state = notify_queue.process_one(
        conn, settings, event_log_id, slack_client_factory=FakeSlackClientFails
    )
    conn.close()

    assert state == "sent"


# ---------------------------------------------------------------------------
# missing SLACK_BOT_TOKEN -> failed immediately (fail-closed, no crash)
# ---------------------------------------------------------------------------
def test_process_one_without_slack_token_marks_failed(monkeypatch, tmp_path):
    settings = configure(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "slack_bot_token", None)
    session_id = seed_session(settings)
    event_log_id = insert_pending_row(settings, session_id)

    conn = make_conn(settings)
    state = notify_queue.process_one(conn, settings, event_log_id)
    conn.close()

    assert state == "failed"
    assert event_log_row(settings, event_log_id)["send_state"] == "failed"


# ---------------------------------------------------------------------------
# enqueue: non-blocking, persists a pending row, pushes onto the queue.
# ---------------------------------------------------------------------------
def test_enqueue_persists_pending_row_and_queues_item(monkeypatch, tmp_path):
    settings = configure(monkeypatch, tmp_path)
    monkeypatch.setattr(notify_queue, "_ensure_worker_started", lambda s: None)
    session_id = seed_session(settings)

    conn = make_conn(settings)
    event_log_id = notify_queue.enqueue(
        conn,
        settings,
        session_id=session_id,
        method="withdraw_buttons",
        kwargs={"channel": "C1", "message_ts": "1.1", "header_text": "h", "reason": "r"},
    )
    conn.close()

    row = event_log_row(settings, event_log_id)
    assert row["send_state"] == "pending"
    assert row["kind"] == "notify"

    assert not notify_queue._queue.empty()
    item = notify_queue._queue.get_nowait()
    assert item.event_log_id == event_log_id


# ---------------------------------------------------------------------------
# process restart: a pending row (not currently in the in-process queue,
# simulating a fresh process) is requeued by requeue_pending_on_start.
# ---------------------------------------------------------------------------
def test_requeue_pending_on_start_reloads_pending_rows(monkeypatch, tmp_path):
    settings = configure(monkeypatch, tmp_path)
    session_id = seed_session(settings)
    pending_id = insert_pending_row(settings, session_id)
    sent_id = insert_pending_row(settings, session_id)

    conn = make_conn(settings)
    notify_queue.process_one(conn, settings, sent_id, slack_client_factory=FakeSlackClientOk)
    conn.close()
    assert event_log_row(settings, sent_id)["send_state"] == "sent"

    assert notify_queue._queue.empty()  # simulating a fresh process: nothing queued yet

    count = notify_queue.requeue_pending_on_start(settings)

    assert count == 1  # only the still-pending row, not the already-sent one
    item = notify_queue._queue.get_nowait()
    assert item.event_log_id == pending_id


# ---------------------------------------------------------------------------
# start_worker must never propagate a requeue_pending_on_start failure — the
# worker thread still starts (fail-soft, same contract as app.sweeper's
# background loop; see app.notify_queue.start_worker's docstring).
# ---------------------------------------------------------------------------
def test_start_worker_survives_requeue_pending_on_start_exception(monkeypatch, tmp_path):
    settings = configure(monkeypatch, tmp_path)
    called = {"count": 0}

    def boom(_settings=None):
        called["count"] += 1
        raise RuntimeError("requeue exploded")

    monkeypatch.setattr(notify_queue, "requeue_pending_on_start", boom)

    try:
        thread = notify_queue.start_worker(settings)  # must not raise
        assert called["count"] == 1
        assert thread.is_alive()
    finally:
        notify_queue.stop_worker()
