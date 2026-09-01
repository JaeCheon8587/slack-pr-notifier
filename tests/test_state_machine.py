"""Tests for app/state_machine.py — CAS transitions per §③ state diagram."""

import pytest

from app.db import get_connection, init_db
from app.state_machine import (
    MANUAL,
    MERGED,
    MERGING,
    REVIEWING,
    REVISING,
    InvalidTransitionError,
    advance_round_and_reset_attempts,
    cas_transition,
    record_revise_failure,
)


@pytest.fixture
def conn():
    connection = get_connection(":memory:")
    init_db(connection)
    yield connection
    connection.close()


def _make_session(conn, status=REVIEWING, project_id="grp/proj", mr_iid=1):
    cur = conn.execute(
        "INSERT INTO review_session (project_id, mr_iid, status) VALUES (?, ?, ?)",
        (project_id, mr_iid, status),
    )
    conn.commit()
    return cur.lastrowid


def _status(conn, session_id):
    return conn.execute(
        "SELECT status FROM review_session WHERE id = ?", (session_id,)
    ).fetchone()["status"]


# ---------------------------------------------------------------------------
# All allowed edges succeed
# ---------------------------------------------------------------------------

ALL_ALLOWED_EDGES = [
    (REVIEWING, MERGING, "approve"),
    (REVIEWING, REVISING, "opinion"),
    (REVIEWING, MERGED, "external_merge"),
    (REVIEWING, MANUAL, "external_close"),
    (REVIEWING, MANUAL, "guard_reject"),
    (MERGING, MERGED, "merge_poll_success"),
    (MERGING, MERGED, "external_merge"),
    (MERGING, MANUAL, "merge_poll_failed"),
    (MERGING, MANUAL, "push_during_merge"),
    (MERGING, MANUAL, "external_close"),
    (REVISING, REVIEWING, "revise_success"),
    (REVISING, MANUAL, "human_push"),
    (REVISING, MANUAL, "revise_timeout"),
    (REVISING, MANUAL, "revise_attempts_exceeded"),
    (REVISING, MANUAL, "external_close"),
    (REVISING, MERGED, "external_merge"),
    (MANUAL, MERGED, "human_merge"),
    (MANUAL, REVIEWING, "operator_resume"),
]


@pytest.mark.parametrize("from_status,to_status,reason", ALL_ALLOWED_EDGES)
def test_allowed_transition_succeeds(conn, from_status, to_status, reason):
    session_id = _make_session(conn, status=from_status)
    accepted = cas_transition(conn, session_id, from_status, to_status, reason=reason)
    assert accepted is True
    assert _status(conn, session_id) == to_status


