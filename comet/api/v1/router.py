"""API v1 routes used by the public shell and authenticated dashboard."""

from __future__ import annotations

import base64
import secrets
from typing import Annotated, Any

import orjson
from fastapi import APIRouter, Cookie, Depends, Query, Request
from pydantic import ValidationError

from comet.api.cookie_policy import secure_session_cookie
from comet.api.login_rate_limit import (
    LOGIN_RETRY_AFTER_SECONDS,
    admit_login_attempt,
    valid_login_field,
)
from comet.api.v1.cometnet import router as cometnet_router
from comet.api.v1.contracts import (
    ApiError,
    ApiSuccess,
    AuditEntry,
    AuditPageData,
    ConfiguratorBootstrapData,
    ConfiguratorCapabilities,
    ConfigureSessionData,
    ConfigValidationRequest,
    LoginRequest,
    SessionData,
    SettingsMutationData,
    SettingsMutationRequest,
    SettingsSnapshotData,
    SettingView,
)
from comet.api.v1.logs import router as logs_router
from comet.api.v1.metrics import router as metrics_router
from comet.api.v1.proxy import router as proxy_router
from comet.api.v1.responses import ApiProblem, success_response
from comet.api.v1.scraping import router as scraping_router
from comet.api.v1.security import (
    ADMIN_SESSION_COOKIE,
    CONFIGURE_SESSION_COOKIE,
    admin_session_ttl,
    configure_csrf_token,
    configure_session_ttl,
    create_admin_session,
    create_configure_session,
    csrf_token,
    require_admin_session,
    require_configure_csrf,
    require_configure_session,
    require_csrf,
    require_same_origin,
    revoke_session,
    verify_configure_session,
)
from comet.api.v1.system import router as system_router
from comet.api.v1.usenet import router as usenet_router
from comet.core.live_settings import apply_latest_settings, pending_restart_keys
from comet.core.models import (
    USER_USENET_DISCOVERY_SOURCE_KINDS,
    VALID_DEBRID_SERVICES,
    AppSettings,
    ConfigModel,
    database,
    native_usenet_offered,
    native_usenet_sources,
    settings,
    web_config,
)
from comet.core.operator_settings import (
    deployment_setting_keys,
    effective_settings_payload,
)
from comet.core.operator_store import OperatorSettingsStore
from comet.core.settings_catalog import build_settings_catalog
from comet.core.sources import USENET_PLAYBACK_PROVIDER_KINDS
from comet.observability.logging import current_settings
from comet.services.operator_commands import dispatch_settings_apply
from comet.utils.languages import LANGUAGE_EMOJIS

router = APIRouter(
    prefix="/api/v1",
    responses={
        400: {"model": ApiError},
        401: {"model": ApiError},
        403: {"model": ApiError},
        404: {"model": ApiError},
        409: {"model": ApiError},
        413: {"model": ApiError},
        422: {"model": ApiError},
        429: {"model": ApiError},
        500: {"model": ApiError},
        503: {"model": ApiError},
    },
)
router.include_router(logs_router)
router.include_router(metrics_router)
router.include_router(proxy_router)
router.include_router(scraping_router)
router.include_router(usenet_router)
router.include_router(cometnet_router)
router.include_router(system_router)
_store = OperatorSettingsStore(database)
_ACTOR = "admin"
_MAX_AUDIT_CURSOR_BYTES = 256


def _setting_value(key: str) -> Any:
    if key in AppSettings.model_fields:
        return getattr(settings, key)
    return getattr(current_settings(), key)


def _setting_source(
    key: str,
    *,
    dashboard_keys: set[str],
    generated_keys: frozenset[str],
    environment_keys: frozenset[str],
) -> str:
    if key in dashboard_keys:
        return "dashboard"
    if key in generated_keys:
        return "generated_shared"
    if key in environment_keys:
        return "environment"
    return "default"


async def _settings_snapshot() -> SettingsSnapshotData:
    overrides = await _store.load_overrides()
    revision = await _store.current_revision()
    dashboard_keys = set(overrides)
    environment_keys = deployment_setting_keys()
    generated_keys = effective_settings_payload().generated_keys
    views: list[SettingView] = []
    for entry in build_settings_catalog():
        source = _setting_source(
            entry.key,
            dashboard_keys=dashboard_keys,
            generated_keys=generated_keys,
            environment_keys=environment_keys,
        )
        effective_value = (
            overrides[entry.key]
            if entry.key in overrides
            else _setting_value(entry.key)
        )
        views.append(
            SettingView(
                catalog=entry,
                value=effective_value,
                active_value=_setting_value(entry.key),
                source=source,
            )
        )
    return SettingsSnapshotData(
        stored_revision=revision,
        applied_revision=settings.APPLIED_SETTINGS_REVISION,
        pending_restart_keys=list(pending_restart_keys()),
        settings=views,
    )


