import base64
import hashlib
import hmac
import json
import time
from typing import Any


class InvalidActionToken(ValueError):
    """Raised when a Slack action token is invalid or expired."""


def create_action_token(data: dict[str, Any], secret: str, *, ttl_seconds: int = 86400) -> str:
    """Create a compact signed token so button state does not require a database."""

    payload = {**data, "exp": int(time.time()) + ttl_seconds}
    encoded = _encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded}.{_encode(signature)}"


def decode_action_token(token: str, secret: str, *, now: int | None = None) -> dict[str, Any]:
    """Verify and decode a signed action token."""

    try:
        encoded, supplied_signature = token.split(".", maxsplit=1)
        expected = hmac.new(
            secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(_decode(supplied_signature), expected):
            raise InvalidActionToken("Invalid token signature")
        payload = json.loads(_decode(encoded))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        if isinstance(error, InvalidActionToken):
            raise
        raise InvalidActionToken("Malformed action token") from error

    if not isinstance(payload, dict) or not isinstance(payload.get("exp"), int):
        raise InvalidActionToken("Malformed action token payload")
    if payload["exp"] < (int(time.time()) if now is None else now):
        raise InvalidActionToken("Action token expired")
    return payload


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)
