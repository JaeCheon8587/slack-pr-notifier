import asyncio
import hashlib
import hmac
import json

from httpx import ASGITransport, AsyncClient, Response

from app.config import get_settings
from app.main import app

SECRET = "test-webhook-secret"


def signature(body: bytes) -> str:
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def post_webhook(
    payload: dict[str, object],
    event: str,
    request_signature: str | None,
) -> Response:
    body = json.dumps(payload, separators=(",", ":")).encode()

    async def send() -> Response:
        transport = ASGITransport(app=app)
        headers = {
            "Content-Type": "application/json",
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": "delivery-123",
        }
        if request_signature is not None:
            headers["X-Hub-Signature-256"] = request_signature

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/webhooks/github", content=body, headers=headers)

    return asyncio.run(send())


def test_accepts_signed_ping(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(get_settings(), "github_webhook_secret", SECRET)
    payload = {"zen": "Keep it logically awesome."}
    body = json.dumps(payload, separators=(",", ":")).encode()

    response = post_webhook(payload, "ping", signature(body))

    assert response.status_code == 200
    assert response.json() == {"accepted": True, "event": "ping"}


def test_accepts_signed_pull_request(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(get_settings(), "github_webhook_secret", SECRET)
    payload: dict[str, object] = {
        "action": "opened",
        "repository": {"full_name": "example/slack-pr-notifier"},
        "pull_request": {
            "number": 1,
            "title": "Add middleware",
            "head": {"sha": "abc123"},
        },
    }
    body = json.dumps(payload, separators=(",", ":")).encode()

    response = post_webhook(payload, "pull_request", signature(body))

    assert response.status_code == 200
    assert response.json() == {
        "accepted": True,
        "event": "pull_request",
        "action": "opened",
        "repository": "example/slack-pr-notifier",
        "pull_request_number": 1,
        "title": "Add middleware",
        "head_sha": "abc123",
    }


def test_rejects_invalid_signature(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(get_settings(), "github_webhook_secret", SECRET)

    response = post_webhook({"zen": "tampered"}, "ping", "sha256=invalid")

    assert response.status_code == 401
