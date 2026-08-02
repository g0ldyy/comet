import hashlib

from starlette.requests import Request

from comet.core.provider_governor import ProviderGovernor

MAX_LOGIN_FIELD_CHARACTERS = 4_096
LOGIN_RETRY_AFTER_SECONDS = 60
_LOGIN_ATTEMPT_LIMIT = 10
_LOGIN_OPERATIONS = frozenset(("admin_login", "configure_login"))


def valid_login_field(value: object) -> bool:
    if type(value) is not str or not value:
        return False
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return False
    return size <= MAX_LOGIN_FIELD_CHARACTERS and all(
        ord(character) >= 32 and ord(character) != 127 for character in value
    )


async def admit_login_attempt(database, request: Request, operation: str) -> bool:
    if operation not in _LOGIN_OPERATIONS:
        raise ValueError("login operation is invalid")
    client = request.client
    peer = client.host if client is not None and client.host else "unknown"
    scope = hashlib.sha256(
        operation.encode("ascii") + b"\0" + peer.encode("utf-8", errors="replace")
    ).digest()
    permit = await ProviderGovernor(database).acquire_window(
        scope,
        f"api_{operation}",
        limit=_LOGIN_ATTEMPT_LIMIT,
        window_seconds=LOGIN_RETRY_AFTER_SECONDS,
    )
    return permit is not None
