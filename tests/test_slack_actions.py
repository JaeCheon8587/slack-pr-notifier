"""Tests for the Slack approval/opinion rails (app/slack_actions.py) — v4.1/P4a.

Covers: step-0 authorization (unauthorized click, empty reviewer mapping),
sha-freshness rejection, CAS duplicate-click no-op, the merge-success path
(merging -> merged via background poll), the poll-failure path
(merging -> manual), signed-token forgery/expiry rejection, and the P4a
opinion rail: [의견] button -> modal open, modal submit -> opinion INSERT +
GitLab comment mirror -> guard (a)(b) -> CAS reviewing->revising -> revise
executor kickoff (stubbed) -> Slack update.
"""

import asyncio
import hashlib
import hmac
import json
from urllib.parse import urlencode

import httpx
import pytest
from httpx import ASGITransport, AsyncClient, Response

import app.slack_dispatch as slack_dispatch
from app.action_token import create_action_token
from app.config import get_settings
from app.db import get_connection, init_db
from app.gitlab_client import GitLabClient
from app.main import app
from app.slack_client import SlackClient, _revise_result_payload, review_blocks
from app.state_machine import MANUAL, MERGED, MERGING, REVIEWING, REVISING, cas_transition

SLACK_SECRET = "test-slack-secret"
ACTION_SECRET = "test-action-secret"
TIMESTAMP = "1000"

PROJECT_ID = 42
IID = 1
REPO_SLUG = "group/project"
SHA = "abc123"
AUTHORIZED_USER = "U_AUTH"


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------
def slack_signature(body: bytes) -> str:
    base = b"v0:" + TIMESTAMP.encode() + b":" + body
    digest = hmac.new(SLACK_SECRET.encode(), base, hashlib.sha256).hexdigest()
    return f"v0={digest}"


def post_slack(payload: dict[str, object], signature: str | None = None) -> Response:
    body = urlencode({"payload": json.dumps(payload, separators=(",", ":"))}).encode()

    async def send() -> Response:
        transport = ASGITransport(app=app)
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Slack-Request-Timestamp": TIMESTAMP,
            "X-Slack-Signature": signature or slack_signature(body),
        }
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/webhooks/slack/actions", content=body, headers=headers)

    return asyncio.run(send())


def mr_token(*, project_id: int = PROJECT_ID, iid: int = IID, sha: str = SHA, ttl_seconds: int = 86400) -> str:
    return create_action_token(
        {
            "project_id": project_id,
            "repository": REPO_SLUG,
            "iid": iid,
            "title": "Test MR",
            "url": "https://gitlab.example.com/group/project/-/merge_requests/1",
            "sha": sha,
            "head_ref": "feature/test",
            "base_ref": "main",
            "author": "author",
        },
        ACTION_SECRET,
        ttl_seconds=ttl_seconds,
    )


def approve_payload(token: str, *, user_id: str = AUTHORIZED_USER) -> dict[str, object]:
    return {
        "type": "block_actions",
        "user": {"id": user_id},
        "channel": {"id": "C123"},
        "container": {"message_ts": "111.222"},
        "response_url": "https://hooks.slack.example.com/actions/response",
        "actions": [{"action_id": "approve_mr", "value": token}],
    }


def opinion_click_payload(token: str, *, user_id: str = AUTHORIZED_USER, trigger_id: str = "T123") -> dict[str, object]:
    return {
        "type": "block_actions",
        "user": {"id": user_id},
        "channel": {"id": "C123"},
        "container": {"message_ts": "111.222"},
        "response_url": "https://hooks.slack.example.com/actions/response",
        "trigger_id": trigger_id,
        "actions": [{"action_id": "request_changes_mr", "value": token}],
    }


def opinion_submit_payload(token: str, body: str, *, user_id: str = AUTHORIZED_USER) -> dict[str, object]:
    # No "question_refs_block" — the modal no longer has that input (AI
    # confirmation-question generation is unimplemented, so it was dead UI).
    # Every test using this helper therefore doubles as a regression check
    # that submission still succeeds without a question_refs field at all.
    return {
        "type": "view_submission",
        "user": {"id": user_id},
        "view": {
            "callback_id": "opinion_submission",
            "private_metadata": token,
            "state": {
                "values": {
                    "opinion_block": {"opinion_body": {"value": body}},
                }
            },
        },
    }


# ---------------------------------------------------------------------------
# Settings / DB fixtures
# ---------------------------------------------------------------------------
def configure(monkeypatch, tmp_path, *, reviewer_map: str | None = None):  # type: ignore[no-untyped-def]
    settings = get_settings()
    monkeypatch.setattr(settings, "slack_signing_secret", SLACK_SECRET)
    monkeypatch.setattr(settings, "slack_bot_token", "xoxb-test")
    monkeypatch.setattr(settings, "gitlab_url", "https://gitlab.example.com")
    monkeypatch.setattr(settings, "gitlab_token", "gitlab-test")
    monkeypatch.setattr(settings, "action_token_secret", ACTION_SECRET)
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(
        settings,
        "reviewer_map",
        reviewer_map if reviewer_map is not None else json.dumps({REPO_SLUG: AUTHORIZED_USER}),
    )
    monkeypatch.setattr("app.security.time.time", lambda: int(TIMESTAMP))
    monkeypatch.setattr("app.action_token.time.time", lambda: int(TIMESTAMP))
    # The merge poll sleeps 3s x up to 10 attempts — make it instant for tests.
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
    return settings


