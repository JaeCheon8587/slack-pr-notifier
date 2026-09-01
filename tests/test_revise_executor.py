"""Tests for the P4b revise executor body (app/revise_executor.py).

Drives ``process_one`` directly (the executor's synchronous per-item unit,
shared by the real worker thread and these tests — see the module docstring)
with a ``FakeRunner`` standing in for the P5 claude orchestrator, and
monkeypatches git/GitLab/Slack so no subprocess/network call is made.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

import app.revise_executor as revise_executor
from app.ai_runner import ReviseResult
from app.config import get_settings
from app.db import get_connection, init_db
from app.state_machine import MANUAL, REVIEWING, REVISING, cas_transition

PROJECT_ID = 42
IID = 1
REPO_SLUG = "group/project"
SHA = "abc123"
FAKE_WORKSPACE = Path("fake-workspace")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeRunner:
    """Stands in for the P5 AI orchestrator (StubRunner-shaped, but scriptable)."""

    def __init__(self, *, kind: str = "ok", unapplied_ids: set[int] | None = None, detail: str = ""):
        self.kind = kind
        self.unapplied_ids = unapplied_ids or set()
        self.detail = detail

    def run(
        self,
        workspace: Any,
        opinions: list[dict[str, Any]],
        session_ctx: dict[str, Any],
        timeout_seconds: int,
    ) -> ReviseResult:
        if self.kind == "failed":
            return ReviseResult(kind="failed", detail=self.detail or "fake runner failure")
        unapplied = [
            {"opinion_id": op["id"], "reason": "테스트 미반영 사유"}
            for op in opinions
            if op["id"] in self.unapplied_ids
        ]
        return ReviseResult(kind="ok", unapplied=unapplied, detail=self.detail)


class RaisingRunner:
    def run(self, workspace: Any, opinions: list[dict[str, Any]], session_ctx: dict[str, Any], timeout_seconds: int) -> ReviseResult:
        raise RuntimeError("boom")


class FakeGitLabClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def get_merge_request(self, project_id: Any, merge_request_iid: Any) -> dict[str, Any]:
        return {
            "source_branch": "feature/test",
            "target_branch": "main",
            "title": "Test MR",
            "web_url": "https://gitlab.example.com/group/project/-/merge_requests/1",
            "sha": "newsha000",
            "author": {"username": "author1"},
        }


def make_fake_slack_client(calls: list[dict[str, Any]]):
    class FakeSlackClient:
        def __init__(self, token: str) -> None:
            self.token = token

        async def update_revise_result(
            self,
            channel: str,
            message_ts: str,
            mr: dict[str, Any],
            token: str,
            *,
            round_number: int,
            unapplied: list[dict[str, Any]],
        ) -> None:
            calls.append(
                {
                    "kind": "revise_result",
                    "channel": channel,
                    "message_ts": message_ts,
                    "mr": mr,
                    "token": token,
                    "round_number": round_number,
                    "unapplied": unapplied,
                }
            )

        async def post_revise_result(
            self,
            channel: str,
            mr: dict[str, Any],
            token: str,
            *,
            round_number: int,
            unapplied: list[dict[str, Any]],
            summary: str | None = None,
            diff_stat: str | None = None,
            compare_url: str | None = None,
        ) -> dict[str, Any]:
            calls.append(
                {
                    "kind": "revise_result_post",
                    "channel": channel,
                    "mr": mr,
                    "token": token,
                    "round_number": round_number,
                    "unapplied": unapplied,
                    "summary": summary,
                    "diff_stat": diff_stat,
                    "compare_url": compare_url,
                }
            )
            return {"ts": "999.888", "channel": channel}

        async def withdraw_buttons(
            self, channel: str, message_ts: str, header_text: str, reason: str
        ) -> None:
            calls.append(
                {
                    "kind": "withdraw",
                    "channel": channel,
                    "message_ts": message_ts,
                    "header_text": header_text,
                    "reason": reason,
                }
            )

    return FakeSlackClient


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _drain_global_queue():
    """The executor's queue is process-global — keep tests hermetic."""

    while not revise_executor._queue.empty():
        revise_executor._queue.get_nowait()
    yield
    while not revise_executor._queue.empty():
        revise_executor._queue.get_nowait()


