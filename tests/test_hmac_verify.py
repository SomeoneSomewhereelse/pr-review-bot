import hashlib
import hmac

from bot.hmac_verify import verify_signature

SECRET = "test-secret"


def _sign(body: bytes, secret: str = SECRET) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_verify_signature_valid():
    body = b'{"action": "opened"}'
    header = _sign(body)
    assert verify_signature(body, header, SECRET) is True


def test_verify_signature_wrong_secret():
    body = b'{"action": "opened"}'
    header = _sign(body, secret="wrong-secret")
    assert verify_signature(body, header, SECRET) is False


def test_verify_signature_tampered_body():
    body = b'{"action": "opened"}'
    header = _sign(body)
    tampered = b'{"action": "closed"}'
    assert verify_signature(tampered, header, SECRET) is False


def test_verify_signature_missing_header():
    body = b'{"action": "opened"}'
    assert verify_signature(body, None, SECRET) is False


def test_verify_signature_malformed_header():
    body = b'{"action": "opened"}'
    assert verify_signature(body, "not-a-valid-header", SECRET) is False
    assert verify_signature(body, "sha1=deadbeef", SECRET) is False
