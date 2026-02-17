import base64
import binascii
import hashlib
import hmac
import secrets
import time


def encode_signed_session(secret: bytes, ttl: int):
    expires_at = int(time.time()) + ttl
    expires_at_text = str(expires_at)
    nonce = secrets.token_hex(8)
    payload = f"{expires_at_text}:{nonce}"
    signature = hmac.new(
        secret,
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    raw_token = f"{payload}:{signature}".encode("utf-8")
    return base64.urlsafe_b64encode(raw_token).decode("utf-8").rstrip("=")


def verify_signed_session(token: str | None, secret: bytes):
    if not token:
        return False

    try:
        padded = token + ("=" * (-len(token) % 4))
        decoded = base64.urlsafe_b64decode(padded).decode("utf-8")
        expires_at_text, nonce, signature = decoded.split(":", 2)
        expires_at = int(expires_at_text)
    except (ValueError, TypeError, binascii.Error):
        return False

    if expires_at <= int(time.time()):
        return False

    payload = f"{expires_at_text}:{nonce}"
    expected_signature = hmac.new(
        secret,
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return secrets.compare_digest(signature, expected_signature)