def _encode_audit_cursor(changed_at: float, identifier: str) -> str:
    document = orjson.dumps({"changed_at": changed_at, "id": identifier})
    return base64.urlsafe_b64encode(document).decode("ascii").rstrip("=")


def _decode_audit_cursor(cursor: str) -> tuple[float, str]:
    if (
        not cursor
        or len(cursor.encode("utf-8")) > _MAX_AUDIT_CURSOR_BYTES
        or not cursor.isascii()
    ):
        raise ApiProblem(
            status_code=422,
            code="invalid_cursor",
            message="The audit cursor is invalid.",
        )
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = orjson.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
        changed_at = decoded["changed_at"]
        identifier = decoded["id"]
    except (KeyError, TypeError, ValueError, orjson.JSONDecodeError):
        raise ApiProblem(
            status_code=422,
            code="invalid_cursor",
            message="The audit cursor is invalid.",
        ) from None
    if (
        isinstance(changed_at, bool)
        or not isinstance(changed_at, (int, float))
        or changed_at <= 0
        or not isinstance(identifier, str)
        or len(identifier) != 32
        or any(character not in "0123456789abcdef" for character in identifier)
    ):
        raise ApiProblem(
            status_code=422,
            code="invalid_cursor",
            message="The audit cursor is invalid.",
        )
    return float(changed_at), identifier


@router.post(
    "/auth/login",
    response_model=ApiSuccess[SessionData],
    tags=["API v1 Auth"],
)
async def login(request: Request, body: LoginRequest):
    require_same_origin(request)
    if not await admit_login_attempt(database, request, "admin_login"):
        raise ApiProblem(
            status_code=429,
            code="login_rate_limited",
            message="Too many login attempts were made.",
            headers={"Retry-After": str(LOGIN_RETRY_AFTER_SECONDS)},
        )
    if not valid_login_field(body.password) or not secrets.compare_digest(
        body.password.encode("utf-8"),
        settings.ADMIN_DASHBOARD_PASSWORD.encode("utf-8"),
    ):
        raise ApiProblem(
            status_code=401,
            code="invalid_credentials",
            message="The supplied credentials are invalid.",
        )
    session_token = create_admin_session()
    response = success_response(
        request,
        SessionData(
            csrf_token=csrf_token(session_token),
            expires_in=admin_session_ttl(),
        ),
    )
    response.set_cookie(
        key=ADMIN_SESSION_COOKIE,
        value=session_token,
        path="/",
        httponly=True,
        secure=secure_session_cookie(request, settings.PUBLIC_BASE_URL),
        samesite="strict",
        max_age=admin_session_ttl(),
    )
    return response


@router.get(
    "/auth/session",
    response_model=ApiSuccess[SessionData],
    tags=["API v1 Auth"],
)
async def session(
    request: Request,
    session_token: Annotated[str, Depends(require_admin_session)],
):
    return success_response(
        request,
        SessionData(
            csrf_token=csrf_token(session_token),
            expires_in=admin_session_ttl(),
        ),
    )


@router.post(
    "/auth/configure/login",
    response_model=ApiSuccess[ConfigureSessionData],
    tags=["API v1 Auth"],
)
async def configure_login(request: Request, body: LoginRequest):
    require_same_origin(request)
    if not settings.CONFIGURE_PAGE_PASSWORD:
        return success_response(
            request,
            ConfigureSessionData(
                protected=False,
                authenticated=True,
                csrf_token=None,
                expires_in=None,
            ),
        )
    if not await admit_login_attempt(database, request, "configure_login"):
        raise ApiProblem(
            status_code=429,
            code="login_rate_limited",
            message="Too many login attempts were made.",
            headers={"Retry-After": str(LOGIN_RETRY_AFTER_SECONDS)},
        )
    if not valid_login_field(body.password) or not secrets.compare_digest(
        body.password.encode("utf-8"),
        settings.CONFIGURE_PAGE_PASSWORD.encode("utf-8"),
    ):
        raise ApiProblem(
            status_code=401,
            code="invalid_credentials",
            message="The supplied credentials are invalid.",
        )
    session_token = create_configure_session()
    response = success_response(
        request,
        ConfigureSessionData(
            protected=True,
            authenticated=True,
            csrf_token=configure_csrf_token(session_token),
            expires_in=configure_session_ttl(),
        ),
    )
    response.set_cookie(
        key=CONFIGURE_SESSION_COOKIE,
        value=session_token,
        path="/",
        httponly=True,
        secure=secure_session_cookie(request, settings.PUBLIC_BASE_URL),
        samesite="strict",
        max_age=configure_session_ttl(),
    )
    return response