def test_allowed_transition_logs_event(conn):
    session_id = _make_session(conn, status=REVIEWING)
    cas_transition(conn, session_id, REVIEWING, MERGING, reason="approve", detail="U123 clicked approve")
    row = conn.execute(
        "SELECT kind, detail FROM event_log WHERE session_id = ? ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    assert row["kind"] == "approve"
    assert row["detail"] == "U123 clicked approve"


# ---------------------------------------------------------------------------
# Disallowed transitions raise
# ---------------------------------------------------------------------------

DISALLOWED_EDGES = [
    (REVIEWING, REVIEWING),
    (MERGED, REVIEWING),
    (MERGED, MANUAL),
    (MANUAL, REVISING),
    (MANUAL, MERGING),
    (MERGING, REVISING),
    (MERGING, REVIEWING),
    (REVISING, MERGING),
]


@pytest.mark.parametrize("from_status,to_status", DISALLOWED_EDGES)
def test_disallowed_transition_raises(conn, from_status, to_status):
    session_id = _make_session(conn, status=from_status)
    with pytest.raises(InvalidTransitionError):
        cas_transition(conn, session_id, from_status, to_status)
    # Status must remain unchanged after a rejected transition attempt.
    assert _status(conn, session_id) == from_status


def test_invalid_reason_for_valid_edge_raises(conn):
    session_id = _make_session(conn, status=REVIEWING)
    with pytest.raises(InvalidTransitionError):
        cas_transition(conn, session_id, REVIEWING, MERGING, reason="not_a_real_reason")


# ---------------------------------------------------------------------------
# CAS race: only the first caller with a matching from_status wins
# ---------------------------------------------------------------------------

def test_cas_race_second_call_rejected(conn):
    session_id = _make_session(conn, status=REVIEWING)
    first = cas_transition(conn, session_id, REVIEWING, MERGING, reason="approve")
    second = cas_transition(conn, session_id, REVIEWING, MERGING, reason="approve")
    assert first is True
    assert second is False
    assert _status(conn, session_id) == MERGING


def test_duplicate_approve_click_scenario(conn):
    """reviewing -> merging succeeds once; a retried approve click is rowcount=0."""
    session_id = _make_session(conn, status=REVIEWING)
    assert cas_transition(conn, session_id, REVIEWING, MERGING, reason="approve") is True
    # Simulated duplicate click retries the same from_status -> to_status CAS.
    retry = cas_transition(conn, session_id, REVIEWING, MERGING, reason="approve")
    assert retry is False


def test_external_merge_races_with_approve_first_wins(conn):
    """§③ 651: racing external merge/close webhook vs in-flight transition —
    whichever CAS succeeds first wins, the later one is rowcount=0."""
    session_id = _make_session(conn, status=REVIEWING)
    approve_ok = cas_transition(conn, session_id, REVIEWING, MERGING, reason="approve")
    external_merge_ok = cas_transition(conn, session_id, REVIEWING, MERGED, reason="external_merge")
    assert approve_ok is True
    assert external_merge_ok is False
    assert _status(conn, session_id) == MERGING


# ---------------------------------------------------------------------------
# merging_since / revising_since set & clear
# ---------------------------------------------------------------------------

def test_merging_since_set_on_entry_and_cleared_on_exit(conn):
    session_id = _make_session(conn, status=REVIEWING)
    cas_transition(conn, session_id, REVIEWING, MERGING, reason="approve")
    row = conn.execute(
        "SELECT merging_since FROM review_session WHERE id = ?", (session_id,)
    ).fetchone()
    assert row["merging_since"] is not None

    cas_transition(conn, session_id, MERGING, MERGED, reason="merge_poll_success")
    row = conn.execute(
        "SELECT merging_since FROM review_session WHERE id = ?", (session_id,)
    ).fetchone()
    assert row["merging_since"] is None


def test_revising_since_set_on_entry_and_cleared_on_exit(conn):
    session_id = _make_session(conn, status=REVIEWING)
    cas_transition(conn, session_id, REVIEWING, REVISING, reason="opinion")
    row = conn.execute(
        "SELECT revising_since FROM review_session WHERE id = ?", (session_id,)
    ).fetchone()
    assert row["revising_since"] is not None

    cas_transition(conn, session_id, REVISING, REVIEWING, reason="revise_success")
    row = conn.execute(
        "SELECT revising_since FROM review_session WHERE id = ?", (session_id,)
    ).fetchone()
    assert row["revising_since"] is None


# ---------------------------------------------------------------------------
# revise_attempts +1 on failed / reset to 0 on round advance
# ---------------------------------------------------------------------------

def test_record_revise_failure_increments_attempts(conn):
    session_id = _make_session(conn, status=REVISING)
    assert record_revise_failure(conn, session_id) == 1
    assert record_revise_failure(conn, session_id) == 2
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM event_log WHERE session_id = ? AND kind = 'failed'",
        (session_id,),
    ).fetchone()
    assert row["c"] == 2


def test_record_revise_failure_at_threshold_triggers_manual(conn):
    session_id = _make_session(conn, status=REVISING)
    record_revise_failure(conn, session_id)
    attempts = record_revise_failure(conn, session_id)
    assert attempts >= 2
    # §③: revise_attempts >= 2 -> manual
    accepted = cas_transition(
        conn, session_id, REVISING, MANUAL, reason="revise_attempts_exceeded"
    )
    assert accepted is True
    assert _status(conn, session_id) == MANUAL


def test_advance_round_resets_attempts(conn):
    session_id = _make_session(conn, status=REVISING)
    record_revise_failure(conn, session_id)
    assert conn.execute(
        "SELECT revise_attempts FROM review_session WHERE id = ?", (session_id,)
    ).fetchone()["revise_attempts"] == 1

    new_round = advance_round_and_reset_attempts(conn, session_id)
    assert new_round == 1
    row = conn.execute(
        "SELECT revise_attempts FROM review_session WHERE id = ?", (session_id,)
    ).fetchone()
    assert row["revise_attempts"] == 0
