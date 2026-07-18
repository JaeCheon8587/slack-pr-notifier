import asyncio
import hashlib
import hmac
import json
from urllib.parse import urlencode

from httpx import ASGITransport, AsyncClient, Response

from app.action_token import create_action_token
from app.config import get_settings
from app.github_client import GitHubClient
from app.main import app
from app.slack_client import SlackClient

SLACK_SECRET = "test-slack-secret"
ACTION_SECRET = "test-action-secret"
TIMESTAMP = "1000"


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


def pr_token() -> str:
    return create_action_token(
        {
            "repository": "owner/repo",
            "number": 1,
            "title": "Test PR",
            "url": "https://github.com/owner/repo/pull/1",
            "sha": "abc123",
            "head_ref": "feature/test",
            "base_ref": "main",
            "author": "author",
        },
        ACTION_SECRET,
    )


def configure(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = get_settings()
    monkeypatch.setattr(settings, "slack_signing_secret", SLACK_SECRET)
    monkeypatch.setattr(settings, "slack_bot_token", "xoxb-test")
    monkeypatch.setattr(settings, "github_token", "github-test")
    monkeypatch.setattr(settings, "action_token_secret", ACTION_SECRET)
    monkeypatch.setattr("app.security.time.time", lambda: int(TIMESTAMP))
    monkeypatch.setattr("app.action_token.time.time", lambda: int(TIMESTAMP))


def test_yes_submits_approval_and_updates_message(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    configure(monkeypatch)
    submitted: dict[str, object] = {}
    updated: dict[str, object] = {}

    async def submit(self, repository, number, sha, event, body):  # type: ignore[no-untyped-def]
        submitted.update(
            repository=repository, number=number, sha=sha, event=event, body=body
        )
        return {"id": 1}

    async def update(self, channel, message_ts, pr, **kwargs):  # type: ignore[no-untyped-def]
        updated.update(channel=channel, message_ts=message_ts, pr=pr, **kwargs)

    monkeypatch.setattr(GitHubClient, "submit_review", submit)
    monkeypatch.setattr(SlackClient, "update_decision", update)
    payload: dict[str, object] = {
        "type": "block_actions",
        "user": {"id": "U123"},
        "channel": {"id": "C123"},
        "container": {"message_ts": "123.456"},
        "actions": [{"action_id": "approve_pr", "value": pr_token()}],
    }

    response = post_slack(payload)

    assert response.status_code == 200
    assert response.json() == {"accepted": True, "action": "approved"}
    assert submitted["event"] == "APPROVE"
    assert "## Slack PR Review" in str(submitted["body"])
    assert updated["approved"] is True


def test_no_opens_reason_modal(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    configure(monkeypatch)
    opened: dict[str, str] = {}

    async def open_modal(self, trigger_id, metadata):  # type: ignore[no-untyped-def]
        opened.update(trigger_id=trigger_id, metadata=metadata)

    monkeypatch.setattr(SlackClient, "open_rejection_modal", open_modal)
    payload: dict[str, object] = {
        "type": "block_actions",
        "user": {"id": "U123"},
        "channel": {"id": "C123"},
        "container": {"message_ts": "123.456"},
        "trigger_id": "trigger-1",
        "actions": [{"action_id": "request_changes_pr", "value": pr_token()}],
    }

    response = post_slack(payload)

    assert response.status_code == 200
    assert response.json()["action"] == "request_changes_modal_opened"
    assert opened["trigger_id"] == "trigger-1"


def test_reason_submission_requests_changes(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    configure(monkeypatch)
    submitted: dict[str, object] = {}
    updated: dict[str, object] = {}

    async def submit(self, repository, number, sha, event, body):  # type: ignore[no-untyped-def]
        submitted.update(event=event, body=body)
        return {"id": 2}

    async def update(self, channel, message_ts, pr, **kwargs):  # type: ignore[no-untyped-def]
        updated.update(channel=channel, message_ts=message_ts, **kwargs)

    monkeypatch.setattr(GitHubClient, "submit_review", submit)
    monkeypatch.setattr(SlackClient, "update_decision", update)
    metadata = json.dumps(
        {"token": pr_token(), "channel": "C123", "message_ts": "123.456"}
    )
    payload: dict[str, object] = {
        "type": "view_submission",
        "user": {"id": "U123"},
        "view": {
            "callback_id": "request_changes_submission",
            "private_metadata": metadata,
            "state": {
                "values": {
                    "reason_block": {"reason": {"value": "테스트를 추가해 주세요."}}
                }
            },
        },
    }

    response = post_slack(payload)

    assert response.status_code == 200
    assert response.json()["action"] == "changes_requested"
    assert submitted["event"] == "REQUEST_CHANGES"
    assert "테스트를 추가해 주세요." in str(submitted["body"])
    assert updated["approved"] is False
    assert updated["reason"] == "테스트를 추가해 주세요."


def test_rejects_invalid_slack_signature(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    configure(monkeypatch)
    payload: dict[str, object] = {"type": "block_actions", "user": {"id": "U123"}}

    response = post_slack(payload, "v0=invalid")

    assert response.status_code == 401