@router.get(
    "/auth/configure/session",
    response_model=ApiSuccess[ConfigureSessionData],
    tags=["API v1 Auth"],
)
async def configure_session(
    request: Request,
    configure_session_token: Annotated[
        str | None,
        Cookie(alias=CONFIGURE_SESSION_COOKIE),
    ] = None,
):
    protected = bool(settings.CONFIGURE_PAGE_PASSWORD)
    authenticated = verify_configure_session(configure_session_token)
    if protected and not authenticated:
        raise ApiProblem(
            status_code=401,
            code="configure_authentication_required",
            message="Configuration-page authentication is required.",
        )
    return success_response(
        request,
        ConfigureSessionData(
            protected=protected,
            authenticated=True,
            csrf_token=(
                configure_csrf_token(configure_session_token)
                if protected and configure_session_token is not None
                else None
            ),
            expires_in=configure_session_ttl() if protected else None,
        ),
    )


@router.get(
    "/configure/bootstrap",
    response_model=ApiSuccess[ConfiguratorBootstrapData],
    tags=["API v1 Configuration"],
)
async def configure_bootstrap(
    request: Request,
    _session_token: Annotated[str | None, Depends(require_configure_session)],
):
    default_configuration = ConfigModel(
        enableTorrent=not settings.DISABLE_TORRENT_STREAMS
    )
    return success_response(
        request,
        ConfiguratorBootstrapData(
            default_configuration=default_configuration,
            capabilities=ConfiguratorCapabilities(
                proxy_debrid_stream=settings.PROXY_DEBRID_STREAM,
                torrent_streams=not settings.DISABLE_TORRENT_STREAMS,
                usenet=settings.USENET_ENABLED,
                native_usenet=native_usenet_offered(settings),
                stremio_api_prefix=settings.STREMIO_API_PREFIX,
            ),
            resolutions=list(default_configuration.resolutions or {}),
            result_formats=web_config["resultFormat"],
            languages=LANGUAGE_EMOJIS,
            debrid_services=list(VALID_DEBRID_SERVICES),
            native_usenet_sources=list(native_usenet_sources(settings)),
            usenet_provider_kinds=sorted(USENET_PLAYBACK_PROVIDER_KINDS),
            usenet_source_kinds=list(USER_USENET_DISCOVERY_SOURCE_KINDS),
        ),
    )


@router.post(
    "/configure/validate",
    response_model=ApiSuccess[ConfigModel],
    response_model_exclude_unset=True,
    tags=["API v1 Configuration"],
)
async def validate_configuration(
    request: Request,
    body: ConfigValidationRequest,
    _session_token: Annotated[str | None, Depends(require_configure_csrf)],
):
    return success_response(
        request,
        body.configuration.model_dump(mode="json", exclude_unset=True),
    )


@router.post(
    "/auth/configure/logout",
    response_model=ApiSuccess[dict[str, bool]],
    tags=["API v1 Auth"],
)
async def configure_logout(
    request: Request,
    _session_token: Annotated[
        str | None,
        Depends(require_configure_csrf),
    ],
):
    if settings.CONFIGURE_PAGE_PASSWORD:
        if _session_token is None:
            raise ApiProblem(
                status_code=401,
                code="configure_authentication_required",
                message="Configuration-page authentication is required.",
            )
        await revoke_session(
            scope="configure",
            token=_session_token,
            ttl=configure_session_ttl(),
        )
        await _store.record_access(
            key="CONFIGURE_SESSION",
            action="session_invalidate",
            actor="configure",
        )
    response = success_response(request, {"authenticated": False})
    response.delete_cookie(
        CONFIGURE_SESSION_COOKIE,
        path="/",
        httponly=True,
        secure=secure_session_cookie(request, settings.PUBLIC_BASE_URL),
        samesite="strict",
    )
    return response


