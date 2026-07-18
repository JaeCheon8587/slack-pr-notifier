import hashlib
import hmac


def verify_github_signature(body: bytes, signature: str | None, secret: str) -> bool:
    """Verify a GitHub webhook HMAC-SHA256 signature."""

    if signature is None:
        return False

    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    expected = f"sha256={digest}"
    return hmac.compare_digest(expected, signature)
