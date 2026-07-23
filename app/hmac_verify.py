"""Constant-time verification of GitHub's HMAC-SHA256 webhook signature."""

import hashlib
import hmac


def verify_signature(raw_body: bytes, signature_header: str | None, secret: str) -> bool:
    """Verify the ``X-Hub-Signature-256`` header against the raw request body.

    Returns False if the header is missing or malformed, otherwise compares
    the computed HMAC-SHA256 digest to the provided one in constant time.
    """
    if signature_header is None or not signature_header.startswith("sha256="):
        return False

    expected_digest = signature_header.removeprefix("sha256=")
    computed_digest = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed_digest, expected_digest)