@router.post(
    "/auth/logout",
    response_model=ApiSuccess[dict[str, bool]],
    tags=["API v1 Auth"],
)
async def logout(
    request: Request,
    _session_token: Annotated[str, Depends(require_csrf)],
):
    await revoke_session(
        scope="admin",
        token=_session_token,
        ttl=admin_session_ttl(),
    )
    await _store.record_access(
        key="ADMIN_SESSION",
        action="session_invalidate",
        actor=_ACTOR,
    )
    response = success_response(request, {"authenticated": False})
    response.delete_cookie(
        ADMIN_SESSION_COOKIE,
        path="/",
        httponly=True,
        secure=secure_session_cookie(request, settings.PUBLIC_BASE_URL),
        samesite="strict",
    )
    return response


@router.get(
    "/admin/settings",
    response_model=ApiSuccess[SettingsSnapshotData],
    tags=["API v1 Settings"],
)
async def get_settings(
    request: Request,
    _session_token: Annotated[str, Depends(require_admin_session)],
):
    return success_response(request, await _settings_snapshot())


@router.put(
    "/admin/settings",
    response_model=ApiSuccess[SettingsMutationData],
    tags=["API v1 Settings"],
)
async def update_settings(
    request: Request,
    body: SettingsMutationRequest,
    _session_token: Annotated[str, Depends(require_csrf)],
):
    try:
        result = await _store.save(
            body.updates,
            reset_keys=set(body.reset),
            actor=_ACTOR,
        )
    except ValidationError as exc:
        raise ApiProblem(
            status_code=422,
            code="settings_invalid",
            message="The proposed settings are invalid.",
            details=[
                {
                    "location": [str(part) for part in error["loc"]],
                    "type": error["type"],
                    "message": str(error["msg"]).removeprefix("Value error, "),
                }
                for error in exc.errors(include_input=False, include_url=False)
            ],
        ) from None
    except ValueError:
        raise ApiProblem(
            status_code=422,
            code="settings_invalid",
            message="The proposed settings are invalid.",
        ) from None
    application = await apply_latest_settings(database) if result.changed_keys else None
    if result.changed_keys:
        await dispatch_settings_apply(database)
    return success_response(
        request,
        SettingsMutationData(
            revision=result.revision,
            changed_keys=list(result.changed_keys),
            live_applied_keys=(
                [key for key in result.changed_keys if key in application.live_keys]
                if application
                else []
            ),
            component_reloaded_keys=(
                [
                    key
                    for key in result.changed_keys
                    if key in application.component_keys
                ]
                if application
                else []
            ),
            restart_required_keys=(
                [key for key in result.changed_keys if key in application.restart_keys]
                if application
                else []
            ),
            restart_required=(
                bool(application.restart_keys)
                if application is not None
                else result.restart_required
            ),
        ),
    )


@router.get(
    "/admin/settings/audit",
    response_model=ApiSuccess[AuditPageData],
    tags=["API v1 Settings"],
)
async def settings_audit(
    request: Request,
    _session_token: Annotated[str, Depends(require_admin_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
):
    values: dict[str, Any] = {"limit": limit + 1}
    predicate = ""
    if cursor is not None:
        changed_at, identifier = _decode_audit_cursor(cursor)
        predicate = """
          AND (
              changed_at < :changed_at
              OR (changed_at = :changed_at AND id < :cursor_id)
          )
        """
        values.update({"changed_at": changed_at, "cursor_id": identifier})
    rows = await database.fetch_all(
        f"""
        SELECT
            id, revision, key, action, previous_source,
            next_source, changed_at, changed_by
        FROM operator_settings_audit
        WHERE 1 = 1
        {predicate}
        ORDER BY changed_at DESC, id DESC
        LIMIT :limit
        """,
        values,
        force_primary=True,
    )
    page_rows = rows[:limit]
    next_cursor = None
    if len(rows) > limit and page_rows:
        last = page_rows[-1]
        next_cursor = _encode_audit_cursor(last["changed_at"], last["id"])
    return success_response(
        request,
        AuditPageData(
            items=[AuditEntry(**dict(row)) for row in page_rows],
            next_cursor=next_cursor,
        ),
    )