async def _instant_sleep(_seconds: float) -> None:
    return None


def _seed_session(
    settings, *, project_id: int = PROJECT_ID, iid: int = IID, sha: str = SHA, repo_slug: str = REPO_SLUG, status: str = REVIEWING
) -> int:
    conn = get_connection(settings.db_path)
    init_db(conn)
    conn.execute(
        "INSERT INTO review_session (project_id, mr_iid, mr_sha, repo_slug, slack_channel, slack_ts) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (str(project_id), iid, sha, repo_slug, "C123", "111.222"),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM review_session WHERE project_id = ? AND mr_iid = ?", (str(project_id), iid)
    ).fetchone()
    session_id = row["id"]
    if status == MERGING:
        assert cas_transition(conn, session_id, REVIEWING, MERGING, reason="approve")
    elif status == REVISING:
        assert cas_transition(conn, session_id, REVIEWING, REVISING, reason="opinion")
    elif status != REVIEWING:
        raise ValueError(status)
    conn.close()
    return session_id


def _set_round(settings, session_id: int, round_value: int) -> None:  # type: ignore[no-untyped-def]
    conn = get_connection(settings.db_path)
    conn.execute("UPDATE review_session SET round = ? WHERE id = ?", (round_value, session_id))
    conn.commit()
    conn.close()


def _opinion_rows(settings, session_id: int):  # type: ignore[no-untyped-def]
    conn = get_connection(settings.db_path)
    rows = conn.execute(
        "SELECT * FROM opinion WHERE session_id = ? ORDER BY id", (session_id,)
    ).fetchall()
    conn.close()
    return rows


def _session_row(settings, session_id: int):  # type: ignore[no-untyped-def]
    conn = get_connection(settings.db_path)
    row = conn.execute("SELECT * FROM review_session WHERE id = ?", (session_id,)).fetchone()
    conn.close()
    return row


def _event_kinds(settings, session_id: int) -> list[str]:
    conn = get_connection(settings.db_path)
    rows = conn.execute(
        "SELECT kind FROM event_log WHERE session_id = ? ORDER BY id", (session_id,)
    ).fetchall()
    conn.close()
    return [row["kind"] for row in rows]


# ---------------------------------------------------------------------------
# Fixtures shared by tests that need to fake GitLab responses
# ---------------------------------------------------------------------------
def _fake_get_merge_request(responses):  # type: ignore[no-untyped-def]
    """Return an async fn returning successive ``responses`` (repeats the last)."""

    state = {"calls": list(responses)}

    async def fake(self, project_id, iid):  # type: ignore[no-untyped-def]
        if len(state["calls"]) > 1:
            return state["calls"].pop(0)
        return state["calls"][0]

    return fake


