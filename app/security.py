import hashlib
import hmac
import time


def verify_gitlab_token(token: str | None, secret: str) -> bool:
    """Verify the secret token sent in GitLab's X-Gitlab-Token header.

    Fail-closed: an empty/missing secret never matches, even against an empty
    token (``hmac.compare_digest("", "")`` is otherwise ``True``). Comparison
    is done on bytes so a non-ASCII header cannot raise ``TypeError`` (which
    ``hmac.compare_digest`` would otherwise do for non-ASCII ``str`` inputs).
    """

    if not secret or token is None:
        return False
    return hmac.compare_digest(token.encode("utf-8"), secret.encode("utf-8"))


def verify_slack_signature(
    body: bytes,
    timestamp: str | None,
    signature: str | None,
    secret: str,
    *,
    now: int | None = None,
) -> bool:
    """Verify a Slack request signature and reject replayed requests.

    Fail-closed on an empty/missing secret (see ``verify_gitlab_token``), and
    compares the signature as bytes to avoid the same non-ASCII ``TypeError``
    surface.
    """

    if not secret or timestamp is None or signature is None:
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
    expected = f"v0={digest}"
    return hmac.compare_digest(expected.encode("utf-8"), signature.encode("utf-8"))
