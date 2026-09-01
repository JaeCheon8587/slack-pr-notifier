"""Tests for the GitLab webhook rail (app/gitlab_webhook.py) — v4.1 design.

Covers: token verification (fail-closed), webhook-UUID idempotency, session
creation on open/reopen, `update` being ignored, external merge/close
detection, and the human-push policy branches (reviewing/revising/merging)
including the bot-push no-op.
"""

import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient, Response

import app.gitlab_webhook as gitlab_webhook
import app.ingest as ingest
from app.config import get_settings
from app.db import get_connection, init_db
from app.main import app
from app.state_machine import MANUAL, MERGED, MERGING, REVIEWING, REVISING, cas_transition

SECRET = "test-webhook-secret"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeSlackClient:
    posted: list[dict[str, object]] = []
    withdrawn: list[dict[str, object]] = []
    reissued: list[dict[str, object]] = []

    def __init__(self, token: str) -> None:
        self.token = token

    async def post_mr_message(self, channel, mr, token, review=None):  # noqa: ANN001
        FakeSlackClient.posted.append({"channel": channel, "mr": mr, "token": token})
        return {"ok": True, "channel": channel, "ts": "1111.2222"}

    async def withdraw_buttons(self, channel, message_ts, header_text, reason):  # noqa: ANN001
        FakeSlackClient.withdrawn.append(
            {"channel": channel, "ts": message_ts, "header": header_text, "reason": reason}
        )

    async def reissue_approve_only(self, channel, message_ts, header_text, token):  # noqa: ANN001
        FakeSlackClient.reissued.append(
            {"channel": channel, "ts": message_ts, "header": header_text, "token": token}
        )


@pytest.fixture(autouse=True)
def _reset_fake_slack():
    FakeSlackClient.posted.clear()
    FakeSlackClient.withdrawn.clear()
    FakeSlackClient.reissued.clear()
    yield


@pytest.fixture
def db_settings(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    """Point the middleware at an isolated on-disk SQLite file for this test."""

    settings = get_settings()
    monkeypatch.setattr(settings, "gitlab_webhook_secret", SECRET)
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "slack_bot_token", "xoxb-fake")
    monkeypatch.setattr(settings, "slack_channel_id", "C123")
    monkeypatch.setattr(settings, "action_token_secret", "action-secret")
    monkeypatch.setattr(settings, "reviewer_map", '{"example/proj": "U1"}')
    monkeypatch.setattr(ingest, "SlackClient", FakeSlackClient)
    return settings


def _conn(settings):  # type: ignore[no-untyped-def]
    conn = get_connection(settings.db_path)
    init_db(conn)
    return conn


def post_webhook(
    payload: dict[str, object],
    event: str,
    request_token: str | None,
    delivery: str | None = "delivery-123",
) -> Response:
    body = json.dumps(payload, separators=(",", ":")).encode()

    async def send() -> Response:
        transport = ASGITransport(app=app)
        headers = {"Content-Type": "application/json", "X-Gitlab-Event": event}
        if delivery is not None:
            headers["X-Gitlab-Webhook-UUID"] = delivery
        if request_token is not None:
            headers["X-Gitlab-Token"] = request_token

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/webhooks/gitlab/mr", content=body, headers=headers)

    return asyncio.run(send())


def _mr_payload(action: str, *, iid: int = 1, sha: str = "sha1", project_id: int = 42) -> dict[str, object]:
    return {
        "object_kind": "merge_request",
        "project": {"id": project_id, "path_with_namespace": "example/proj"},
        "user": {"username": "author"},
        "object_attributes": {
            "action": action,
            "iid": iid,
            "title": "Add middleware",
            "url": "https://gitlab.example.com/example/proj/-/merge_requests/1",
            "source_branch": "feature-x",
            "target_branch": "main",
            "last_commit": {"id": sha},
        },
    }


def _push_payload(
    *,
    project_id: int = 42,
    before: str = "sha1",
    after: str = "sha2",
    user_username: str = "author",
    user_email: str = "author@example.com",
) -> dict[str, object]:
    return {
        "object_kind": "push",
        "project_id": project_id,
        "project": {"id": project_id, "path_with_namespace": "example/proj"},
        "ref": "refs/heads/feature-x",
        "before": before,
        "after": after,
        "checkout_sha": after,
        "user_username": user_username,
        "user_email": user_email,
    }


