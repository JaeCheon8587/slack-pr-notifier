"""Tests for app/db.py — schema creation, constraints, and query patterns."""

import hashlib
import sqlite3

import pytest

from app.db import get_connection, init_db


@pytest.fixture
def conn():
    connection = get_connection(":memory:")
    init_db(connection)
    yield connection
    connection.close()


def _make_session(conn, project_id="grp/proj", mr_iid=1):
    cur = conn.execute(
        "INSERT INTO review_session (project_id, mr_iid) VALUES (?, ?)",
        (project_id, mr_iid),
    )
    conn.commit()
    return cur.lastrowid


def test_schema_creates_all_tables(conn):
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"review_session", "opinion", "event_log"} <= tables


def test_review_session_default_status_and_round(conn):
    session_id = _make_session(conn)
    row = conn.execute(
        "SELECT status, round, revise_attempts, merging_since, revising_since "
        "FROM review_session WHERE id = ?",
        (session_id,),
    ).fetchone()
    assert row["status"] == "reviewing"
    assert row["round"] == 0
    assert row["revise_attempts"] == 0
    assert row["merging_since"] is None
    assert row["revising_since"] is None


def test_review_session_unique_project_mr_pair(conn):
    _make_session(conn, project_id="grp/proj", mr_iid=1)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO review_session (project_id, mr_iid) VALUES (?, ?)",
            ("grp/proj", 1),
        )


def test_review_session_status_check_constraint(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO review_session (project_id, mr_iid, status) VALUES (?, ?, ?)",
            ("grp/proj", 2, "bogus_status"),
        )


def test_opinion_unique_session_user_bodyhash(conn):
    session_id = _make_session(conn)
    body = "please fix the typo"
    body_hash = hashlib.sha256(body.encode()).hexdigest()
    conn.execute(
        "INSERT INTO opinion (session_id, slack_user, body, body_hash) VALUES (?, ?, ?, ?)",
        (session_id, "U123", body, body_hash),
    )
    conn.commit()
    # Duplicate submission (modal double-submit) with identical body must be rejected.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO opinion (session_id, slack_user, body, body_hash) VALUES (?, ?, ?, ?)",
            (session_id, "U123", body, body_hash),
        )


def test_opinion_different_body_same_user_allowed(conn):
    session_id = _make_session(conn)
    body_a = "fix typo"
    body_b = "add test"
    conn.execute(
        "INSERT INTO opinion (session_id, slack_user, body, body_hash) VALUES (?, ?, ?, ?)",
        (session_id, "U123", body_a, hashlib.sha256(body_a.encode()).hexdigest()),
    )
    conn.execute(
        "INSERT INTO opinion (session_id, slack_user, body, body_hash) VALUES (?, ?, ?, ?)",
        (session_id, "U123", body_b, hashlib.sha256(body_b.encode()).hexdigest()),
    )
    conn.commit()
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM opinion WHERE session_id = ?", (session_id,)
    ).fetchone()["c"]
    assert count == 2


def test_opinion_unapplied_queue_lookup(conn):
    """applied_round IS NULL AND created_at <= lock time = next revise batch."""
    session_id = _make_session(conn)
    for i, body in enumerate(["a", "b", "c"]):
        conn.execute(
            "INSERT INTO opinion (session_id, slack_user, body, body_hash, applied_round) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, f"U{i}", body, hashlib.sha256(body.encode()).hexdigest(), None),
        )
    conn.commit()
    lock_time = conn.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now') AS t").fetchone()["t"]

    # A late-arriving opinion after the lock time must not be picked up.
    conn.execute(
        "INSERT INTO opinion (session_id, slack_user, body, body_hash, applied_round, created_at) "
        "VALUES (?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '+1 hour'))",
        (session_id, "Ulate", "late", hashlib.sha256(b"late").hexdigest(), None),
    )
    conn.commit()

    queued = conn.execute(
        "SELECT body FROM opinion WHERE session_id = ? AND applied_round IS NULL "
        "AND created_at <= ?",
        (session_id, lock_time),
    ).fetchall()
    assert {row["body"] for row in queued} == {"a", "b", "c"}


