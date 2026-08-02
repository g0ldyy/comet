import asyncio

import orjson
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import ValidationError

from comet.api.frontend import frontend_index_response
from comet.api.v1.security import CONFIGURE_SESSION_COOKIE, configure_session_active
from comet.core.capabilities import CapabilityPlanner, CapabilityStateSnapshot
from comet.core.capability_bindings import (
    ensure_playback_capability_states,
    native_instance_credential_material,
)
from comet.core.config_validation import config_check, normalize_validated_config
from comet.core.discovery_sources import effective_discovery_sources
from comet.core.models import (
    USENET_PLAYBACK_PROVIDER_KINDS,
    ConfigModel,
    database,
    settings,
)
from comet.discovery.capabilities import ensure_discovery_capability_states
from comet.playback.registry import build_playback_providers
from comet.playback.tokens import CapabilityCodec
from comet.usenet.access import NativeAccessAuthorizer
from comet.utils.cache import CachePolicies
from comet.utils.http_client import http_client_manager

router = APIRouter()
PRIVATE_NO_CACHE_CONTROL = CachePolicies.no_cache().build()
_MAX_CAPABILITY_TEST_BODY_BYTES = 256 * 1024
_CAPABILITY_TEST_TIMEOUT_SECONDS = 20
_CAPABILITY_TEST_CONCURRENCY = asyncio.Semaphore(2)


def _apply_private_no_cache(response):
    response.headers["Cache-Control"] = PRIVATE_NO_CACHE_CONTROL
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["Vary"] = "Cookie, Accept, Accept-Encoding"


async def _read_bounded_request(request: Request, limit: int) -> bytes | None:
    document = bytearray()
    async for chunk in request.stream():
        if len(document) + len(chunk) > limit:
            return None
        document.extend(chunk)
    return bytes(document)