def configure(monkeypatch, tmp_path):  # type: ignore[no-untyped-def]
    settings = get_settings()
    monkeypatch.setattr(settings, "gitlab_url", "https://gitlab.example.com")
    monkeypatch.setattr(settings, "gitlab_token", "gitlab-test-pat")
    monkeypatch.setattr(settings, "slack_bot_token", "xoxb-test")
    monkeypatch.setattr(settings, "action_token_secret", "test-action-secret")
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "workspace_root", str(tmp_path / "workspaces"))
    monkeypatch.setattr(settings, "revise_queue_wait_limit", 600)
    monkeypatch.setattr(settings, "revise_wall_clock_seconds", 900)
    # Round HTML reports have their own delivery tests
    # (tests/test_report_delivery.py); keep these notify-wiring tests hermetic.
    monkeypatch.setattr(settings, "report_html_enabled", False)

    # Never touch git or the network — the git/GitLab boundary is exercised
    # separately in tests/test_git_workspace.py.
    monkeypatch.setattr(revise_executor.git_workspace, "ensure_workspace", lambda *a, **k: FAKE_WORKSPACE)
    monkeypatch.setattr(revise_executor.git_workspace, "checkout", lambda *a, **k: None)
    monkeypatch.setattr(revise_executor.git_workspace, "has_changes", lambda *a, **k: False)
    monkeypatch.setattr(revise_executor.git_workspace, "commit_all", lambda *a, **k: None)
    monkeypatch.setattr(revise_executor.git_workspace, "push", lambda *a, **k: None)
    monkeypatch.setattr(revise_executor.git_workspace, "current_sha", lambda *a, **k: "newsha000")
    monkeypatch.setattr(
        revise_executor.git_workspace,
        "diff_stat",
        lambda *a, **k: " 1 file changed, 2 insertions(+), 1 deletion(-)",
    )
    monkeypatch.setattr(revise_executor, "GitLabClient", FakeGitLabClient)
    # Prevent the real worker thread from ever spawning during unit tests —
    # retries are driven explicitly by re-invoking process_one below.
    monkeypatch.setattr(revise_executor, "_ensure_worker_started", lambda: None)
    return settings


def seed_revising_session(settings, bodies: list[str]) -> tuple[int, list[int]]:
    """Insert opinions then CAS reviewing->revising, matching production order
    (app.slack_actions inserts the opinion row before attempting the CAS lock),
    so every opinion's created_at <= the CAS-lock event_log timestamp."""

    conn = get_connection(settings.db_path)
    init_db(conn)
    conn.execute(
        "INSERT INTO review_session (project_id, mr_iid, mr_sha, repo_slug, slack_channel, slack_ts) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (str(PROJECT_ID), IID, SHA, REPO_SLUG, "C123", "111.222"),
    )
    conn.commit()
    session_id = conn.execute(
        "SELECT id FROM review_session WHERE project_id = ? AND mr_iid = ?", (str(PROJECT_ID), IID)
    ).fetchone()["id"]

    opinion_ids = []
    for index, body in enumerate(bodies):
        cur = conn.execute(
            "INSERT INTO opinion (session_id, slack_user, body, body_hash) VALUES (?, ?, ?, ?)",
            (session_id, f"U{index}", body, f"hash{index}"),
        )
        opinion_ids.append(cur.lastrowid)
    conn.commit()

    assert cas_transition(conn, session_id, REVIEWING, REVISING, reason="opinion")
    conn.close()
    return session_id, opinion_ids


def session_row(settings, session_id: int):  # type: ignore[no-untyped-def]
    conn = get_connection(settings.db_path)
    row = conn.execute("SELECT * FROM review_session WHERE id = ?", (session_id,)).fetchone()
    conn.close()
    return row


def opinion_rows(settings, session_id: int):  # type: ignore[no-untyped-def]
    conn = get_connection(settings.db_path)
    rows = conn.execute(
        "SELECT * FROM opinion WHERE session_id = ? ORDER BY id", (session_id,)
    ).fetchall()
    conn.close()
    return rows


def event_kinds(settings, session_id: int) -> list[str]:
    conn = get_connection(settings.db_path)
    rows = conn.execute(
        "SELECT kind FROM event_log WHERE session_id = ? ORDER BY id", (session_id,)
    ).fetchall()
    conn.close()
    return [row["kind"] for row in rows]


