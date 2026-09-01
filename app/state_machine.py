"""5-state CAS state machine for the MR review pipeline.

Ground truth: docs/mr-review-pipeline.html §③ (상태 기계). Every transition
is a single atomic CAS statement owned by the middleware:

    UPDATE review_session SET status=? WHERE id=? AND status=?

``rowcount == 0`` means the transition was rejected — this is not
check-then-act, the single SQL statement *is* the lock.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

# ---------------------------------------------------------------------------
# States (§③ legend)
# ---------------------------------------------------------------------------
REVIEWING = "reviewing"
MERGING = "merging"
REVISING = "revising"
MANUAL = "manual"
MERGED = "merged"

ALL_STATES = frozenset({REVIEWING, MERGING, REVISING, MANUAL, MERGED})

# ---------------------------------------------------------------------------
# Allowed transition edges, per §③ state diagram.
#
#   reviewing -> merging   : 승인 클릭 (CAS, 중복 클릭은 rowcount=0으로 거부)
#   reviewing -> revising  : 의견 제출 (즉시 동결)
#   reviewing -> merged    : 외부 머지 웹훅 (action=merge)
#   reviewing -> manual    : 외부 닫힘 웹훅(action=close) 또는 가드(a)(b) 사전 거부
#   merging   -> merged    : merge poll 성공 또는 외부 머지 웹훅
#   merging   -> manual    : poll N회 실패/차단, merge poll 중 사람 push 수신,
#                            또는 외부 닫힘 웹훅
#   revising  -> reviewing : revise 성공 (새 SHA 토큰으로 버튼 복원)
#   revising  -> manual    : 사람 push 수신, revising_since 타임아웃,
#                            revise_attempts>=2, 또는 외부 닫힘 웹훅
#   revising  -> merged    : 외부 머지 웹훅
#   manual    -> merged    : 사람 수정·머지
#   manual    -> reviewing : 운영자 재개, 또는 (P.2 폴러) external_close로 manual이 된
#                            세션의 MR이 다시 opened로 관측된 경우("mr_reopened") — 단,
#                            guard_reject/human_push/revise 실패 등 다른 사유로 manual이
#                            된 세션은 대상이 아님(무한 알림 사고 방지, app/gitlab_poller.py)
#
# `merged` is terminal — no outgoing edges.
# ---------------------------------------------------------------------------
ALLOWED_TRANSITIONS: dict[tuple[str, str], frozenset[str]] = {
    (REVIEWING, MERGING): frozenset({"approve"}),
    (REVIEWING, REVISING): frozenset({"opinion"}),
    (REVIEWING, MERGED): frozenset({"external_merge"}),
    (REVIEWING, MANUAL): frozenset({"external_close", "guard_reject"}),
    (MERGING, MERGED): frozenset({"merge_poll_success", "external_merge"}),
    (MERGING, MANUAL): frozenset(
        {"merge_poll_failed", "push_during_merge", "external_close"}
    ),
    (REVISING, REVIEWING): frozenset({"revise_success"}),
    (REVISING, MANUAL): frozenset(
        {"human_push", "revise_timeout", "revise_attempts_exceeded", "external_close"}
    ),
    (REVISING, MERGED): frozenset({"external_merge"}),
    (MANUAL, MERGED): frozenset({"human_merge"}),
    # operator_resume: reserved — 프로덕션 호출부 미구현(운영자 재개 백로그), 설계 §③ 대응.
    # mr_reopened: app/gitlab_poller.py가 호출 — 세션의 가장 최근 event_log.kind가
    # "external_close"인 경우에만 사용(그 외 사유로 manual이 된 세션은 절대 이 엣지를 타지
    # 않음 — 되살리면 매 주기 재알림이 반복되는 사고가 되므로 가드가 poller 쪽에 있음).
    (MANUAL, REVIEWING): frozenset({"operator_resume", "mr_reopened"}),
}


class InvalidTransitionError(ValueError):
    """Raised when a (from_status, to_status) pair is not a defined edge."""


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def cas_transition(
    conn: sqlite3.Connection,
    session_id: int,
    from_status: str,
    to_status: str,
    reason: str | None = None,
    detail: str | None = None,
) -> bool:
    """Attempt an atomic CAS transition of review_session.status.

    Executes ``UPDATE review_session SET status=?, ... WHERE id=? AND
    status=?``. Returns True if the row was updated (transition accepted),
    False if rowcount was 0 (transition rejected — someone else already
    moved the state, e.g. duplicate approve click or a racing external
    merge/close webhook per §③ 651/§⑤ dual-write note).

    Raises InvalidTransitionError if (from_status, to_status) is not one of
    the edges defined in ALLOWED_TRANSITIONS, or if ``reason`` is given but
    not a recognized reason for that edge.

    merging_since / revising_since are set on entry to their respective
    state and cleared on exit, per §④ column notes.
    """
    edge = (from_status, to_status)
    if edge not in ALLOWED_TRANSITIONS:
        raise InvalidTransitionError(f"transition {from_status!r} -> {to_status!r} is not allowed")
    if reason is not None and reason not in ALLOWED_TRANSITIONS[edge]:
        raise InvalidTransitionError(
            f"reason {reason!r} is not valid for transition {from_status!r} -> {to_status!r}"
        )

    now = _now()
    set_clauses = ["status = ?", "updated_at = ?"]
    params: list[object] = [to_status, now]

    if to_status == MERGING:
        set_clauses.append("merging_since = ?")
        params.append(now)
    elif from_status == MERGING:
        set_clauses.append("merging_since = NULL")

    if to_status == REVISING:
        set_clauses.append("revising_since = ?")
        params.append(now)
    elif from_status == REVISING:
        set_clauses.append("revising_since = NULL")

    sql = f"UPDATE review_session SET {', '.join(set_clauses)} WHERE id = ? AND status = ?"
    params.extend([session_id, from_status])

    cur = conn.execute(sql, params)
    accepted = cur.rowcount == 1

    if accepted:
        conn.execute(
            "INSERT INTO event_log (session_id, kind, detail) VALUES (?, ?, ?)",
            (session_id, reason or f"{from_status}->{to_status}", detail),
        )
    conn.commit()
    return accepted


def record_revise_failure(conn: sqlite3.Connection, session_id: int, detail: str | None = None) -> int:
    """Increment revise_attempts and log a `kind=failed` event.

    Per §③: "revise 실패 → revising 상태 유지 ... revise_attempts는
    kind=failed 수신 시 +1". Returns the new revise_attempts value.
    """
    conn.execute(
        "UPDATE review_session SET revise_attempts = revise_attempts + 1, updated_at = ? WHERE id = ?",
        (_now(), session_id),
    )
    conn.execute(
        "INSERT INTO event_log (session_id, kind, detail) VALUES (?, 'failed', ?)",
        (session_id, detail),
    )
    conn.commit()
    row = conn.execute(
        "SELECT revise_attempts FROM review_session WHERE id = ?", (session_id,)
    ).fetchone()
    return int(row["revise_attempts"])


def advance_round_and_reset_attempts(conn: sqlite3.Connection, session_id: int) -> int:
    """Increment session.round and reset revise_attempts to 0 (round success).

    Per §④: "round: revise 반복 횟수 (0부터)" and §③ "revise_attempts는 ...
    round+1(라운드 성공) 시 0 리셋". Returns the new round value.
    """
    conn.execute(
        "UPDATE review_session SET round = round + 1, revise_attempts = 0, updated_at = ? WHERE id = ?",
        (_now(), session_id),
    )
    conn.commit()
    row = conn.execute("SELECT round FROM review_session WHERE id = ?", (session_id,)).fetchone()
    return int(row["round"])
