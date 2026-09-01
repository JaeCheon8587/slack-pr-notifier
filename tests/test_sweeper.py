"""Tests for the P6 timeout sweeper (app/sweeper.py).

Drives ``sweep_once(conn, now, settings)`` directly — a pure function over an
explicit ``now``, so no real sleeps are needed. The notify-outbox side effect
(app.notify_queue.enqueue) is exercised for its DB write only; the worker
thread is never started (monkeypatched to a no-op, matching the pattern in
tests/test_revise_executor.py).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import app.notify_queue as notify_queue
import app.sweeper as sweeper
from app.config import get_settings
from app.db import get_connection, init_db
from app.state_machine import MANUAL, MERGED, MERGING, REVIEWING, REVISING, cas_transition

PROJECT_ID = "77"
REPO_SLUG = "group/project"


@pytest.fixture(autouse=True)
def _no_real_notify_worker(monkeypatch):
    """Never spawn the real notify worker thread (network) from these tests."""

    monkeypatch.setattr(notify_queue, "_ensure_worker_started", lambda settings: None)


def configure(monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "merging_timeout", 300)
    monkeypatch.setattr(settings, "revising_timeout", 1200)
    monkeypatch.setattr(settings, "slack_bot_token", "xoxb-test")
    return settings


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def seed_session(
    settings,
    *,
    status: str,
    since_column: str | None = None,
    since_seconds_ago: float = 0.0,
    slack_channel: str | None = "C123",
    slack_ts: str | None = "111.222",
    mr_iid: int = 1,
) -> int:
    conn = get_connection(settings.db_path)
    init_db(conn)
    conn.execute(
        "INSERT INTO review_session (project_id, mr_iid, mr_sha, repo_slug, slack_channel, slack_ts) "
        "VALUES (?, ?, 'sha1', ?, ?, ?)",
        (PROJECT_ID, mr_iid, REPO_SLUG, slack_channel, slack_ts),
    )
    conn.commit()
    session_id = conn.execute(
        "SELECT id FROM review_session WHERE project_id = ? AND mr_iid = ?", (PROJECT_ID, mr_iid)
    ).fetchone()["id"]

    # Reach `status` through real CAS edges so merging_since/revising_since is
    # set exactly as production would set it, then optionally backdate it.
    if status == MERGING:
        assert cas_transition(conn, session_id, REVIEWING, MERGING, reason="approve")
    elif status == REVISING:
        assert cas_transition(conn, session_id, REVIEWING, REVISING, reason="opinion")
    elif status == MANUAL:
        assert cas_transition(conn, session_id, REVIEWING, MANUAL, reason="external_close")
    elif status == MERGED:
        assert cas_transition(conn, session_id, REVIEWING, MERGED, reason="external_merge")
    elif status == REVIEWING:
        pass
    else:
        raise AssertionError(f"unsupported seed status: {status}")

    if since_column is not None:
        backdated = _iso(datetime.now(UTC) - timedelta(seconds=since_seconds_ago))
        conn.execute(f"UPDATE review_session SET {since_column} = ? WHERE id = ?", (backdated, session_id))
        conn.commit()

    conn.close()
    return session_id


def session_row(settings, session_id: int):
    conn = get_connection(settings.db_path)
    row = conn.execute("SELECT * FROM review_session WHERE id = ?", (session_id,)).fetchone()
    conn.close()
    return row


def event_rows(settings, session_id: int):
    conn = get_connection(settings.db_path)
    rows = conn.execute(
        "SELECT * FROM event_log WHERE session_id = ? ORDER BY id", (session_id,)
    ).fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# (a) merging timeout
# ---------------------------------------------------------------------------
def test_merging_under_threshold_is_untouched(monkeypatch, tmp_path):
    settings = configure(monkeypatch, tmp_path)
    session_id = seed_session(settings, status=MERGING, since_column="merging_since", since_seconds_ago=10)

    conn = get_connection(settings.db_path)
    counts = sweeper.sweep_once(conn, datetime.now(UTC), settings)
    conn.close()

    assert counts == {"merging_timeout": 0, "revising_timeout": 0}
    assert session_row(settings, session_id)["status"] == MERGING


def test_merging_over_threshold_is_cas_to_manual_with_reason(monkeypatch, tmp_path):
    settings = configure(monkeypatch, tmp_path)
    session_id = seed_session(
        settings, status=MERGING, since_column="merging_since", since_seconds_ago=settings.merging_timeout + 1
    )

    conn = get_connection(settings.db_path)
    counts = sweeper.sweep_once(conn, datetime.now(UTC), settings)
    conn.close()

    assert counts == {"merging_timeout": 1, "revising_timeout": 0}
    row = session_row(settings, session_id)
    assert row["status"] == MANUAL
    assert row["merging_since"] is None

    kinds_and_reasons = [(e["kind"], e["detail"]) for e in event_rows(settings, session_id)]
    assert any(kind == "merge_poll_failed" for kind, _detail in kinds_and_reasons)
    # A notify-outbox row should have been enqueued for the Slack reason update.
    assert any(kind == "notify" for kind, _detail in kinds_and_reasons)


# ---------------------------------------------------------------------------
# (b) revising timeout
# ---------------------------------------------------------------------------
def test_revising_under_threshold_is_untouched(monkeypatch, tmp_path):
    settings = configure(monkeypatch, tmp_path)
    session_id = seed_session(
        settings, status=REVISING, since_column="revising_since", since_seconds_ago=10
    )

    conn = get_connection(settings.db_path)
    counts = sweeper.sweep_once(conn, datetime.now(UTC), settings)
    conn.close()

    assert counts == {"merging_timeout": 0, "revising_timeout": 0}
    assert session_row(settings, session_id)["status"] == REVISING


def test_revising_over_threshold_is_cas_to_manual_with_reason(monkeypatch, tmp_path):
    settings = configure(monkeypatch, tmp_path)
    session_id = seed_session(
        settings, status=REVISING, since_column="revising_since", since_seconds_ago=settings.revising_timeout + 1
    )

    conn = get_connection(settings.db_path)
    counts = sweeper.sweep_once(conn, datetime.now(UTC), settings)
    conn.close()

    assert counts == {"merging_timeout": 0, "revising_timeout": 1}
    row = session_row(settings, session_id)
    assert row["status"] == MANUAL
    assert row["revising_since"] is None

    kinds = [e["kind"] for e in event_rows(settings, session_id)]
    assert "revise_timeout" in kinds
    assert "notify" in kinds


# ---------------------------------------------------------------------------
# merged/manual/reviewing sessions are ignored regardless of *_since age
# ---------------------------------------------------------------------------
def test_merged_and_manual_and_reviewing_sessions_are_ignored(monkeypatch, tmp_path):
    settings = configure(monkeypatch, tmp_path)
    merged_id = seed_session(settings, status=MERGED, mr_iid=2)
    manual_id = seed_session(settings, status=MANUAL, mr_iid=3)
    reviewing_id = seed_session(settings, status=REVIEWING, mr_iid=4)

    conn = get_connection(settings.db_path)
    counts = sweeper.sweep_once(conn, datetime.now(UTC) + timedelta(hours=1), settings)
    conn.close()

    assert counts == {"merging_timeout": 0, "revising_timeout": 0}
    assert session_row(settings, merged_id)["status"] == MERGED
    assert session_row(settings, manual_id)["status"] == MANUAL
    assert session_row(settings, reviewing_id)["status"] == REVIEWING


def test_no_slack_message_skips_notify_enqueue_but_still_transitions(monkeypatch, tmp_path):
    settings = configure(monkeypatch, tmp_path)
    session_id = seed_session(
        settings,
        status=MERGING,
        since_column="merging_since",
        since_seconds_ago=settings.merging_timeout + 1,
        slack_channel=None,
        slack_ts=None,
    )

    conn = get_connection(settings.db_path)
    counts = sweeper.sweep_once(conn, datetime.now(UTC), settings)
    conn.close()

    assert counts["merging_timeout"] == 1
    assert session_row(settings, session_id)["status"] == MANUAL
    kinds = [e["kind"] for e in event_rows(settings, session_id)]
    assert "notify" not in kinds
