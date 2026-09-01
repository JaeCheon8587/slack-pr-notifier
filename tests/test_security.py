"""Unit tests for app/security.py (A1/A2 fail-closed + non-ASCII safety)."""

import hashlib
import hmac

from pydantic import SecretStr

from app.security import verify_gitlab_token, verify_slack_signature

NON_ASCII_TOKEN = "\xff\xfe\x80secret"


def test_gitlab_token_non_ascii_returns_false_without_raising() -> None:
    """A1: a non-ASCII header must not raise TypeError; it must fail closed."""

    assert verify_gitlab_token(NON_ASCII_TOKEN, "real-secret") is False


def test_gitlab_token_empty_secret_rejects_even_empty_token() -> None:
    """A2: an empty/unset secret must never authenticate, even an empty token."""

    assert verify_gitlab_token("", "") is False
    assert verify_gitlab_token(None, "") is False
    assert verify_gitlab_token("anything", "") is False


def test_gitlab_token_valid_match_still_succeeds() -> None:
    assert verify_gitlab_token("real-secret", "real-secret") is True


def test_gitlab_token_mismatch_returns_false() -> None:
    assert verify_gitlab_token("wrong", "real-secret") is False


def _slack_signature(secret: str, timestamp: str, body: bytes) -> str:
    base = b"v0:" + timestamp.encode() + b":" + body
    digest = hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    return f"v0={digest}"


def test_slack_signature_non_ascii_returns_false_without_raising() -> None:
    """A1: a non-ASCII Slack signature header must not raise TypeError."""

    body = b"payload=%7B%7D"
    result = verify_slack_signature(
        body,
        "1000",
        NON_ASCII_TOKEN,
        "real-secret",
        now=1000,
    )
    assert result is False


def test_slack_signature_empty_secret_rejects() -> None:
    """A2: an empty/unset signing secret must never authenticate."""

    body = b"payload=%7B%7D"
    signature = _slack_signature("", "1000", body)
    assert verify_slack_signature(body, "1000", signature, "", now=1000) is False


def test_slack_signature_valid_match_succeeds() -> None:
    body = b"payload=%7B%7D"
    secret = "real-secret"
    signature = _slack_signature(secret, "1000", body)
    assert verify_slack_signature(body, "1000", signature, secret, now=1000) is True


def test_slack_signature_replay_window_rejects_stale_timestamp() -> None:
    body = b"payload=%7B%7D"
    secret = "real-secret"
    signature = _slack_signature(secret, "1000", body)
    assert verify_slack_signature(body, "1000", signature, secret, now=1000 + 301) is False


def test_slack_signature_none_timestamp_returns_false() -> None:
    body = b"payload=%7B%7D"
    signature = _slack_signature("real-secret", "1000", body)
    assert verify_slack_signature(body, None, signature, "real-secret", now=1000) is False


def test_slack_signature_none_signature_returns_false() -> None:
    body = b"payload=%7B%7D"
    assert verify_slack_signature(body, "1000", None, "real-secret", now=1000) is False


def test_slack_signature_non_numeric_timestamp_returns_false_without_raising() -> None:
    body = b"payload=%7B%7D"
    signature = _slack_signature("real-secret", "abc", body)
    assert verify_slack_signature(body, "abc", signature, "real-secret", now=1000) is False


def test_secretstr_repr_and_str_do_not_leak_value() -> None:
    """A7: SecretStr must mask its value in repr()/str() to prevent trace/log leaks."""

    secret = SecretStr("super-secret-value")
    assert "super-secret-value" not in repr(secret)
    assert "super-secret-value" not in str(secret)
    assert secret.get_secret_value() == "super-secret-value"