@router.post(
    "/configure/capabilities/test",
    tags=["Configuration"],
    summary="Test Usenet capabilities",
    description="Runs bounded non-mutating validation for one v2 configuration.",
)
async def test_configure_capabilities(request: Request):
    if settings.CONFIGURE_PAGE_PASSWORD and not await configure_session_active(
        request.cookies.get(CONFIGURE_SESSION_COOKIE)
    ):
        response = JSONResponse(
            {"version": 1, "ok": False, "code": "configure_auth_required"},
            status_code=401,
        )
        _apply_private_no_cache(response)
        return response
    if not settings.USENET_ENABLED or not settings.COMET_CAPABILITY_SECRET:
        response = JSONResponse(
            {"version": 1, "ok": False, "code": "usenet_unavailable"},
            status_code=503,
        )
        _apply_private_no_cache(response)
        return response
    document = await _read_bounded_request(
        request,
        _MAX_CAPABILITY_TEST_BODY_BYTES,
    )
    if document is None:
        response = JSONResponse(
            {
                "version": 1,
                "ok": False,
                "code": "configuration_too_large",
            },
            status_code=413,
        )
        _apply_private_no_cache(response)
        return response
    try:
        payload = orjson.loads(document)
        if not isinstance(payload, dict):
            raise ValueError
        config = ConfigModel.model_validate(payload)
        if config.schemaVersion != 2:
            raise ValueError
        normalized = normalize_validated_config(config.model_dump(mode="python"))
    except (orjson.JSONDecodeError, ValidationError, ValueError):
        response = JSONResponse(
            {"version": 1, "ok": False, "code": "configuration_invalid"},
            status_code=400,
        )
        _apply_private_no_cache(response)
        return response

    target_configuration_id = request.query_params.get("configuration_id")
    provider_configuration_ids = None
    source_configuration_ids = None
    if target_configuration_id is not None:
        enabled_provider_ids = {
            entry["configurationId"]
            for entry in normalized["playbackProviders"]
            if entry["enabled"] and entry["kind"] in USENET_PLAYBACK_PROVIDER_KINDS
        }
        enabled_source_ids = {
            entry["configurationId"]
            for entry in normalized["discoverySources"]
            if entry["enabled"]
        }
        if target_configuration_id in enabled_provider_ids:
            provider_configuration_ids = frozenset({target_configuration_id})
            source_configuration_ids = frozenset()
        elif target_configuration_id in enabled_source_ids:
            provider_configuration_ids = frozenset()
            source_configuration_ids = frozenset({target_configuration_id})
        else:
            response = JSONResponse(
                {
                    "version": 1,
                    "ok": False,
                    "code": "binding_not_testable",
                },
                status_code=400,
            )
            _apply_private_no_cache(response)
            return response

    try:
        async with asyncio.timeout(_CAPABILITY_TEST_TIMEOUT_SECONDS):
            async with _CAPABILITY_TEST_CONCURRENCY:
                session = await http_client_manager.get_session()
                user_session = await http_client_manager.get_user_session()
                codec = CapabilityCodec(settings.COMET_CAPABILITY_SECRET)
                providers = build_playback_providers(
                    normalized,
                    session,
                    user_session=user_session,
                    database=database,
                    eligible_configuration_ids=provider_configuration_ids,
                )
                provider_states, discovery_states = await asyncio.gather(
                    ensure_playback_capability_states(
                        normalized,
                        codec,
                        database,
                        providers,
                        instance_credential_material={
                            "comet_native_usenet": (
                                native_instance_credential_material(
                                    settings.USENET_NATIVE_ACCESS_TOKEN,
                                    settings.USENET_NATIVE_SERVERS,
                                )
                            )
                        },
                        provider_configuration_ids=provider_configuration_ids,
                        force_retest=True,
                    ),
                    ensure_discovery_capability_states(
                        normalized,
                        codec,
                        database,
                        session,
                        user_session=user_session,
                        source_configuration_ids=source_configuration_ids,
                        force_retest=True,
                    ),
                )
    except TimeoutError:
        response = JSONResponse(
            {"version": 1, "ok": False, "code": "validation_timeout"},
            status_code=503,
        )
        _apply_private_no_cache(response)
        return response

    snapshot = CapabilityStateSnapshot(provider_states, discovery_states)
    display_names = {
        entry["configurationId"]: entry.get("displayName") or entry["kind"]
        for entry in (
            *normalized["playbackProviders"],
            *effective_discovery_sources(normalized),
        )
    }
    results = [
        {
            "configuration_id": configuration_id,
            "display_name": display_names[configuration_id],
            "state": state.state,
            "eligible": state.eligible,
            "degraded": state.degraded,
            "error_code": state.error_code,
            "retry_after": state.retry_after,
        }
        for states in (snapshot.providers, snapshot.discovery)
        for configuration_id, state in states.items()
    ]
    if target_configuration_id is None:
        plan = CapabilityPlanner(
            usenet_offered=settings.USENET_ENABLED,
            native_authorizer=NativeAccessAuthorizer(
                settings.USENET_NATIVE_ACCESS_TOKEN
            ),
            native_engine_enabled=settings.USENET_ENGINE_ENABLED,
            native_instance_pool_available=bool(settings.USENET_NATIVE_SERVERS),
            native_user_servers_allowed=settings.USENET_NATIVE_ALLOW_USER_SERVERS,
        ).build(normalized, snapshot)
        reachable_usenet_sources = {
            item.configuration_id
            for item in plan.discovery
            if any(branch.value == "usenet" for branch in item.branches)
        }
        discovery_configuration_ids = set(discovery_states)
        for result in results:
            if (
                result["eligible"]
                and result["configuration_id"] in discovery_configuration_ids
                and result["configuration_id"] not in reachable_usenet_sources
            ):
                result.update(
                    {
                        "state": "plan_incompatible",
                        "eligible": False,
                        "degraded": False,
                        "error_code": "no_compatible_playback_provider",
                        "retry_after": None,
                    }
                )
    response = JSONResponse(
        {
            "version": 1,
            "ok": all(result["eligible"] for result in results),
            "bindings": results,
        }
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
    b64config: str | None = None,
):
    if b64config is not None and not config_check(b64config):
        return RedirectResponse("/configure", status_code=303)

    return frontend_index_response(
        public=True,
        public_base_url=settings.PUBLIC_BASE_URL or str(request.base_url).rstrip("/"),
        indexable=b64config is None,
        custom_header_html=settings.CUSTOM_HEADER_HTML,
    )
