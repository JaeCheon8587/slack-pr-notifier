import pytest

from app.action_token import InvalidActionToken, create_action_token, decode_action_token


def test_action_token_round_trip(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("app.action_token.time.time", lambda: 1000)
    token = create_action_token({"repository": "owner/repo", "number": 7}, "secret")

    payload = decode_action_token(token, "secret", now=1001)

    assert payload == {"repository": "owner/repo", "number": 7, "exp": 87400}


def test_action_token_rejects_tampering(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("app.action_token.time.time", lambda: 1000)
    token = create_action_token({"number": 7}, "secret")
    tampered = f"{token[:-1]}{'A' if token[-1] != 'A' else 'B'}"

    with pytest.raises(InvalidActionToken):
        decode_action_token(tampered, "secret", now=1001)


def test_action_token_rejects_expired_token(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("app.action_token.time.time", lambda: 1000)
    token = create_action_token({"number": 7}, "secret", ttl_seconds=10)

    with pytest.raises(InvalidActionToken, match="expired"):
        decode_action_token(token, "secret", now=1011)
