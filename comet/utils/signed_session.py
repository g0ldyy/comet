import base64
import binascii
import hashlib
import hmac
import re
import secrets
import time

_NONCE_PATTERN = re.compile(r"[0-9a-f]{16}")
_SIGNATURE_PATTERN = re.compile(r"[0-9a-f]{64}")
_MAX_TOKEN_LENGTH = 160


def derive_session_secret(password: str, scope: str):
    if type(password) is not str or not password:
        raise ValueError("session password must be a non-empty string")
    if type(scope) is not str or not scope:
        raise ValueError("session scope must be a non-empty string")
    return hashlib.sha256(f"{scope}:{password}".encode("utf-8")).digest()


def encode_signed_session(secret: bytes, ttl: int):
    if type(secret) is not bytes or len(secret) != hashlib.sha256().digest_size:
        raise ValueError("session secret must be exactly 32 bytes")
    if type(ttl) is not int or ttl < 60:
        raise ValueError("session TTL must be an integer of at least 60 seconds")

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
    if type(secret) is not bytes or len(secret) != hashlib.sha256().digest_size:
        raise ValueError("session secret must be exactly 32 bytes")
    if type(token) is not str or not token or len(token) > _MAX_TOKEN_LENGTH:
        return False

    try:
        token.encode("ascii")
        padded = token + ("=" * (-len(token) % 4))
        decoded = base64.b64decode(
            padded,
            altchars=b"-_",
            validate=True,
        ).decode("ascii")
        expires_at_text, nonce, signature = decoded.split(":", 2)
        expires_at = int(expires_at_text)
    except (UnicodeEncodeError, UnicodeDecodeError, ValueError, binascii.Error):
        return False

    if (
        str(expires_at) != expires_at_text
        or _NONCE_PATTERN.fullmatch(nonce) is None
        or _SIGNATURE_PATTERN.fullmatch(signature) is None
    ):
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