# ---------------------------------------------------------------------------
# [1] token verification (fail-closed) — unchanged behaviour, new route path
# ---------------------------------------------------------------------------
def test_missing_secret_returns_503(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(get_settings(), "gitlab_webhook_secret", None)

    response = post_webhook({"object_kind": "push"}, "Push Hook", "any-token")

    assert response.status_code == 503


def test_rejects_invalid_token(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(get_settings(), "gitlab_webhook_secret", SECRET)

    response = post_webhook({"object_kind": "push"}, "Push Hook", "invalid")

    assert response.status_code == 401


def test_accepts_authenticated_non_mr_non_push_event(db_settings) -> None:  # type: ignore[no-untyped-def]
    response = post_webhook({"object_kind": "note"}, "Note Hook", SECRET)

    assert response.status_code == 200
    assert response.json() == {"accepted": True, "event": "Note Hook", "ignored": True}


# ---------------------------------------------------------------------------
# [2] webhook-UUID idempotency
# ---------------------------------------------------------------------------
def test_duplicate_delivery_uuid_is_a_no_op(db_settings) -> None:  # type: ignore[no-untyped-def]
    payload = _mr_payload("open")

    first = post_webhook(payload, "Merge Request Hook", SECRET, delivery="dup-uuid")
    second = post_webhook(payload, "Merge Request Hook", SECRET, delivery="dup-uuid")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json().get("duplicate") is True
    # Only the first delivery should have triggered a Slack notification.
    assert len(FakeSlackClient.posted) == 1

    conn = _conn(db_settings)
    rows = conn.execute("SELECT * FROM review_session").fetchall()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# [3] merge_request routing
# ---------------------------------------------------------------------------
def test_open_creates_session_and_notifies(db_settings) -> None:  # type: ignore[no-untyped-def]
    response = post_webhook(_mr_payload("open"), "Merge Request Hook", SECRET)

    assert response.status_code == 200
    assert len(FakeSlackClient.posted) == 1

    conn = _conn(db_settings)
    row = conn.execute("SELECT * FROM review_session").fetchone()
    assert row["project_id"] == "42"
    assert row["mr_iid"] == 1
    assert row["status"] == REVIEWING
    assert row["round"] == 0
    assert row["mr_sha"] == "sha1"
    # Slack coordinates persisted from the (faked) post_mr_message result.
    assert row["slack_channel"] == "C123"
    assert row["slack_ts"] == "1111.2222"


def test_open_with_empty_reviewer_mapping_goes_manual_with_warning(
    db_settings, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(db_settings, "reviewer_map", "")  # no repo has any mapped reviewer
    posted: list[dict[str, object]] = []

    async def fake_call(self, method, payload):  # noqa: ANN001
        posted.append({"method": method, "payload": payload})
        return {"ok": True}

    monkeypatch.setattr(FakeSlackClient, "call", fake_call, raising=False)

    response = post_webhook(_mr_payload("open"), "Merge Request Hook", SECRET)

    assert response.status_code == 200
    assert FakeSlackClient.posted == []  # no MR review message posted
    assert len(posted) == 1
    assert posted[0]["method"] == "chat.postMessage"
    assert "매핑" in posted[0]["payload"]["text"]

    conn = _conn(db_settings)
    row = conn.execute("SELECT * FROM review_session").fetchone()
    assert row["status"] == MANUAL


def test_reopen_reuses_existing_session(db_settings) -> None:  # type: ignore[no-untyped-def]
    post_webhook(_mr_payload("open", sha="sha1"), "Merge Request Hook", SECRET, delivery="d1")
    response = post_webhook(
        _mr_payload("reopen", sha="sha2"), "Merge Request Hook", SECRET, delivery="d2"
    )

    assert response.status_code == 200
    conn = _conn(db_settings)
    rows = conn.execute("SELECT * FROM review_session").fetchall()
    assert len(rows) == 1
    assert rows[0]["mr_sha"] == "sha2"
    assert len(FakeSlackClient.posted) == 2  # notified on both open and reopen


def test_update_action_is_ignored(db_settings) -> None:  # type: ignore[no-untyped-def]
    post_webhook(_mr_payload("open"), "Merge Request Hook", SECRET, delivery="d1")
    FakeSlackClient.posted.clear()

    response = post_webhook(_mr_payload("update"), "Merge Request Hook", SECRET, delivery="d2")

    assert response.status_code == 200
    assert response.json()["ignored"] is True
    assert FakeSlackClient.posted == []  # never re-triggers review-prep

    conn = _conn(db_settings)
    row = conn.execute("SELECT * FROM review_session").fetchone()
    assert row["status"] == REVIEWING  # untouched


def test_external_merge_transitions_session_and_withdraws_buttons(db_settings) -> None:  # type: ignore[no-untyped-def]
    post_webhook(_mr_payload("open"), "Merge Request Hook", SECRET, delivery="d1")

    response = post_webhook(_mr_payload("merge"), "Merge Request Hook", SECRET, delivery="d2")

    assert response.status_code == 200
    assert response.json()["transitioned"] is True
    conn = _conn(db_settings)
    row = conn.execute("SELECT * FROM review_session").fetchone()
    assert row["status"] == MERGED
    assert len(FakeSlackClient.withdrawn) == 1
    assert "머지" in FakeSlackClient.withdrawn[0]["header"] or "머지" in FakeSlackClient.withdrawn[0]["reason"]


def test_external_close_transitions_session_to_manual(db_settings) -> None:  # type: ignore[no-untyped-def]
    post_webhook(_mr_payload("open"), "Merge Request Hook", SECRET, delivery="d1")

    response = post_webhook(_mr_payload("close"), "Merge Request Hook", SECRET, delivery="d2")

    assert response.status_code == 200
    assert response.json()["transitioned"] is True
    conn = _conn(db_settings)
    row = conn.execute("SELECT * FROM review_session").fetchone()
    assert row["status"] == MANUAL
    assert len(FakeSlackClient.withdrawn) == 1


# ---------------------------------------------------------------------------
# [4] push event — human-push policy
# ---------------------------------------------------------------------------
def _seeded_session(db_settings, *, status: str, sha: str = "sha1"):  # type: ignore[no-untyped-def]
    """Open a session (reviewing), then optionally drive it into another state
    via the real CAS transitions, and stamp Slack coordinates directly (as the
    background notify task would have done)."""

    post_webhook(_mr_payload("open", sha=sha), "Merge Request Hook", SECRET, delivery="seed-open")
    conn = _conn(db_settings)
    row = conn.execute("SELECT * FROM review_session").fetchone()
    session_id = row["id"]

    if status == REVISING:
        assert cas_transition(conn, session_id, REVIEWING, REVISING, reason="opinion")
    elif status == MERGING:
        assert cas_transition(conn, session_id, REVIEWING, MERGING, reason="approve")
    elif status != REVIEWING:
        raise ValueError(status)

    conn.close()
    return session_id


def test_push_by_bot_actor_is_ignored(db_settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(db_settings, "bot_username", "bot-ci")
    _seeded_session(db_settings, status=REVIEWING, sha="sha1")

    response = post_webhook(
        _push_payload(before="sha1", after="sha2", user_username="bot-ci", user_email="bot@example.com"),
        "Push Hook",
        SECRET,
        delivery="push-1",
    )

    assert response.status_code == 200
    assert response.json().get("reason") == "bot_actor"
    assert FakeSlackClient.reissued == []
    conn = _conn(db_settings)
    row = conn.execute("SELECT * FROM review_session").fetchone()
    assert row["mr_sha"] == "sha1"  # untouched
    assert row["status"] == REVIEWING


def test_push_while_reviewing_reissues_approve_only_button(db_settings) -> None:  # type: ignore[no-untyped-def]
    _seeded_session(db_settings, status=REVIEWING, sha="sha1")

    response = post_webhook(
        _push_payload(before="sha1", after="sha2"), "Push Hook", SECRET, delivery="push-1"
    )

    assert response.status_code == 200
    assert response.json()["action"] == "reviewing_sha_updated"
    conn = _conn(db_settings)
    row = conn.execute("SELECT * FROM review_session").fetchone()
    assert row["mr_sha"] == "sha2"
    assert row["status"] == REVIEWING
    assert len(FakeSlackClient.reissued) == 1


def test_push_while_revising_transitions_to_manual(db_settings) -> None:  # type: ignore[no-untyped-def]
    _seeded_session(db_settings, status=REVISING, sha="sha1")

    response = post_webhook(
        _push_payload(before="sha1", after="sha2"), "Push Hook", SECRET, delivery="push-1"
    )

    assert response.status_code == 200
    assert response.json()["transitioned"] is True
    conn = _conn(db_settings)
    row = conn.execute("SELECT * FROM review_session").fetchone()
    assert row["status"] == MANUAL
    assert len(FakeSlackClient.withdrawn) == 1


def test_push_while_merging_transitions_to_manual(db_settings) -> None:  # type: ignore[no-untyped-def]
    _seeded_session(db_settings, status=MERGING, sha="sha1")

    response = post_webhook(
        _push_payload(before="sha1", after="sha2"), "Push Hook", SECRET, delivery="push-1"
    )

    assert response.status_code == 200
    assert response.json()["transitioned"] is True
    conn = _conn(db_settings)
    row = conn.execute("SELECT * FROM review_session").fetchone()
    assert row["status"] == MANUAL
    assert len(FakeSlackClient.withdrawn) == 1


def test_push_with_no_active_session_is_ignored(db_settings) -> None:  # type: ignore[no-untyped-def]
    response = post_webhook(
        _push_payload(project_id=999, before="sha1", after="sha2"),
        "Push Hook",
        SECRET,
        delivery="push-1",
    )

    assert response.status_code == 200
    assert response.json()["reason"] == "no_active_session"


# ---------------------------------------------------------------------------
# [5] idempotency insert — regression: only a UNIQUE violation is a duplicate
# ---------------------------------------------------------------------------
def test_non_unique_integrity_error_is_not_mistaken_for_duplicate(db_settings) -> None:  # type: ignore[no-untyped-def]
    """A FOREIGN KEY violation (bogus session_id) must propagate, not be
    absorbed as a "duplicate delivery" no-op — only UNIQUE violations on
    idempotency_key mean that."""

    conn = _conn(db_settings)
    try:
        with pytest.raises(gitlab_webhook.sqlite3.IntegrityError):
            gitlab_webhook._try_idempotency_insert(conn, 999999, "some-uuid", "mr_open")
    finally:
        conn.close()