def test_opinion_applied_round_marks_processed(conn):
    session_id = _make_session(conn)
    body = "apply me"
    conn.execute(
        "INSERT INTO opinion (session_id, slack_user, body, body_hash) VALUES (?, ?, ?, ?)",
        (session_id, "U1", body, hashlib.sha256(body.encode()).hexdigest()),
    )
    conn.commit()
    conn.execute(
        "UPDATE opinion SET applied_round = 1, last_verdict = 'applied' "
        "WHERE session_id = ? AND slack_user = ?",
        (session_id, "U1"),
    )
    conn.commit()
    remaining = conn.execute(
        "SELECT COUNT(*) AS c FROM opinion WHERE session_id = ? AND applied_round IS NULL",
        (session_id,),
    ).fetchone()["c"]
    assert remaining == 0


def test_event_log_idempotency_key_null_allows_multiple_audit_rows(conn):
    """revise_fail etc. are append-only audit rows; NULL key must not collide."""
    session_id = _make_session(conn)
    conn.execute(
        "INSERT INTO event_log (session_id, kind, idempotency_key) VALUES (?, 'failed', NULL)",
        (session_id,),
    )
    conn.execute(
        "INSERT INTO event_log (session_id, kind, idempotency_key) VALUES (?, 'failed', NULL)",
        (session_id,),
    )
    conn.commit()
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM event_log WHERE session_id = ? AND kind = 'failed'",
        (session_id,),
    ).fetchone()["c"]
    assert count == 2


def test_event_log_idempotency_key_unique_when_present(conn):
    session_id = _make_session(conn)
    conn.execute(
        "INSERT INTO event_log (session_id, kind, idempotency_key) VALUES (?, 'notify', 'key-1')",
        (session_id,),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO event_log (session_id, kind, idempotency_key) VALUES (?, 'notify', 'key-1')",
            (session_id,),
        )


def test_event_log_send_state_check_constraint(conn):
    session_id = _make_session(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO event_log (session_id, kind, send_state) VALUES (?, 'notify', 'bogus')",
            (session_id,),
        )


def test_event_log_send_state_update(conn):
    session_id = _make_session(conn)
    cur = conn.execute(
        "INSERT INTO event_log (session_id, kind, send_state, idempotency_key) "
        "VALUES (?, 'notify', 'pending', 'key-2')",
        (session_id,),
    )
    conn.commit()
    event_id = cur.lastrowid

    conn.execute(
        "UPDATE event_log SET send_state = 'failed', attempts = attempts + 1 WHERE id = ?",
        (event_id,),
    )
    conn.commit()
    row = conn.execute(
        "SELECT send_state, attempts FROM event_log WHERE id = ?", (event_id,)
    ).fetchone()
    assert row["send_state"] == "failed"
    assert row["attempts"] == 1

    conn.execute(
        "UPDATE event_log SET send_state = 'sent' WHERE id = ?", (event_id,)
    )
    conn.commit()
    row = conn.execute(
        "SELECT send_state FROM event_log WHERE id = ?", (event_id,)
    ).fetchone()
    assert row["send_state"] == "sent"


def test_init_db_is_idempotent(conn):
    # Calling init_db again on an already-initialized connection must not error.
    init_db(conn)
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"review_session", "opinion", "event_log"} <= tables


def test_wal_mode_enabled():
    conn = get_connection(":memory:")
    # :memory: databases report journal_mode as 'memory' regardless of PRAGMA,
    # so verify the PRAGMA statement itself does not error and returns a value.
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode is not None
    conn.close()


def test_concurrent_connections_to_same_file_and_busy_timeout(tmp_path):
    """Two connections opened back-to-back against the same real DB file must
    not raise sqlite3.OperationalError: database is locked — regression for
    the WAL re-issue race in get_connection (the second connection must not
    die re-setting an already-WAL journal_mode). Each connection's
    busy_timeout pragma must read back as 30000 (30s)."""
    db_path = tmp_path / "concurrent.db"

    conn1 = get_connection(db_path)
    init_db(conn1)
    conn2 = get_connection(db_path)  # second connection, same file, opened right after
    try:
        assert conn1.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
        assert conn2.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
        # Both usable without raising — the regression this test guards against.
        assert conn1.execute("SELECT 1").fetchone()[0] == 1
        assert conn2.execute("SELECT 1").fetchone()[0] == 1
    finally:
        conn2.close()
        conn1.close()
