import hashlib
import hmac
import time


def verify_github_signature(body: bytes, signature: str | None, secret: str) -> bool:
    """Verify a GitHub webhook HMAC-SHA256 signature."""

    if signature is None:
        return False

    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    expected = f"sha256={digest}"
    return hmac.compare_digest(expected, signature)


def verify_slack_signature(
    body: bytes,
    timestamp: str | None,
    signature: str | None,
    secret: str,
    *,
    now: int | None = None,
) -> bool:
    """Verify a Slack request signature and reject replayed requests."""

    if timestamp is None or signature is None:
        return False

    try:
        request_time = int(timestamp)
    except ValueError:
        return False

    current_time = int(time.time()) if now is None else now
    if abs(current_time - request_time) > 60 * 5:
        return False

    base = b"v0:" + timestamp.encode("utf-8") + b":" + body
    digest = hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"v0={digest}", signature)
