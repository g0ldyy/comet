"""Shared-cookie authentication, same-origin checks, CSRF, and re-auth."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import Cookie, Depends, Header, Request

from comet.api.v1.responses import ApiProblem
from comet.core.models import database, settings
from comet.utils.signed_session import (
    derive_session_secret,
    encode_signed_session,
    verify_signed_session,
)

ADMIN_SESSION_COOKIE = "admin_session"
CONFIGURE_SESSION_COOKIE = "configure_session"
_CSRF_PATTERN = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class SecuritySettings:
    admin_ttl: int
    configure_ttl: int
    admin_secret: bytes
    configure_secret: bytes | None


def _security_settings(config) -> SecuritySettings:
    return SecuritySettings(
        admin_ttl=config.ADMIN_DASHBOARD_SESSION_TTL,
        configure_ttl=config.CONFIGURE_PAGE_SESSION_TTL,
        admin_secret=derive_session_secret(
            config.ADMIN_DASHBOARD_PASSWORD,
            "admin-dashboard",
        ),
        configure_secret=(
            derive_session_secret(config.CONFIGURE_PAGE_PASSWORD, "configure-page")
            if config.CONFIGURE_PAGE_PASSWORD
            else None
        ),
    )


_security = _security_settings(settings)
_ADMIN_SESSION_SECRET = _security.admin_secret
_CONFIGURE_SESSION_SECRET = _security.configure_secret


def configure_security(config) -> None:
    global _ADMIN_SESSION_SECRET, _CONFIGURE_SESSION_SECRET, _security
    _security = _security_settings(config)
    _ADMIN_SESSION_SECRET = _security.admin_secret
    _CONFIGURE_SESSION_SECRET = _security.configure_secret


def admin_session_ttl() -> int:
    return _security.admin_ttl


def configure_session_ttl() -> int:
    return _security.configure_ttl


def create_admin_session() -> str:
    return encode_signed_session(
        secret=_ADMIN_SESSION_SECRET,
        ttl=_security.admin_ttl,
    )


def verify_admin_session(token: str | None) -> bool:
    return verify_signed_session(token=token, secret=_ADMIN_SESSION_SECRET)


def _csrf_token(secret: bytes, session_token: str) -> str:
    return hmac.new(
        secret,
        f"csrf:{session_token}".encode(),
        hashlib.sha256,
    ).hexdigest()


def csrf_token(session_token: str) -> str:
    return _csrf_token(_ADMIN_SESSION_SECRET, session_token)


def create_configure_session() -> str:
    if _CONFIGURE_SESSION_SECRET is None:
        raise ValueError("the configure page is not password protected")
    return encode_signed_session(
        secret=_CONFIGURE_SESSION_SECRET,
        ttl=_security.configure_ttl,
    )


def verify_configure_session(token: str | None) -> bool:
    if _CONFIGURE_SESSION_SECRET is None:
        return True
    return verify_signed_session(token=token, secret=_CONFIGURE_SESSION_SECRET)


def configure_csrf_token(session_token: str) -> str:
    if _CONFIGURE_SESSION_SECRET is None:
        raise ValueError("the configure page is not password protected")
    return _csrf_token(_CONFIGURE_SESSION_SECRET, session_token)


def _session_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


async def _session_revoked(scope: str, token: str) -> bool:
    revoked = await database.fetch_val(
        """
        SELECT 1
        FROM operator_session_revocations
        WHERE token_hash = :token_hash
          AND scope = :scope
          AND expires_at > :now
        """,
        {
            "token_hash": _session_hash(token),
            "scope": scope,
            "now": time.time(),
        },
        force_primary=True,
    )
    return revoked is not None


async def revoke_session(
    *,
    scope: Literal["admin", "configure"],
    token: str,
    ttl: int,
) -> None:
    now = time.time()
    async with database.transaction():
        await database.execute(
            """
            DELETE FROM operator_session_revocations
            WHERE expires_at <= :now
            """,
            {"now": now},
        )
        await database.execute(
            """
            INSERT INTO operator_session_revocations (
                token_hash, scope, expires_at, revoked_at
            ) VALUES (
                :token_hash, :scope, :expires_at, :revoked_at
            )
            ON CONFLICT (token_hash) DO UPDATE SET
                scope = excluded.scope,
                expires_at = excluded.expires_at,
                revoked_at = excluded.revoked_at
            """,
            {
                "token_hash": _session_hash(token),
                "scope": scope,
                "expires_at": now + ttl,
                "revoked_at": now,
            },
        )


def _origin(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return f"{parsed.scheme}://{parsed.hostname.lower()}:{port}"


def require_same_origin(request: Request) -> None:
    supplied = request.headers.get("origin")
    if supplied is None:
        supplied = request.headers.get("referer")
    candidate = _origin(supplied or "")
    allowed = {_origin(str(request.base_url))}
    if settings.PUBLIC_BASE_URL:
        allowed.add(_origin(settings.PUBLIC_BASE_URL))
    allowed.discard(None)
    if candidate is None or candidate not in allowed:
        raise ApiProblem(
            status_code=403,
            code="origin_mismatch",
            message="The request origin is not permitted.",
        )


async def require_admin_session(
    admin_session: Annotated[
        str | None,
        Cookie(alias=ADMIN_SESSION_COOKIE),
    ] = None,
) -> str:
    if not verify_admin_session(admin_session) or await _session_revoked(
        "admin", admin_session
    ):
        raise ApiProblem(
            status_code=401,
            code="authentication_required",
            message="Authentication is required.",
        )
    return admin_session


async def admin_session_active(token: str) -> bool:
    return verify_admin_session(token) and not await _session_revoked("admin", token)


async def configure_session_active(token: str | None) -> bool:
    return verify_configure_session(token) and (
        token is None or not await _session_revoked("configure", token)
    )


def require_csrf(
    request: Request,
    session_token: Annotated[str, Depends(require_admin_session)],
    supplied_token: Annotated[
        str | None,
        Header(alias="X-CSRF-Token"),
    ] = None,
) -> str:
    require_same_origin(request)
    if (
        supplied_token is None
        or _CSRF_PATTERN.fullmatch(supplied_token) is None
        or not secrets.compare_digest(
            supplied_token,
            csrf_token(session_token),
        )
    ):
        raise ApiProblem(
            status_code=403,
            code="csrf_failed",
            message="The CSRF token is missing or invalid.",
        )
    return session_token


async def require_configure_session(
    configure_session: Annotated[
        str | None,
        Cookie(alias=CONFIGURE_SESSION_COOKIE),
    ] = None,
) -> str | None:
    if not verify_configure_session(configure_session) or (
        configure_session is not None
        and await _session_revoked("configure", configure_session)
    ):
        raise ApiProblem(
            status_code=401,
            code="configure_authentication_required",
            message="Configuration-page authentication is required.",
        )
    return configure_session


def require_configure_csrf(
    request: Request,
    session_token: Annotated[
        str | None,
        Depends(require_configure_session),
    ],
    supplied_token: Annotated[
        str | None,
        Header(alias="X-CSRF-Token"),
    ] = None,
) -> str | None:
    require_same_origin(request)
    if _CONFIGURE_SESSION_SECRET is None:
        return None
    if (
        session_token is None
        or supplied_token is None
        or _CSRF_PATTERN.fullmatch(supplied_token) is None
        or not secrets.compare_digest(
            supplied_token,
            configure_csrf_token(session_token),
        )
    ):
        raise ApiProblem(
            status_code=403,
            code="csrf_failed",
            message="The CSRF token is missing or invalid.",
        )
    return session_token