# ---------------------------------------------------------------------------
# [1] Step-0 authorization
# ---------------------------------------------------------------------------
def test_unauthorized_click_is_rejected_without_state_change(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    session_id = _seed_session(settings)
    sent: list[str] = []

    async def fake_ephemeral(response_url, text):  # noqa: ANN001
        sent.append(text)

    monkeypatch.setattr(slack_dispatch, "_send_ephemeral", fake_ephemeral)

    response = post_slack(approve_payload(mr_token(), user_id="U_STRANGER"))

    assert response.status_code == 200
    assert response.json()["action"] == "unauthorized"
    assert sent  # ephemeral rejection sent
    row = _session_row(settings, session_id)
    assert row["status"] == REVIEWING
    assert "guard_reject" in _event_kinds(settings, session_id)


def test_empty_reviewer_mapping_is_rejected(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path, reviewer_map="")
    session_id = _seed_session(settings)
    monkeypatch.setattr(slack_dispatch, "_send_ephemeral", _noop_ephemeral)

    response = post_slack(approve_payload(mr_token(), user_id=AUTHORIZED_USER))

    assert response.status_code == 200
    assert response.json()["action"] == "unauthorized"
    row = _session_row(settings, session_id)
    assert row["status"] == REVIEWING


async def _noop_ephemeral(response_url, text):  # noqa: ANN001
    return None


# ---------------------------------------------------------------------------
# [1b] Ephemeral response must not replace the original message (regression).
# Slack's response_url POST replaces the original message by default when
# replace_original/delete_original are omitted — that was silently deleting
# the [승인]/[의견] buttons (and their still-valid signed action token) the
# moment an unauthorized click's rejection was sent, so a later authorized
# click failed with InvalidActionToken instead of succeeding.
# ---------------------------------------------------------------------------
def test_send_ephemeral_sets_replace_original_false(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured: dict[str, object] = {}

    async def fake_post(self, url, json=None, **kwargs):  # noqa: ANN001
        captured["url"] = url
        captured["json"] = json
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    asyncio.run(
        slack_dispatch._send_ephemeral(
            "https://hooks.slack.example.com/actions/response", "이 MR을 승인/머지할 권한이 없습니다."
        )
    )

    assert captured["url"] == "https://hooks.slack.example.com/actions/response"
    body = captured["json"]
    assert body["response_type"] == "ephemeral"
    assert body["text"] == "이 MR을 승인/머지할 권한이 없습니다."
    assert body["replace_original"] is False
    assert body["delete_original"] is False


def test_unauthorized_approve_click_preserves_original_message(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """End-to-end regression: after an unauthorized [승인] click is rejected,
    the original message (buttons + signed action token) must remain exactly
    as posted — i.e. (a) the ephemeral rejection is sent with
    replace_original=False and (b) the guard-reject path never calls any
    Slack message-update API (chat.update via SlackClient.call), which is the
    only other thing that could rewrite/strip the original message.
    """
    settings = configure(monkeypatch, tmp_path)
    session_id = _seed_session(settings)

    captured_ephemeral: list[dict[str, object]] = []
    original_post = httpx.AsyncClient.post

    async def fake_post(self, url, *args, **kwargs):  # noqa: ANN001
        if url == "https://hooks.slack.example.com/actions/response":
            captured_ephemeral.append({"url": url, "json": kwargs.get("json")})
            return httpx.Response(200, json={"ok": True})
        return await original_post(self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    slack_api_calls: list[str] = []

    async def spying_call(self, method, payload):  # noqa: ANN001
        slack_api_calls.append(method)
        return {"ok": True}

    monkeypatch.setattr(SlackClient, "call", spying_call)

    response = post_slack(approve_payload(mr_token(), user_id="U_STRANGER"))

    assert response.status_code == 200
    assert response.json()["action"] == "unauthorized"

    assert len(captured_ephemeral) == 1
    body = captured_ephemeral[0]["json"]
    assert body["response_type"] == "ephemeral"
    assert body["replace_original"] is False
    assert body["delete_original"] is False

    # The buttons + signed token embedded in the original message survive
    # only if nothing on this path calls chat.update (or any other Slack Web
    # API method) to rewrite/replace it.
    assert slack_api_calls == []

    row = _session_row(settings, session_id)
    assert row["status"] == REVIEWING
    assert "guard_reject" in _event_kinds(settings, session_id)


# ---------------------------------------------------------------------------
# [2] sha freshness
# ---------------------------------------------------------------------------
def test_stale_sha_is_rejected(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    session_id = _seed_session(settings)
    monkeypatch.setattr(slack_dispatch, "_send_ephemeral", _noop_ephemeral)
    monkeypatch.setattr(
        GitLabClient, "get_merge_request", _fake_get_merge_request([{"sha": "new-commit-sha"}])
    )

    response = post_slack(approve_payload(mr_token(sha=SHA)))

    assert response.status_code == 200
    assert response.json()["action"] == "sha_stale"
    row = _session_row(settings, session_id)
    assert row["status"] == REVIEWING
    assert "sha_stale" in _event_kinds(settings, session_id)


# ---------------------------------------------------------------------------
# [3] CAS duplicate-click no-op
# ---------------------------------------------------------------------------
def test_duplicate_click_is_a_noop(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    session_id = _seed_session(settings, status=MERGING)
    monkeypatch.setattr(slack_dispatch, "_send_ephemeral", _noop_ephemeral)
    monkeypatch.setattr(GitLabClient, "get_merge_request", _fake_get_merge_request([{"sha": SHA}]))

    response = post_slack(approve_payload(mr_token()))

    assert response.status_code == 200
    assert response.json()["action"] == "duplicate"
    row = _session_row(settings, session_id)
    assert row["status"] == MERGING
    assert "duplicate_click" in _event_kinds(settings, session_id)


# ---------------------------------------------------------------------------
# [4] merge success path
# ---------------------------------------------------------------------------
def test_approve_success_merges_and_polls_to_merged(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    session_id = _seed_session(settings)
    monkeypatch.setattr(slack_dispatch, "_send_ephemeral", _noop_ephemeral)
    monkeypatch.setattr(
        GitLabClient,
        "get_merge_request",
        _fake_get_merge_request(
            [
                {"sha": SHA},  # step (b) freshness check
                {"sha": SHA, "state": "opened"},  # poll attempt 1
                {"sha": SHA, "state": "merged"},  # poll attempt 2 -> success
            ]
        ),
    )
    merge_calls: list[tuple[object, object, object]] = []

    async def fake_merge(self, project_id, iid, sha):  # noqa: ANN001
        merge_calls.append((project_id, iid, sha))
        return {"state": "opened"}

    async def fake_note(self, project_id, iid, body):  # noqa: ANN001
        return {"id": 1}

    updated: dict[str, object] = {}

    async def fake_update_decision(self, channel, message_ts, mr, **kwargs):  # noqa: ANN001
        updated.update(channel=channel, message_ts=message_ts, mr=mr, **kwargs)

    monkeypatch.setattr(GitLabClient, "merge_merge_request", fake_merge)
    monkeypatch.setattr(GitLabClient, "create_merge_request_note", fake_note)
    monkeypatch.setattr(SlackClient, "update_decision", fake_update_decision)

    response = post_slack(approve_payload(mr_token()))

    assert response.status_code == 200
    assert response.json()["action"] == "approve_in_progress"
    assert merge_calls == [(PROJECT_ID, IID, SHA)]
    row = _session_row(settings, session_id)
    assert row["status"] == MERGED
    assert updated["approved"] is True
    assert updated["slack_user_id"] == AUTHORIZED_USER


# ---------------------------------------------------------------------------
# [5] poll failure -> manual
# ---------------------------------------------------------------------------
def test_poll_timeout_transitions_to_manual(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    session_id = _seed_session(settings)
    monkeypatch.setattr(slack_dispatch, "_send_ephemeral", _noop_ephemeral)

    responses = [{"sha": SHA}] + [{"sha": SHA, "state": "opened"}] * 10
    monkeypatch.setattr(GitLabClient, "get_merge_request", _fake_get_merge_request(responses))

    async def fake_merge(self, project_id, iid, sha):  # noqa: ANN001
        return {"state": "opened"}

    async def fake_note(self, project_id, iid, body):  # noqa: ANN001
        return {"id": 1}

    withdrawn: dict[str, object] = {}

    async def fake_withdraw(self, channel, message_ts, header_text, reason):  # noqa: ANN001
        withdrawn.update(channel=channel, message_ts=message_ts, header=header_text, reason=reason)

    monkeypatch.setattr(GitLabClient, "merge_merge_request", fake_merge)
    monkeypatch.setattr(GitLabClient, "create_merge_request_note", fake_note)
    monkeypatch.setattr(SlackClient, "withdraw_buttons", fake_withdraw)

    response = post_slack(approve_payload(mr_token()))

    assert response.status_code == 200
    row = _session_row(settings, session_id)
    assert row["status"] == MANUAL
    assert withdrawn
    assert "merge_poll_failed" in _event_kinds(settings, session_id)


# ---------------------------------------------------------------------------
# [6] signed-token forgery / expiry
# ---------------------------------------------------------------------------
def test_forged_token_signature_is_rejected(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    _seed_session(settings)
    token = mr_token()
    # Flip the first character (a real data bit, not a base64 padding bit —
    # unlike the trailing char, which can land on discarded padding bits and
    # decode identically) so the signature is guaranteed to change.
    tampered = ("A" if token[0] != "A" else "B") + token[1:]

    response = post_slack(approve_payload(tampered))

    assert response.status_code == 400


def test_expired_token_is_rejected(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    _seed_session(settings)
    expired_token = mr_token(ttl_seconds=-10)

    response = post_slack(approve_payload(expired_token))

    assert response.status_code == 400


def test_rejects_invalid_slack_signature(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    configure(monkeypatch, tmp_path)
    payload: dict[str, object] = {"type": "block_actions", "user": {"id": "U123"}}

    response = post_slack(payload, "v0=invalid")

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# [7] [의견] rail (P4a) — modal open
# ---------------------------------------------------------------------------
def test_opinion_click_opens_modal_when_authorized(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    _seed_session(settings)
    opened: dict[str, object] = {}

    async def fake_open_modal(self, trigger_id, metadata):  # noqa: ANN001
        opened.update(trigger_id=trigger_id, metadata=metadata)

    monkeypatch.setattr(SlackClient, "open_opinion_modal", fake_open_modal)

    token = mr_token()
    response = post_slack(opinion_click_payload(token))

    assert response.status_code == 200
    assert response.json()["action"] == "opinion_modal_opened"
    assert opened == {"trigger_id": "T123", "metadata": token}


def test_opinion_click_rejected_when_unauthorized(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    session_id = _seed_session(settings)
    sent: list[str] = []

    async def fake_ephemeral(response_url, text):  # noqa: ANN001
        sent.append(text)

    monkeypatch.setattr(slack_dispatch, "_send_ephemeral", fake_ephemeral)

    opened_calls: list[str] = []

    async def fake_open_modal(self, trigger_id, metadata):  # noqa: ANN001
        opened_calls.append(trigger_id)

    monkeypatch.setattr(SlackClient, "open_opinion_modal", fake_open_modal)

    response = post_slack(opinion_click_payload(mr_token(), user_id="U_STRANGER"))

    assert response.status_code == 200
    assert response.json()["action"] == "unauthorized"
    assert sent
    assert not opened_calls
    row = _session_row(settings, session_id)
    assert row["status"] == REVIEWING
    assert "guard_reject" in _event_kinds(settings, session_id)


def test_opinion_click_rejects_forged_token(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    _seed_session(settings)
    token = mr_token()
    tampered = ("A" if token[0] != "A" else "B") + token[1:]

    response = post_slack(opinion_click_payload(tampered))

    assert response.status_code == 400


# ---------------------------------------------------------------------------
# [7b] [의견] modal — "대상 확인질문 번호" input removed (dead UI: AI
# confirmation-question generation is unimplemented, so there was never a
# numbered question for it to reference).
# ---------------------------------------------------------------------------
def test_opinion_modal_has_no_question_refs_input(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured: dict[str, object] = {}

    async def fake_call(self, method, payload):  # noqa: ANN001
        captured["method"] = method
        captured["payload"] = payload
        return {"ok": True}

    monkeypatch.setattr(SlackClient, "call", fake_call)

    client = SlackClient("xoxb-test")
    asyncio.run(client.open_opinion_modal("T123", "metadata-token"))

    assert captured["method"] == "views.open"
    blocks = captured["payload"]["view"]["blocks"]
    block_ids = {block.get("block_id") for block in blocks}
    action_ids = {
        block.get("element", {}).get("action_id") for block in blocks if block.get("type") == "input"
    }
    assert "question_refs_block" not in block_ids
    assert "question_refs" not in action_ids
    assert block_ids == {"opinion_block"}
    assert action_ids == {"opinion_body"}


# ---------------------------------------------------------------------------
# [8] [의견] rail (P4a) — modal submit: opinion INSERT + GitLab note mirror
# ---------------------------------------------------------------------------
def _stub_gitlab_note(monkeypatch):  # type: ignore[no-untyped-def]
    notes: list[tuple[object, object, str]] = []

    async def fake_note(self, project_id, iid, body):  # noqa: ANN001
        notes.append((project_id, iid, body))
        return {"id": 1}

    monkeypatch.setattr(GitLabClient, "create_merge_request_note", fake_note)
    return notes


def _stub_no_new_commits(monkeypatch):  # type: ignore[no-untyped-def]
    async def fake_commits(self, project_id, iid):  # noqa: ANN001
        return [{"id": SHA, "author_name": "someone", "author_email": "someone@example.com"}]

    monkeypatch.setattr(GitLabClient, "list_mr_commits", fake_commits)


def test_opinion_submission_inserts_row_and_mirrors_gitlab_note(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    session_id = _seed_session(settings)
    notes = _stub_gitlab_note(monkeypatch)
    _stub_no_new_commits(monkeypatch)

    enqueued: list[int] = []

    async def fake_enqueue(session_id_arg):  # noqa: ANN001
        enqueued.append(session_id_arg)

    monkeypatch.setattr(slack_dispatch, "enqueue_revise", fake_enqueue)
    updated: dict[str, object] = {}

    async def fake_update_revising(self, channel, message_ts, header_text):  # noqa: ANN001
        updated.update(channel=channel, message_ts=message_ts, header_text=header_text)

    monkeypatch.setattr(SlackClient, "update_revising", fake_update_revising)

    response = post_slack(opinion_submit_payload(mr_token(), "이 부분을 고쳐주세요"))

    # F4: view_submission success is an empty-body 200 (Slack contract) —
    # user-facing outcome is the main-message update below, not the HTTP body.
    assert response.status_code == 200
    assert response.json() == {}
    rows = _opinion_rows(settings, session_id)
    assert len(rows) == 1
    assert rows[0]["slack_user"] == AUTHORIZED_USER
    assert rows[0]["body"] == "이 부분을 고쳐주세요"
    # question_refs input was removed from the modal (dead UI) — the column
    # stays NULL. The submitted payload also carries no question_refs field
    # at all (regression: submission still succeeds without it).
    assert rows[0]["question_refs"] is None
    assert notes and notes[0][2].startswith("## Slack MR Review")
    assert enqueued == [session_id]
    row = _session_row(settings, session_id)
    assert row["status"] == REVISING
    assert updated


# ---------------------------------------------------------------------------
# [9] [의견] rail (P4a) — UNIQUE duplicate submit
# ---------------------------------------------------------------------------
def test_opinion_duplicate_submission_is_ephemeral_and_state_unchanged(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    session_id = _seed_session(settings)
    _stub_gitlab_note(monkeypatch)
    _stub_no_new_commits(monkeypatch)

    enqueued: list[int] = []

    async def fake_enqueue(session_id_arg):  # noqa: ANN001
        enqueued.append(session_id_arg)

    monkeypatch.setattr(slack_dispatch, "enqueue_revise", fake_enqueue)

    async def fake_update_revising(self, channel, message_ts, header_text):  # noqa: ANN001
        return None

    monkeypatch.setattr(SlackClient, "update_revising", fake_update_revising)

    ephemeral_calls: list[str] = []

    async def fake_post_ephemeral(self, channel, user_id, text):  # noqa: ANN001
        ephemeral_calls.append(text)

    monkeypatch.setattr(SlackClient, "post_ephemeral", fake_post_ephemeral)

    body = "동일한 의견입니다"
    first = post_slack(opinion_submit_payload(mr_token(), body))
    assert first.status_code == 200
    assert first.json() == {}

    # Session is now `revising`; re-seed to `reviewing` to isolate the
    # duplicate-INSERT check from the CAS-race check below.
    conn = get_connection(settings.db_path)
    conn.execute("UPDATE review_session SET status = ? WHERE id = ?", (REVIEWING, session_id))
    conn.commit()
    conn.close()

    second = post_slack(opinion_submit_payload(mr_token(), body))

    assert second.status_code == 200
    assert second.json() == {}
    assert ephemeral_calls and "이미 접수" in ephemeral_calls[0]
    rows = _opinion_rows(settings, session_id)
    assert len(rows) == 1
    row = _session_row(settings, session_id)
    assert row["status"] == REVIEWING


# ---------------------------------------------------------------------------
# [10] [의견] rail (P4a) — guard (a): round cap
# ---------------------------------------------------------------------------
def test_opinion_guard_round_cap_transitions_to_manual(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    session_id = _seed_session(settings)
    _set_round(settings, session_id, 3)
    _stub_gitlab_note(monkeypatch)

    withdrawn: dict[str, object] = {}

    async def fake_withdraw(self, channel, message_ts, header_text, reason):  # noqa: ANN001
        withdrawn.update(channel=channel, message_ts=message_ts, header=header_text, reason=reason)

    monkeypatch.setattr(SlackClient, "withdraw_buttons", fake_withdraw)

    response = post_slack(opinion_submit_payload(mr_token(), "라운드 초과 의견"))

    assert response.status_code == 200
    assert response.json() == {}
    row = _session_row(settings, session_id)
    assert row["status"] == MANUAL
    assert withdrawn
    # The opinion row itself is still recorded even though the round guard
    # rejected the auto-revise.
    assert len(_opinion_rows(settings, session_id)) == 1
    conn = get_connection(settings.db_path)
    detail = conn.execute(
        "SELECT detail FROM event_log WHERE session_id = ? AND kind = 'guard_reject' ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()["detail"]
    conn.close()
    assert "round" in detail


# ---------------------------------------------------------------------------
# [11] [의견] rail (P4a) — guard (b): human commit detected
# ---------------------------------------------------------------------------
def test_opinion_guard_human_commit_transitions_to_manual(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    session_id = _seed_session(settings)
    _stub_gitlab_note(monkeypatch)

    async def fake_commits(self, project_id, iid):  # noqa: ANN001
        return [
            {"id": "new-human-sha", "author_name": "a-human", "author_email": "human@example.com"},
            {"id": SHA, "author_name": "someone", "author_email": "someone@example.com"},
        ]

    monkeypatch.setattr(GitLabClient, "list_mr_commits", fake_commits)

    withdrawn: dict[str, object] = {}

    async def fake_withdraw(self, channel, message_ts, header_text, reason):  # noqa: ANN001
        withdrawn.update(channel=channel, message_ts=message_ts, header=header_text, reason=reason)

    monkeypatch.setattr(SlackClient, "withdraw_buttons", fake_withdraw)

    response = post_slack(opinion_submit_payload(mr_token(), "사람 커밋 이후 의견"))

    # F4: view_submission always returns an empty body (Slack's contract) —
    # the outcome is observed via state transition + Slack notification, not
    # the response payload.
    assert response.status_code == 200
    assert response.json() == {}
    row = _session_row(settings, session_id)
    assert row["status"] == MANUAL
    assert withdrawn


def test_opinion_guard_human_commit_ignores_bot_author(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "bot_username", "revise-bot")
    session_id = _seed_session(settings)
    _stub_gitlab_note(monkeypatch)

    async def fake_commits(self, project_id, iid):  # noqa: ANN001
        return [
            {"id": "bot-sha", "author_name": "revise-bot", "author_email": "bot@example.com"},
            {"id": SHA, "author_name": "someone", "author_email": "someone@example.com"},
        ]

    monkeypatch.setattr(GitLabClient, "list_mr_commits", fake_commits)

    enqueued: list[int] = []

    async def fake_enqueue(session_id_arg):  # noqa: ANN001
        enqueued.append(session_id_arg)

    monkeypatch.setattr(slack_dispatch, "enqueue_revise", fake_enqueue)

    async def fake_update_revising(self, channel, message_ts, header_text):  # noqa: ANN001
        return None

    monkeypatch.setattr(SlackClient, "update_revising", fake_update_revising)

    response = post_slack(opinion_submit_payload(mr_token(), "봇 커밋만 있는 경우"))

    # F4: view_submission always returns an empty body (Slack's contract) —
    # the outcome is observed via state transition + revise enqueue, not the
    # response payload.
    assert response.status_code == 200
    assert response.json() == {}
    assert enqueued == [session_id]
    row = _session_row(settings, session_id)
    assert row["status"] == REVISING


# ---------------------------------------------------------------------------
# [12] [의견] rail (P4a) — CAS race: already revising
# ---------------------------------------------------------------------------
def test_opinion_submission_when_already_revising_is_ephemeral(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    session_id = _seed_session(settings, status=REVISING)
    _stub_gitlab_note(monkeypatch)
    _stub_no_new_commits(monkeypatch)

    enqueued: list[int] = []

    async def fake_enqueue(session_id_arg):  # noqa: ANN001
        enqueued.append(session_id_arg)

    monkeypatch.setattr(slack_dispatch, "enqueue_revise", fake_enqueue)

    ephemeral_calls: list[str] = []

    async def fake_post_ephemeral(self, channel, user_id, text):  # noqa: ANN001
        ephemeral_calls.append(text)

    monkeypatch.setattr(SlackClient, "post_ephemeral", fake_post_ephemeral)

    response = post_slack(opinion_submit_payload(mr_token(), "이미 revising 중일 때 의견"))

    # F4: view_submission always returns an empty body (Slack's contract) —
    # the outcome is observed via the ephemeral notice + unchanged state, not
    # the response payload.
    assert response.status_code == 200
    assert response.json() == {}
    assert ephemeral_calls
    assert not enqueued
    row = _session_row(settings, session_id)
    assert row["status"] == REVISING
    # Opinion is still recorded even though the CAS lock was already held.
    assert len(_opinion_rows(settings, session_id)) == 1


# ---------------------------------------------------------------------------
# [13] [의견] rail (P4a) — review-remainder fixups
# ---------------------------------------------------------------------------
def test_opinion_submission_without_gitlab_token_returns_503_and_state_untouched(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    session_id = _seed_session(settings)
    # Guard(b) fails closed with a hard 503 when GITLAB_TOKEN is missing (same
    # posture as the approve rail's step (b) sha re-fetch).
    monkeypatch.setattr(settings, "gitlab_token", None)

    response = post_slack(opinion_submit_payload(mr_token(), "gitlab 토큰 미설정 케이스"))

    assert response.status_code == 503
    row = _session_row(settings, session_id)
    assert row["status"] == REVIEWING  # no CAS was ever attempted
    opinions = _opinion_rows(settings, session_id)
    # Step (a)'s INSERT runs before the guard(b) token check, so the opinion
    # itself is still recorded (audit trail) — but its downstream "state"
    # (applied_round) is left untouched, matching every other guard-fail path.
    assert len(opinions) == 1
    assert opinions[0]["applied_round"] is None


def test_opinion_submission_guard_b_lookup_error_returns_200_empty_body(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    session_id = _seed_session(settings)
    _stub_gitlab_note(monkeypatch)

    async def fake_commits_raises(self, project_id, iid):  # noqa: ANN001
        raise httpx.HTTPError("boom")

    monkeypatch.setattr(GitLabClient, "list_mr_commits", fake_commits_raises)

    enqueued: list[int] = []

    async def fake_enqueue(session_id_arg):  # noqa: ANN001
        enqueued.append(session_id_arg)

    monkeypatch.setattr(slack_dispatch, "enqueue_revise", fake_enqueue)

    ephemeral_calls: list[str] = []

    async def fake_post_ephemeral(self, channel, user_id, text):  # noqa: ANN001
        ephemeral_calls.append(text)

    monkeypatch.setattr(SlackClient, "post_ephemeral", fake_post_ephemeral)

    response = post_slack(opinion_submit_payload(mr_token(), "commits 조회 실패 케이스"))

    assert response.status_code == 200
    assert response.json() == {}
    row = _session_row(settings, session_id)
    assert row["status"] == REVIEWING  # no state transition attempted
    assert not enqueued
    assert ephemeral_calls  # user is told to retry
    opinions = _opinion_rows(settings, session_id)
    assert len(opinions) == 1
    assert opinions[0]["applied_round"] is None


def test_opinion_submission_from_non_reviewer_is_rejected_before_insert(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path)
    session_id = _seed_session(settings)

    ephemeral_calls: list[str] = []

    async def fake_post_ephemeral(self, channel, user_id, text):  # noqa: ANN001
        ephemeral_calls.append(text)

    monkeypatch.setattr(SlackClient, "post_ephemeral", fake_post_ephemeral)

    response = post_slack(opinion_submit_payload(mr_token(), "비담당자 의견", user_id="U_STRANGER"))

    assert response.status_code == 200
    assert response.json() == {}
    assert ephemeral_calls and "권한이 없습니다" in ephemeral_calls[0]
    # Rejected ahead of step (a)'s INSERT — no opinion row at all.
    assert len(_opinion_rows(settings, session_id)) == 0
    row = _session_row(settings, session_id)
    assert row["status"] == REVIEWING
    assert "guard_reject" in _event_kinds(settings, session_id)


def test_opinion_submission_with_empty_reviewer_map_is_rejected(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = configure(monkeypatch, tmp_path, reviewer_map="")
    session_id = _seed_session(settings)

    ephemeral_calls: list[str] = []

    async def fake_post_ephemeral(self, channel, user_id, text):  # noqa: ANN001
        ephemeral_calls.append(text)

    monkeypatch.setattr(SlackClient, "post_ephemeral", fake_post_ephemeral)

    response = post_slack(opinion_submit_payload(mr_token(), "빈 reviewer_map 의견", user_id=AUTHORIZED_USER))

    assert response.status_code == 200
    assert response.json() == {}
    assert ephemeral_calls  # fail-closed: an empty map authorizes nobody
    assert len(_opinion_rows(settings, session_id)) == 0
    row = _session_row(settings, session_id)
    assert row["status"] == REVIEWING


# ---------------------------------------------------------------------------
# _revise_result_payload — "이전 대비 변경점" rendering (summary/diff_stat/compare_url).
# This step only covers rendering; wiring real values in is a follow-up.
# ---------------------------------------------------------------------------
def _revise_mr() -> dict[str, object]:
    return {
        "url": "https://gitlab.example.com/group/project/-/merge_requests/1",
        "iid": 1,
        "title": "Test MR",
        "head_ref": "feature/test",
        "base_ref": "main",
        "author": "author",
        "sha": "abc123",
    }


def test_revise_result_payload_renders_all_three_change_summary_elements() -> None:
    mr = _revise_mr()

    text, blocks = _revise_result_payload(
        mr,
        "token123",
        round_number=2,
        unapplied=[{"reason": "형식이 다름"}],
        summary="이번 라운드 변경 요약입니다.",
        diff_stat="app/foo.py | 4 +++-\n1 file changed, 3 insertions(+), 1 deletion(-)",
        compare_url="https://gitlab.example.com/group/project/-/compare/aaa...bbb",
    )

    assert text == "MR 리뷰 요청 (라운드 2)"
    assert "라운드 2 완료" in blocks[0]["text"]["text"]

    summary_block = blocks[1]
    assert summary_block["type"] == "section"
    assert summary_block["text"]["text"] == "📝 변경 요약\n이번 라운드 변경 요약입니다."

    diff_block = blocks[2]
    assert diff_block["type"] == "section"
    assert diff_block["text"]["text"].startswith("```")
    assert diff_block["text"]["text"].endswith("```")
    assert "1 file changed, 3 insertions(+), 1 deletion(-)" in diff_block["text"]["text"]

    compare_block = blocks[3]
    assert compare_block["type"] == "context"
    assert compare_block["elements"][0]["text"] == (
        "<https://gitlab.example.com/group/project/-/compare/aaa...bbb|이전 대비 변경 보기>"
    )

    # Fixed order: header -> summary -> diff stat -> compare link -> 미반영.
    unapplied_block = blocks[4]
    assert unapplied_block["type"] == "context"
    assert "미반영 의견 1건" in unapplied_block["elements"][0]["text"]


def test_revise_result_payload_omits_change_summary_blocks_when_absent() -> None:
    """No regression: omitting the 3 new kwargs must reproduce the old block shape."""
    mr = _revise_mr()

    text, blocks = _revise_result_payload(mr, "token123", round_number=3, unapplied=[])

    expected_blocks = review_blocks(mr, "token123")
    expected_blocks.insert(
        0,
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "🔄 라운드 3 완료 — 재확인 후 승인해주세요"},
        },
    )
    assert blocks == expected_blocks
    assert text == "MR 리뷰 요청 (라운드 3)"


def test_revise_result_payload_clips_summary_over_700_chars() -> None:
    mr = _revise_mr()
    long_summary = "가" * 710

    _, blocks = _revise_result_payload(
        mr, "token123", round_number=1, unapplied=[], summary=long_summary
    )

    body = blocks[1]["text"]["text"].split("\n", 1)[1]
    assert len(body) == 700
    assert body.endswith("…")
    assert body == long_summary[:699] + "…"


def test_revise_result_payload_clips_diff_stat_over_12_lines() -> None:
    mr = _revise_mr()
    lines = [f"file{i}.py | {i} +" for i in range(1, 21)]  # 20 lines

    _, blocks = _revise_result_payload(
        mr, "token123", round_number=1, unapplied=[], diff_stat="\n".join(lines)
    )

    stat_text = blocks[1]["text"]["text"]
    assert stat_text.startswith("```") and stat_text.endswith("```")
    inner_lines = stat_text[3:-3].split("\n")
    assert inner_lines[:12] == lines[:12]
    assert inner_lines[12] == "…외 8줄"
    assert len(inner_lines) == 13