def make_item(session_id: int, *, age_seconds: float = 0.0) -> revise_executor.QueueItem:
    return revise_executor.QueueItem(session_id=session_id, enqueued_at=time.time() - age_seconds)


# ---------------------------------------------------------------------------
# kind=ok, everything applied
# ---------------------------------------------------------------------------
def test_ok_full_apply_advances_round_and_notifies(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    session_id, opinion_ids = seed_revising_session(settings, ["의견 1", "의견 2"])

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(revise_executor, "SlackClient", make_fake_slack_client(calls))

    revise_executor.process_one(make_item(session_id), settings=settings, runner=FakeRunner(kind="ok"))

    session = session_row(settings, session_id)
    assert session["status"] == REVIEWING
    assert session["round"] == 1
    assert session["revise_attempts"] == 0

    opinions = opinion_rows(settings, session_id)
    assert all(op["applied_round"] == 0 for op in opinions)
    assert all(op["last_verdict"] is None for op in opinions)

    assert "revise_success" in event_kinds(settings, session_id)
    # 새 메시지 발송(post) + 옛 메시지 버튼 회수(withdraw) — 편집(update) 아님.
    assert len(calls) == 2
    assert calls[0]["kind"] == "revise_result_post"
    assert calls[0]["round_number"] == 1
    assert calls[0]["unapplied"] == []
    assert calls[1]["kind"] == "withdraw"
    assert calls[1]["message_ts"] == "111.222"


# ---------------------------------------------------------------------------
# kind=ok, partial apply (some opinions unapplied)
# ---------------------------------------------------------------------------
def test_ok_partial_apply_records_last_verdict_and_leaves_applied_round_null(
    monkeypatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    session_id, opinion_ids = seed_revising_session(settings, ["반영됨", "미반영됨"])
    unapplied_id = opinion_ids[1]

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(revise_executor, "SlackClient", make_fake_slack_client(calls))

    revise_executor.process_one(
        make_item(session_id), settings=settings, runner=FakeRunner(kind="ok", unapplied_ids={unapplied_id})
    )

    opinions = {op["id"]: op for op in opinion_rows(settings, session_id)}
    assert opinions[opinion_ids[0]]["applied_round"] == 0
    assert opinions[opinion_ids[0]]["last_verdict"] is None
    assert opinions[unapplied_id]["applied_round"] is None
    assert opinions[unapplied_id]["last_verdict"] == "테스트 미반영 사유"

    session = session_row(settings, session_id)
    assert session["status"] == REVIEWING
    assert session["round"] == 1

    assert calls[0]["kind"] == "revise_result_post"
    assert calls[0]["unapplied"] == [{"reason": "테스트 미반영 사유"}]


# ---------------------------------------------------------------------------
# kind=ok re-notify — new message per round (사용자 결정: 라운드마다 새 알림),
# slack_ts moves to it, and the old message's buttons are withdrawn.
# ---------------------------------------------------------------------------
def test_revise_success_posts_new_message_with_round_number(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    session_id, _ = seed_revising_session(settings, ["의견 1"])

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(revise_executor, "SlackClient", make_fake_slack_client(calls))

    revise_executor.process_one(make_item(session_id), settings=settings, runner=FakeRunner(kind="ok"))

    post_calls = [call for call in calls if call["kind"] == "revise_result_post"]
    assert len(post_calls) == 1
    assert post_calls[0]["channel"] == "C123"
    assert post_calls[0]["round_number"] == 1


def test_revise_success_moves_session_slack_ts_to_new_message(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    session_id, _ = seed_revising_session(settings, ["의견 1"])
    seeded = session_row(settings, session_id)
    assert seeded["slack_ts"] == "111.222"

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(revise_executor, "SlackClient", make_fake_slack_client(calls))

    revise_executor.process_one(make_item(session_id), settings=settings, runner=FakeRunner(kind="ok"))

    session = session_row(settings, session_id)
    assert session["slack_ts"] == "999.888"
    assert session["slack_ts"] != seeded["slack_ts"]
    assert session["slack_channel"] == "C123"


def test_revise_success_withdraws_old_message_buttons(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    session_id, _ = seed_revising_session(settings, ["의견 1"])

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(revise_executor, "SlackClient", make_fake_slack_client(calls))

    revise_executor.process_one(make_item(session_id), settings=settings, runner=FakeRunner(kind="ok"))

    withdraw_calls = [call for call in calls if call["kind"] == "withdraw"]
    assert len(withdraw_calls) == 1
    assert withdraw_calls[0]["channel"] == "C123"
    assert withdraw_calls[0]["message_ts"] == "111.222"


def test_withdraw_buttons_failure_does_not_revert_round_progress(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Best-effort withdrawal (step e): an exception must not undo the round
    advance, status transition, or the slack_ts move already committed in
    steps (b)/(c)."""

    settings = configure(monkeypatch, tmp_path)
    session_id, _ = seed_revising_session(settings, ["의견 1"])

    calls: list[dict[str, Any]] = []
    base_client = make_fake_slack_client(calls)

    class RaisingWithdrawSlackClient(base_client):  # type: ignore[misc,valid-type]
        async def withdraw_buttons(  # type: ignore[override]
            self, channel: str, message_ts: str, header_text: str, reason: str
        ) -> None:
            raise RuntimeError("slack unavailable")

    monkeypatch.setattr(revise_executor, "SlackClient", RaisingWithdrawSlackClient)

    revise_executor.process_one(make_item(session_id), settings=settings, runner=FakeRunner(kind="ok"))

    session = session_row(settings, session_id)
    assert session["status"] == REVIEWING
    assert session["round"] == 1
    assert session["slack_ts"] == "999.888"


# ---------------------------------------------------------------------------
# Change-summary wiring — A(diff stat)/B(AI summary)/C(compare url) values
# reach post_revise_result's new keyword args.
# ---------------------------------------------------------------------------
def test_revise_success_notifies_with_summary_stat_and_compare_url(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    session_id, _ = seed_revising_session(settings, ["의견 1"])

    monkeypatch.setattr(revise_executor.git_workspace, "has_changes", lambda *a, **k: True)

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(revise_executor, "SlackClient", make_fake_slack_client(calls))

    revise_executor.process_one(
        make_item(session_id),
        settings=settings,
        runner=FakeRunner(kind="ok", detail="AI가 두 파일을 수정했습니다"),
    )

    post_calls = [call for call in calls if call["kind"] == "revise_result_post"]
    assert len(post_calls) == 1
    assert post_calls[0]["summary"] == "AI가 두 파일을 수정했습니다"
    assert post_calls[0]["diff_stat"] == " 1 file changed, 2 insertions(+), 1 deletion(-)"
    assert post_calls[0]["compare_url"] == f"https://gitlab.example.com/{REPO_SLUG}/-/compare/{SHA}...newsha000"


def test_compare_url_strips_userinfo_credentials_from_gitlab_url(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """GITLAB_URL 에 자격증명(userinfo)이 박혀 있어도 compare 링크·Slack 전송 인자에는
    노출되지 않아야 한다 — Slack 공개 채널 게시물은 회수가 불가능하다."""

    settings = configure(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "gitlab_url", "http://someuser:somepass@example.test")
    session_id, _ = seed_revising_session(settings, ["의견 1"])

    monkeypatch.setattr(revise_executor.git_workspace, "has_changes", lambda *a, **k: True)

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(revise_executor, "SlackClient", make_fake_slack_client(calls))

    revise_executor.process_one(
        make_item(session_id),
        settings=settings,
        runner=FakeRunner(kind="ok", detail="AI가 두 파일을 수정했습니다"),
    )

    post_calls = [call for call in calls if call["kind"] == "revise_result_post"]
    assert len(post_calls) == 1
    compare_url = post_calls[0]["compare_url"]
    assert compare_url is not None
    assert "someuser" not in compare_url
    assert "somepass" not in compare_url
    assert compare_url == f"http://example.test/{REPO_SLUG}/-/compare/{SHA}...newsha000"


def test_diff_stat_failure_keeps_notify_and_round_progress_with_diff_stat_none(
    monkeypatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    session_id, _ = seed_revising_session(settings, ["의견 1"])

    monkeypatch.setattr(revise_executor.git_workspace, "has_changes", lambda *a, **k: True)

    def _raise_diff_stat(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("git diff failed")

    monkeypatch.setattr(revise_executor.git_workspace, "diff_stat", _raise_diff_stat)

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(revise_executor, "SlackClient", make_fake_slack_client(calls))

    revise_executor.process_one(
        make_item(session_id), settings=settings, runner=FakeRunner(kind="ok", detail="요약 있음")
    )

    session = session_row(settings, session_id)
    assert session["status"] == REVIEWING
    assert session["round"] == 1
    assert session["slack_ts"] == "999.888"

    post_calls = [call for call in calls if call["kind"] == "revise_result_post"]
    assert len(post_calls) == 1
    assert post_calls[0]["diff_stat"] is None
    assert post_calls[0]["summary"] == "요약 있음"
    assert post_calls[0]["compare_url"] is not None


def test_no_commit_round_sends_none_diff_stat_and_compare_url(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """new_sha == old_sha (commit-less round) — stat/compare must both be None,
    while the summary still passes through untouched."""

    settings = configure(monkeypatch, tmp_path)
    session_id, _ = seed_revising_session(settings, ["의견 1"])

    class SameShaGitLabClient(FakeGitLabClient):
        async def get_merge_request(self, project_id: Any, merge_request_iid: Any) -> dict[str, Any]:
            data = await super().get_merge_request(project_id, merge_request_iid)
            return {**data, "sha": SHA}

    monkeypatch.setattr(revise_executor, "GitLabClient", SameShaGitLabClient)
    # has_changes stays False (configure() default) -> no commit made this round.

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(revise_executor, "SlackClient", make_fake_slack_client(calls))

    revise_executor.process_one(
        make_item(session_id), settings=settings, runner=FakeRunner(kind="ok", detail="변경 없음 요약")
    )

    post_calls = [call for call in calls if call["kind"] == "revise_result_post"]
    assert len(post_calls) == 1
    assert post_calls[0]["diff_stat"] is None
    assert post_calls[0]["compare_url"] is None
    assert post_calls[0]["summary"] == "변경 없음 요약"


def test_compare_url_returns_none_for_equal_shas_and_dedupes_trailing_slash(
    monkeypatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)

    assert revise_executor._compare_url(settings, REPO_SLUG, SHA, SHA) is None
    assert revise_executor._compare_url(settings, "", SHA, "newsha000") is None
    assert revise_executor._compare_url(settings, REPO_SLUG, "", "newsha000") is None

    monkeypatch.setattr(settings, "gitlab_url", "https://gitlab.example.com/")
    url = revise_executor._compare_url(settings, REPO_SLUG, SHA, "newsha000")
    assert url == f"https://gitlab.example.com/{REPO_SLUG}/-/compare/{SHA}...newsha000"
    assert "//" not in url.split("://", 1)[1]


# ---------------------------------------------------------------------------
# kind=failed -> one immediate requeue -> manual after attempts>=2
# ---------------------------------------------------------------------------
def test_failed_requeues_once_then_goes_manual(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    session_id, _ = seed_revising_session(settings, ["의견"])

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(revise_executor, "SlackClient", make_fake_slack_client(calls))

    failing_runner = FakeRunner(kind="failed", detail="첫 시도 실패")
    revise_executor.process_one(make_item(session_id), settings=settings, runner=failing_runner)

    session = session_row(settings, session_id)
    assert session["status"] == REVISING  # still revising — one retry budget left
    assert session["revise_attempts"] == 1
    kinds = event_kinds(settings, session_id)
    assert kinds.count("failed") == 1
    assert "requeued" in kinds
    assert not revise_executor._queue.empty()  # the immediate requeue landed on the queue

    requeued_item = revise_executor._queue.get_nowait()
    assert requeued_item.session_id == session_id

    # Second attempt fails again -> attempts reaches 2 -> CAS to manual.
    revise_executor.process_one(requeued_item, settings=settings, runner=failing_runner)

    session = session_row(settings, session_id)
    assert session["status"] == MANUAL
    assert session["revise_attempts"] == 2
    kinds = event_kinds(settings, session_id)
    assert kinds.count("failed") == 2
    assert calls[-1]["kind"] == "withdraw"


# ---------------------------------------------------------------------------
# Exceptions from the runner behave exactly like kind=failed
# ---------------------------------------------------------------------------
def test_runner_exception_is_treated_as_failed(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    session_id, _ = seed_revising_session(settings, ["의견"])

    revise_executor.process_one(make_item(session_id), settings=settings, runner=RaisingRunner())

    session = session_row(settings, session_id)
    assert session["status"] == REVISING
    assert session["revise_attempts"] == 1
    assert "failed" in event_kinds(settings, session_id)


# ---------------------------------------------------------------------------
# Queue-wait ceiling: a stale item is treated as failed without ever running
# the workspace/runner steps.
# ---------------------------------------------------------------------------
def test_queue_wait_exceeded_is_treated_as_failed(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    session_id, _ = seed_revising_session(settings, ["의견"])

    # Runner/workspace must never be invoked for a stale item — assert via a
    # runner whose .run would raise AssertionError if called.
    class UnreachableRunner:
        def run(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("runner must not be invoked when the queue-wait ceiling is exceeded")

    stale_item = make_item(session_id, age_seconds=settings.revise_queue_wait_limit + 1)
    revise_executor.process_one(stale_item, settings=settings, runner=UnreachableRunner())

    session = session_row(settings, session_id)
    assert session["status"] == REVISING
    assert session["revise_attempts"] == 1
    kinds = event_kinds(settings, session_id)
    assert kinds.count("failed") == 1
    assert "requeued" in kinds
    # The failure detail should be traceable to the queue-wait ceiling.
    conn = get_connection(settings.db_path)
    detail = conn.execute(
        "SELECT detail FROM event_log WHERE session_id = ? AND kind = 'failed' ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()["detail"]
    conn.close()
    assert "queue wait exceeded" in detail


# ---------------------------------------------------------------------------
# A session that already left `revising` before dequeue is a silent no-op.
# ---------------------------------------------------------------------------
def test_session_no_longer_revising_is_a_noop(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    session_id, _ = seed_revising_session(settings, ["의견"])
    conn = get_connection(settings.db_path)
    assert cas_transition(conn, session_id, REVISING, MANUAL, reason="human_push")
    conn.close()

    class UnreachableRunner:
        def run(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("must not run for a session that already left `revising`")

    revise_executor.process_one(make_item(session_id), settings=settings, runner=UnreachableRunner())

    session = session_row(settings, session_id)
    assert session["status"] == MANUAL
    assert session["revise_attempts"] == 0  # untouched


# ---------------------------------------------------------------------------
# F1: CAS(revising->reviewing) is the leading precondition — if it loses the
# race (session already left `revising` via some other path), round,
# opinion.applied_round, and mr_sha must all stay untouched.
# ---------------------------------------------------------------------------
def test_ok_result_cas_race_leaves_round_and_applied_round_and_sha_untouched(
    monkeypatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    session_id, opinion_ids = seed_revising_session(settings, ["의견 1"])

    # Seed a prior failed attempt so revise_attempts is nonzero going in —
    # the raced CAS must leave this counter untouched too, not just
    # round/applied_round/mr_sha.
    conn = get_connection(settings.db_path)
    conn.execute("UPDATE review_session SET revise_attempts = 1 WHERE id = ?", (session_id,))
    conn.commit()
    conn.close()

    before = session_row(settings, session_id)
    assert before["round"] == 0
    assert before["mr_sha"] == SHA
    assert before["revise_attempts"] == 1

    # Race: some other path (e.g. human_push) moves the session out of
    # `revising` before the executor's own CAS gets to run.
    conn = get_connection(settings.db_path)
    assert cas_transition(conn, session_id, REVISING, MANUAL, reason="human_push")
    conn.close()

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(revise_executor, "SlackClient", make_fake_slack_client(calls))

    # current_sha would return a new sha if push happened; force `changed`
    # to make sure a would-be sha update is exercised then must be discarded.
    monkeypatch.setattr(revise_executor.git_workspace, "has_changes", lambda *a, **k: True)

    handle_conn = get_connection(settings.db_path)
    revise_executor._handle_ok(
        conn=handle_conn,
        settings=settings,
        session=before,
        mr_full={"source_branch": "feature/test", "sha": "newsha000"},
        workspace=FAKE_WORKSPACE,
        opinions=[dict(row) for row in opinion_rows(settings, session_id)],
        result=ReviseResult(kind="ok", unapplied=[], detail=""),
    )
    handle_conn.close()

    after = session_row(settings, session_id)
    assert after["status"] == MANUAL  # unchanged by the raced call
    assert after["round"] == 0  # no round advance
    assert after["mr_sha"] == SHA  # no sha update
    assert after["revise_attempts"] == 1  # no attempts-counter write on a raced CAS
    opinions = opinion_rows(settings, session_id)
    assert all(op["applied_round"] is None for op in opinions)  # no applied_round writes
    assert "revise_success_raced" in event_kinds(settings, session_id)
    assert calls == []  # no Slack notify on a raced CAS


# ---------------------------------------------------------------------------
# enqueue_revise: non-blocking, logs event_log, pushes onto the queue.
# ---------------------------------------------------------------------------
def test_enqueue_revise_is_non_blocking_and_logs_event(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    session_id, _ = seed_revising_session(settings, ["의견"])

    import asyncio

    asyncio.run(revise_executor.enqueue_revise(session_id))

    assert not revise_executor._queue.empty()
    item = revise_executor._queue.get_nowait()
    assert item.session_id == session_id
    assert "enqueued" in event_kinds(settings, session_id)


# ---------------------------------------------------------------------------
# Workspace-preparation exceptions: the `failed` detail must carry both the
# exception class name and its message (not just the class name — the
# observability gap revise-workspace-failure.md §1/§2 diagnosed), while any
# app secret embedded in that message is masked before it ever reaches
# event_log (revise-workspace-fix.md §3).
# ---------------------------------------------------------------------------
def test_workspace_preparation_exception_detail_has_class_and_message_without_secret(
    monkeypatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    session_id, _ = seed_revising_session(settings, ["의견"])

    fake_secret = "glpat-fake-secret-in-exception-000"
    monkeypatch.setattr(settings, "gitlab_token", fake_secret)

    def _raise_with_secret(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError(f"clone failed: could not authenticate using token={fake_secret}")

    monkeypatch.setattr(revise_executor.git_workspace, "ensure_workspace", _raise_with_secret)

    revise_executor.process_one(make_item(session_id), settings=settings, runner=FakeRunner(kind="ok"))

    session = session_row(settings, session_id)
    assert session["status"] == REVISING  # one retry budget left, same as other `failed` paths
    assert session["revise_attempts"] == 1

    conn = get_connection(settings.db_path)
    detail = conn.execute(
        "SELECT detail FROM event_log WHERE session_id = ? AND kind = 'failed' ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()["detail"]
    conn.close()

    assert "RuntimeError" in detail  # exception class name
    assert "clone failed: could not authenticate using token=" in detail  # exception message
    assert fake_secret not in detail  # masked before it ever reaches event_log


# ---------------------------------------------------------------------------
# Slack-bound failure reason is capped at 300 chars (revise-workspace-fix.md
# §4) even though the DB-persisted detail is not.
# ---------------------------------------------------------------------------
def test_manual_notify_slack_reason_is_capped_at_300_chars(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    session_id, _ = seed_revising_session(settings, ["의견"])

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(revise_executor, "SlackClient", make_fake_slack_client(calls))

    long_detail = "x" * 900 + " 매우 긴 실패 상세 메시지"
    failing_runner = FakeRunner(kind="failed", detail=long_detail)

    # Two failures exhaust the retry budget -> CAS to manual -> _notify_manual fires.
    revise_executor.process_one(make_item(session_id), settings=settings, runner=failing_runner)
    requeued_item = revise_executor._queue.get_nowait()
    revise_executor.process_one(requeued_item, settings=settings, runner=failing_runner)

    session = session_row(settings, session_id)
    assert session["status"] == MANUAL

    truncated = revise_executor._truncate_for_slack(long_detail)
    assert len(truncated) == 300
    assert truncated.endswith("…")

    assert calls[-1]["kind"] == "withdraw"
    assert truncated in calls[-1]["reason"]
    assert long_detail not in calls[-1]["reason"]  # the untruncated form must never reach Slack

    # The DB-persisted detail keeps the full, untruncated text — the
    # asymmetry _truncate_for_slack's own docstring calls out.
    conn = get_connection(settings.db_path)
    db_detail = conn.execute(
        "SELECT detail FROM event_log WHERE session_id = ? AND kind = 'failed' ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()["detail"]
    conn.close()
    assert db_detail == long_detail
