import base64
import binascii
import hashlib
import hmac
import secrets
import time


def encode_signed_session(secret: bytes, ttl: int):
    expires_at = int(time.time()) + ttl
    expires_at_text = str(expires_at)
    signature = hmac.new(
        secret,
        expires_at_text.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    raw_token = f"{expires_at_text}:{signature}".encode("utf-8")
    return base64.urlsafe_b64encode(raw_token).decode("utf-8").rstrip("=")


def verify_signed_session(token: str | None, secret: bytes):
    if not token:
        return False

    try:
        padded = token + ("=" * (-len(token) % 4))
        decoded = base64.urlsafe_b64decode(padded).decode("utf-8")
        expires_at_text, signature = decoded.split(":", 1)
        expires_at = int(expires_at_text)
    except (ValueError, TypeError, binascii.Error):
        return False

    if expires_at <= int(time.time()):
        return False

    expected_signature = hmac.new(
        secret,
        expires_at_text.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return secrets.compare_digest(signature, expected_signature)
