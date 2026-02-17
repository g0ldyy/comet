import hashlib
import hmac
import secrets

from fastapi import APIRouter, Cookie, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from comet.core.config_validation import config_check
from comet.core.models import settings, web_config
from comet.utils.cache import CachePolicies
from comet.utils.signed_session import (encode_signed_session,
                                        verify_signed_session)

router = APIRouter()
templates = Jinja2Templates("comet/templates")
CONFIGURE_SESSION_COOKIE = "configure_session"
CONFIGURE_PAGE_PASSWORD = settings.CONFIGURE_PAGE_PASSWORD
CONFIGURE_PAGE_PASSWORD_ENABLED = bool(CONFIGURE_PAGE_PASSWORD)
CONFIGURE_PAGE_PASSWORD_BYTES = (
    CONFIGURE_PAGE_PASSWORD.encode("utf-8") if CONFIGURE_PAGE_PASSWORD_ENABLED else b""
)
CONFIGURE_SESSION_SECRET = (
    hmac.new(
        settings.ADMIN_DASHBOARD_SESSION_SECRET.encode("utf-8"),
        CONFIGURE_PAGE_PASSWORD_BYTES,
        hashlib.sha256,
    ).digest()
    if CONFIGURE_PAGE_PASSWORD_ENABLED
    else b""
)
CONFIGURE_PAGE_SESSION_TTL = max(60, settings.CONFIGURE_PAGE_SESSION_TTL)
PRIVATE_NO_CACHE_CONTROL = CachePolicies.no_cache().build()


def _encode_configure_session():
    return encode_signed_session(
        secret=CONFIGURE_SESSION_SECRET,
        ttl=CONFIGURE_PAGE_SESSION_TTL,
    )


def _verify_configure_session(configure_session: str | None):
    return verify_signed_session(
        token=configure_session,
        secret=CONFIGURE_SESSION_SECRET,
    )


def _next_url(request: Request):
    return (
        f"{request.url.path}?{request.url.query}"
        if request.url.query
        else request.url.path
    )


def _sanitize_next_url(next_url: str | None):
    if not next_url:
        return "/configure"

    if not next_url.startswith("/") or next_url.startswith("//"):
        return "/configure"

    return next_url


def _apply_private_no_cache(response):
    response.headers["Cache-Control"] = PRIVATE_NO_CACHE_CONTROL
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["Vary"] = "Cookie, Accept, Accept-Encoding"


def _render_configure_login(request: Request, next_url: str, error: str = ""):
    response = templates.TemplateResponse(
        "admin_login.html",
        {
            "request": request,
            "error": error,
            "form_action": "/configure/login",
            "password_label": "Configure Password",
            "password_placeholder": "Enter configure password",
            "next_url": _sanitize_next_url(next_url),
        },
    )
    _apply_private_no_cache(response)
    return response


@router.post(
    "/configure/login",
    tags=["Configuration"],
    summary="Configuration Login",
    description="Authenticates and unlocks the configuration page.",
)
async def configure_login(
    request: Request,
    password: str = Form(..., description="Configuration page password"),
    next_url: str = Form("/configure", alias="next"),
):
    if not CONFIGURE_PAGE_PASSWORD_ENABLED:
        return RedirectResponse(_sanitize_next_url(next_url), status_code=303)

    is_correct = secrets.compare_digest(password, CONFIGURE_PAGE_PASSWORD)
    if not is_correct:
        return _render_configure_login(
            request, next_url=_sanitize_next_url(next_url), error="Invalid password"
        )

    response = RedirectResponse(_sanitize_next_url(next_url), status_code=303)
    response.set_cookie(
        key=CONFIGURE_SESSION_COOKIE,
        value=_encode_configure_session(),
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        max_age=CONFIGURE_PAGE_SESSION_TTL,
    )
    _apply_private_no_cache(response)
    return response


@router.get(
    "/configure",
    tags=["Configuration"],
    summary="Configuration Page",
    description="Renders the configuration page.",
)
@router.get(
    "/{b64config}/configure",
    tags=["Configuration"],
    summary="Configuration Page",
    description="Renders the configuration page with existing configuration.",
)
async def configure(
    request: Request,
    b64config: str = None,
    configure_session: str = Cookie(
        None, description="Configuration page session token"
    ),
):
    if b64config is not None and not config_check(b64config, strict_b64config=True):
        return RedirectResponse("/configure", status_code=303)

    if CONFIGURE_PAGE_PASSWORD_ENABLED and not _verify_configure_session(
        configure_session
    ):
        return _render_configure_login(request, next_url=_next_url(request))

    response = templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "CUSTOM_HEADER_HTML": settings.CUSTOM_HEADER_HTML
            if settings.CUSTOM_HEADER_HTML
            else "",
            "webConfig": web_config,
            "proxyDebridStream": settings.PROXY_DEBRID_STREAM,
            "disableTorrentStreams": settings.DISABLE_TORRENT_STREAMS,
            "stremioApiPrefix": settings.STREMIO_API_PREFIX,
        },
    )

    if CONFIGURE_PAGE_PASSWORD_ENABLED:
        _apply_private_no_cache(response)
    elif settings.HTTP_CACHE_ENABLED:
        response.headers["Cache-Control"] = CachePolicies.configure_page().build()
        response.headers["Vary"] = "Accept, Accept-Encoding"

    return response
