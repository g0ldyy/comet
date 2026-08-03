"""Owner-bound delivery of brokered NZB artifacts."""

import asyncio
import hashlib
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import orjson
from fastapi import APIRouter, Cookie, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from comet.api.v1.security import configure_session_active
from comet.core.capabilities import (
    CapabilityPlan,
    CapabilityPlanner,
    CapabilityStateSnapshot,
)
from comet.core.capability_bindings import (
    ensure_playback_capability_states,
    native_instance_credential_material,
    resolve_capability_options,
)
from comet.core.config_validation import config_check, configuration_url_segment
from comet.core.models import database, settings
from comet.core.provider_governor import ProviderGovernor
from comet.core.sources import (
    REAL_NZB_PROVIDER_KINDS,
    LocatorKind,
    LocatorPolicy,
    NzbArtifactRef,
    ReleaseCandidate,
    ReleaseScope,
    TransportKind,
)
from comet.discovery import build_discovery_adapters
from comet.discovery.capabilities import record_discovery_capability_failure
from comet.discovery.repository import ReleaseDiscoveryRepository
from comet.playback.manager import (
    NzbSourceError,
    artifact_selection_hint,
    broker_nzb_release,
    resolve_nzb_handoff_intent,
)
from comet.playback.presentation import (
    build_provider_options,
    issue_provider_option_capability,
)
from comet.playback.providers.stremio_nntp import (
    StremioNntpHandoff,
    handoff_selector,
    validate_handoff_manifest,
)
from comet.playback.registry import build_playback_providers
from comet.playback.repository import RenderedCandidateIds, RenderedReleaseRepository
from comet.playback.tokens import CapabilityCodec
from comet.usenet.access import NativeAccessAuthorizer
from comet.usenet.engine_client import EngineClient
from comet.usenet.file_selection import (
    FileSelectionError,
    UsenetAsset,
    catalog_archive_volume_groups,
    select_archive_volume_groups,
    select_asset,
)
from comet.usenet.nzb_broker import (
    NzbArtifact,
    NzbBroker,
    NzbBrokerError,
    normalize_nzb_document,
)
from comet.usenet.outbound import OutboundUrlError, fetch_public_nzb
from comet.usenet.provider_exports import (
    NzbProviderExportError,
    NzbProviderExportRepository,
)
from comet.usenet.stremio_nntp_config import validate_handoff_config
from comet.utils.http_client import http_client_manager
from comet.utils.network import get_client_ip

router = APIRouter()
export_router = APIRouter()
_MAX_UPLOAD_BYTES = 16 * 1024 * 1024
_MAX_MULTIPART_BYTES = _MAX_UPLOAD_BYTES + 64 * 1024
_MAX_SELECTION_BYTES = 64 * 1024
_NZB_HANDOFF_REQUESTS_PER_MINUTE = 120
_NZB_IMPORTS_PER_MINUTE = 10


def _codec() -> CapabilityCodec:
    if not settings.USENET_ENABLED or not settings.COMET_CAPABILITY_SECRET:
        raise HTTPException(status_code=503, detail="Usenet is unavailable")
    return CapabilityCodec(settings.COMET_CAPABILITY_SECRET)


def _broker() -> NzbBroker:
    descriptor = Path(settings.USENET_RUNTIME_DIR) / "engine.json"
    return NzbBroker(settings.USENET_ARTIFACT_DIR, database, EngineClient(descriptor))


def _exports() -> NzbProviderExportRepository:
    return NzbProviderExportRepository(database)


def _artifact_grant_from_token(
    token: str, partition: bytes, codec: CapabilityCodec, *, now: int | None = None
) -> str:
    if not token.startswith("na1."):
        raise ValueError("invalid artifact capability")
    value = codec.decode(token, partition=partition, now=now)
    if len(value) != 6 or not isinstance(value[5], bytes) or len(value[5]) != 16:
        raise ValueError("invalid artifact capability")
    return str(uuid.UUID(bytes=value[5]))


async def _enforce_nzb_handoff_rate(
    partition: bytes,
    operation: str,
    identifiers: tuple[str, ...],
) -> None:
    digest = hashlib.sha256(b"comet-nzb-handoff-rate-v1\0")
    digest.update(partition)
    for identifier in identifiers:
        digest.update(uuid.UUID(identifier).bytes)
    permit = await ProviderGovernor(database).acquire_window(
        digest.digest(),
        operation,
        limit=_NZB_HANDOFF_REQUESTS_PER_MINUTE,
        window_seconds=60,
    )
    if permit is None:
        raise HTTPException(
            status_code=429,
            detail="NZB handoff request limit exceeded",
            headers={"Retry-After": "60"},
        )


async def _enforce_nzb_import_rate(partition: bytes) -> None:
    permit = await ProviderGovernor(database).acquire_window(
        partition,
        "nzb_manual_import",
        limit=_NZB_IMPORTS_PER_MINUTE,
        window_seconds=60,
    )
    if permit is None:
        raise HTTPException(
            status_code=429,
            detail="NZB import request limit exceeded",
            headers={"Retry-After": "60"},
        )


def issue_artifact_capability(
    codec: CapabilityCodec, config: dict, grant_id: str
) -> str:
    """Issue the reusable six-hour `na1` handoff for one owner-scoped grant."""
    return codec.encode(
        "na1",
        partition=codec.configuration_partition_for_config(config),
        suffix=[uuid.UUID(grant_id).bytes],
        ttl=6 * 60 * 60,
    )


async def _read_uploaded_nzb(upload: UploadFile) -> bytes:
    chunks = bytearray()
    while True:
        chunk = await upload.read(64 * 1024)
        if not chunk:
            break
        if len(chunks) + len(chunk) > _MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="NZB upload is too large")
        chunks.extend(chunk)
    return _decode_nzb_document(bytes(chunks))


class _MultipartBodyTooLarge(MultiPartException):
    """Internal parser signal that also closes any spooled file handles."""


async def _read_multipart_nzb(request: Request) -> bytes:
    """Parse one file while bounding the body before it is spooled."""
    total_bytes = 0

    async def bounded_stream():
        nonlocal total_bytes
        async for chunk in request.stream():
            total_bytes += len(chunk)
            if total_bytes > _MAX_MULTIPART_BYTES:
                raise _MultipartBodyTooLarge("NZB upload is too large")
            yield chunk

    parser = MultiPartParser(
        request.headers,
        bounded_stream(),
        max_files=1,
        max_fields=8,
        max_part_size=_MAX_MULTIPART_BYTES,
    )
    try:
        form = await parser.parse()
    except _MultipartBodyTooLarge as exc:
        raise HTTPException(status_code=413, detail="NZB upload is too large") from exc
    except MultiPartException as exc:
        raise HTTPException(
            status_code=400,
            detail="Expected exactly one NZB file",
        ) from exc
    try:
        upload = form.get("file")
        if not isinstance(upload, StarletteUploadFile):
            raise HTTPException(
                status_code=400,
                detail="Expected exactly one NZB file",
            )
        return await _read_uploaded_nzb(upload)
    finally:
        await form.close()


async def _read_bounded_json(
    request: Request,
    *,
    maximum_bytes: int,
    detail: str,
    too_large_detail: str = "NZB import is too large",
) -> dict:
    """Read one bounded object without trusting Content-Length alone."""
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > maximum_bytes:
            raise HTTPException(status_code=413, detail=too_large_detail)
        body.extend(chunk)
    try:
        payload = orjson.loads(body)
    except orjson.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=detail) from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail=detail)
    return payload


async def _read_import_json(request: Request) -> dict:
    return await _read_bounded_json(
        request,
        maximum_bytes=_MAX_UPLOAD_BYTES,
        detail="Expected an NZB URL import",
    )


def _decode_nzb_document(document: bytes) -> bytes:
    try:
        return normalize_nzb_document(document, maximum_bytes=_MAX_UPLOAD_BYTES)
    except NzbBrokerError as exc:
        if exc.code == "nzb_gzip_invalid":
            raise HTTPException(status_code=400, detail="Invalid gzip NZB") from exc
        raise HTTPException(status_code=413, detail="NZB upload is too large") from exc


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin is None:
        return True
    if any(ord(character) <= 32 or ord(character) == 127 for character in origin):
        return False
    try:
        parsed = urlsplit(origin)
        request_port = request.url.port
        origin_port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return False
    default_ports = {"http": 80, "https": 443}
    request_scheme = request.url.scheme.lower()
    return (
        parsed.scheme == request_scheme
        and parsed.hostname == request.url.hostname
        and (origin_port if origin_port is not None else default_ports[parsed.scheme])
        == (request_port if request_port is not None else default_ports[request_scheme])
    )


def _asset_catalog(assets: tuple[UsenetAsset, ...]) -> list[dict]:
    """Expose only the engine-verified, digest-bound source catalog."""
    return [
        {
            "asset_id": asset.asset_id.hex(),
            "file_index": asset.file_index,
            "name": asset.relative_path,
            "byte_size": asset.declared_bytes,
            "kind": asset.kind,
        }
        for asset in assets
    ]


def _manual_selection_intent(
    value: object,
    assets: tuple[UsenetAsset, ...],
) -> tuple[tuple[object, ...] | None, UsenetAsset | None]:
    """Accept only a catalog ID or bounded episode hint; infer one target only."""
    if value is not None:
        if isinstance(value, dict) and "assetId" in value:
            asset_id = value["assetId"]
            if (
                not isinstance(asset_id, str)
                or len(asset_id) != 64
                or any(character not in "0123456789abcdef" for character in asset_id)
            ):
                raise ValueError("Manual NZB asset selection is invalid")
            selected_asset_id = bytes.fromhex(asset_id)
            selected = next(
                (asset for asset in assets if asset.asset_id == selected_asset_id),
                None,
            )
            if selected is None or selected.kind in {"par2", "par2_source"}:
                raise ValueError("Manual NZB asset selection is unavailable")
            return (2, selected.asset_id), selected
        if isinstance(value, dict) and {"season", "episode"} <= value.keys():
            season, episode = value["season"], value["episode"]
            if (
                isinstance(season, bool)
                or not isinstance(season, int)
                or not 0 <= season <= 65_535
                or isinstance(episode, bool)
                or not isinstance(episode, int)
                or not 0 <= episode <= 65_535
            ):
                raise ValueError("Manual NZB episode selection is invalid")
            intent = (1, season, episode)
            direct = [asset for asset in assets if asset.kind == "video"]
            archive_groups = catalog_archive_volume_groups(assets)
            selected_targets: list[UsenetAsset] = []
            try:
                selected_targets.append(select_asset(direct, intent))
            except FileSelectionError:
                pass
            try:
                selected_targets.append(
                    select_archive_volume_groups(
                        archive_groups,
                        intent,
                    ).volumes[0]
                )
            except FileSelectionError:
                pass
            if len(selected_targets) != 1:
                return None, None
            return intent, selected_targets[0]
        raise ValueError("Manual NZB selection intent is invalid")

    direct = [asset for asset in assets if asset.kind == "video"]
    archive_groups = catalog_archive_volume_groups(assets)
    target_count = len(direct) + len(archive_groups)
    if target_count != 1:
        return None, None
    selected = direct[0] if direct else archive_groups[0].volumes[0]
    return (2, selected.asset_id), selected


async def _manual_capability_plan(
    config: dict,
    codec: CapabilityCodec,
) -> CapabilityPlan:
    session = await http_client_manager.get_session()
    user_session = await http_client_manager.get_user_session()
    providers = build_playback_providers(
        config,
        session,
        user_session=user_session,
        database=database,
    )
    states = await ensure_playback_capability_states(
        config,
        codec,
        database,
        providers,
        instance_credential_material={
            "comet_native_usenet": native_instance_credential_material(
                settings.USENET_NATIVE_ACCESS_TOKEN,
                settings.USENET_NATIVE_SERVERS,
            )
        },
    )
    plan = CapabilityPlanner(
        usenet_offered=settings.USENET_ENABLED,
        native_authorizer=NativeAccessAuthorizer(settings.USENET_NATIVE_ACCESS_TOKEN),
        native_engine_enabled=settings.USENET_ENGINE_ENABLED,
        native_instance_pool_available=bool(settings.USENET_NATIVE_SERVERS),
        native_user_servers_allowed=settings.USENET_NATIVE_ALLOW_USER_SERVERS,
    ).build(
        config,
        CapabilityStateSnapshot(states, {}),
    )
    return plan


def _manual_provider_options(
    config: dict,
    candidate: ReleaseCandidate,
    plan: CapabilityPlan,
    persisted: RenderedCandidateIds,
    codec: CapabilityCodec,
    partition: bytes,
    selection_intent: tuple[object, ...] | None,
    selected_asset: UsenetAsset | None,
    playback_base_url: str,
    nzb_url: str,
) -> list[dict]:
    entries = {
        entry["configurationId"]: entry for entry in config["playbackProviders"] or ()
    }
    accounts = config["accounts"] or {}
    records: list[dict] = []
    for option in build_provider_options((candidate,), plan):
        entry = entries[option.provider.configuration_id]
        provider_options = resolve_capability_options(entry, accounts)
        record = {
            "configuration_id": option.provider.configuration_id,
            "kind": option.provider.kind,
            "display_name": entry["displayName"],
        }
        if option.provider.kind == "stremio_nntp":
            if selected_asset is None:
                record["selection_required"] = True
            elif selected_asset.kind != "video":
                record["client_handoff_unavailable"] = True
            else:
                stream = StremioNntpHandoff(
                    nzb_url,
                    validate_handoff_config(provider_options),
                    file_idx=selected_asset.file_index,
                ).render()
                stream.update(
                    {
                        "name": (f"[{record['display_name']}] Comet"),
                        "description": candidate.title,
                    }
                )
                record.update(
                    {
                        "delivery": "client_delegated",
                        "stream": stream,
                    }
                )
        elif selection_intent is None:
            record["selection_required"] = True
        else:
            capability = issue_provider_option_capability(
                codec,
                partition=partition,
                option=option,
                persisted=persisted,
                selection_intent=list(selection_intent),
                client="stremio",
            )
            record.update(
                {
                    "delivery": "server_resolved",
                    "url": f"{playback_base_url}/playback/v2/{capability}",
                }
            )
        records.append(record)
    return records


async def _manual_import_response(
    request: Request,
    b64config: str,
    config: dict,
    codec: CapabilityCodec,
    partition: bytes,
    artifact: NzbArtifact,
    assets: tuple[UsenetAsset, ...],
    selection_request: object,
    *,
    candidate_id: str,
    origin_kind: str,
    persist_origin: bool,
    status_code: int,
) -> JSONResponse:
    try:
        selection_intent, selected_asset = _manual_selection_intent(
            selection_request,
            assets,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    capability = issue_artifact_capability(codec, config, artifact.grant_id)
    base_url = settings.PUBLIC_BASE_URL or (
        f"{request.url.scheme}://{request.url.netloc}"
    )
    compact_config = configuration_url_segment(config, b64config)
    playback_base_url = f"{base_url}{settings.STREMIO_API_PREFIX}/{compact_config}"
    nzb_url = f"{playback_base_url}/nzb/v1/{capability}.nzb"
    selection_url = f"{playback_base_url}/nzb/v1/{capability}/select"
    title = (
        selected_asset.relative_path if selected_asset is not None else "Imported NZB"
    )
    candidate = ReleaseCandidate(
        candidate_id=candidate_id,
        media_id=candidate_id,
        scope=ReleaseScope.MOVIE,
        transport=TransportKind.USENET,
        title=title,
        locators=(
            NzbArtifactRef(
                locator_id=f"manual-nzb:{artifact.artifact_sha256}",
                kind=LocatorKind.NZB_ARTIFACT,
                policy=LocatorPolicy(
                    REAL_NZB_PROVIDER_KINDS,
                    owner_configuration_partition=partition,
                    expires_at=int(artifact.expires_at),
                ),
                artifact_sha256=artifact.artifact_sha256,
                manifest_identity=artifact.nm1,
            ),
        ),
        size=(selected_asset.declared_bytes if selected_asset is not None else None),
        source="Manual import",
    )
    if persist_origin:
        await ReleaseDiscoveryRepository(database).persist_manual(
            candidate,
            origin_kind=origin_kind,
            owner_configuration_partition=partition,
        )
    plan = await _manual_capability_plan(config, codec)
    persisted = (
        await RenderedReleaseRepository(database).persist(
            (candidate,),
            owner_configuration_partition=partition,
        )
    )[candidate.candidate_id]
    provider_options = _manual_provider_options(
        config,
        candidate,
        plan,
        persisted,
        codec,
        partition,
        selection_intent,
        selected_asset,
        playback_base_url,
        nzb_url,
    )
    streams = [record["stream"] for record in provider_options if "stream" in record]
    return JSONResponse(
        {
            "candidate_id": candidate.candidate_id,
            "artifact_sha256": artifact.artifact_sha256,
            "nh1": artifact.nh1,
            "nm1": artifact.nm1,
            "assets": _asset_catalog(assets),
            "selection_required": selection_intent is None,
            "selection_url": selection_url,
            "provider_options": provider_options,
            "nzb_url": nzb_url,
            "streams": streams,
        },
        status_code=status_code,
        headers={
            "Cache-Control": "private, no-store",
            "Referrer-Policy": "no-referrer",
        },
    )


@router.post(
    "/{b64config}/nzb/v1/import",
    tags=["Stremio"],
    summary="Import an NZB artifact",
)
async def import_upload(
    request: Request,
    b64config: str,
    configure_session: str | None = Cookie(None),
):
    if not _same_origin(request):
        raise HTTPException(
            status_code=403, detail="Cross-origin NZB import is not allowed"
        )
    if settings.CONFIGURE_PAGE_PASSWORD and not await configure_session_active(
        configure_session
    ):
        raise HTTPException(
            status_code=403, detail="Configure authentication is required"
        )
    config = config_check(b64config)
    if config is None:
        raise HTTPException(status_code=400, detail="Invalid configuration")
    codec = _codec()
    partition = codec.configuration_partition_for_config(config)
    await _enforce_nzb_import_rate(partition)
    try:
        content_type = (
            request.headers.get("content-type", "").partition(";")[0].strip().lower()
        )
        selection_request = None
        if content_type == "multipart/form-data":
            origin_kind = "manual_upload"
            document = await _read_multipart_nzb(request)
        elif content_type == "application/json":
            origin_kind = "manual_url"
            payload = await _read_import_json(request)
            if "url" not in payload:
                raise HTTPException(
                    status_code=400, detail="Expected an NZB URL import"
                )
            selection_request = payload.get("selection_intent")
            try:
                document = _decode_nzb_document(
                    await fetch_public_nzb(payload["url"], max_bytes=_MAX_UPLOAD_BYTES)
                )
            except OutboundUrlError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
        else:
            raise HTTPException(
                status_code=400, detail="Expected multipart NZB or JSON URL import"
            )
        broker = _broker()
        artifact = await broker.ingest_bytes(
            document, owner_configuration_partition=partition
        )
        assets = await broker.catalog_artifact(artifact)
    except NzbBrokerError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    manual_id = f"manual:{uuid.uuid4()}"
    return await _manual_import_response(
        request,
        b64config,
        config,
        codec,
        partition,
        artifact,
        assets,
        selection_request,
        candidate_id=manual_id,
        origin_kind=origin_kind,
        persist_origin=True,
        status_code=201,
    )


@router.post(
    "/{b64config}/nzb/v1/{capability}/select",
    tags=["Stremio"],
    summary="Select an asset from an imported NZB",
)
async def select_imported_asset(
    request: Request,
    b64config: str,
    capability: str,
    configure_session: str | None = Cookie(None),
):
    if not _same_origin(request):
        raise HTTPException(
            status_code=403,
            detail="Cross-origin NZB selection is not allowed",
        )
    if settings.CONFIGURE_PAGE_PASSWORD and not await configure_session_active(
        configure_session
    ):
        raise HTTPException(
            status_code=403,
            detail="Configure authentication is required",
        )
    config = config_check(b64config)
    if config is None:
        raise HTTPException(status_code=400, detail="Invalid configuration")
    codec = _codec()
    partition = codec.configuration_partition_for_config(config)
    payload = await _read_bounded_json(
        request,
        maximum_bytes=_MAX_SELECTION_BYTES,
        detail="Expected a manual NZB selection",
        too_large_detail="NZB selection is too large",
    )
    if not {"candidate_id", "selection_intent"} <= payload.keys():
        raise HTTPException(
            status_code=400,
            detail="Expected a manual NZB selection",
        )
    selection_request = payload["selection_intent"]
    if not isinstance(selection_request, dict) or "assetId" not in selection_request:
        raise HTTPException(
            status_code=400,
            detail="Expected one catalog asset selection",
        )
    candidate_id = payload["candidate_id"]
    try:
        grant_id = _artifact_grant_from_token(capability, partition, codec)
        await _enforce_nzb_handoff_rate(
            partition,
            "nzb_manual_selection",
            (grant_id,),
        )
        broker = _broker()
        artifact = await broker.resolve_granted_artifact(
            grant_id,
            owner_configuration_partition=partition,
        )
        origin_kind = await ReleaseDiscoveryRepository(database).manual_artifact_origin(
            candidate_id,
            artifact.artifact_sha256,
            owner_configuration_partition=partition,
        )
    except (NzbBrokerError, ValueError):
        raise HTTPException(
            status_code=404,
            detail="Manual NZB import is unavailable",
        ) from None
    try:
        assets = await broker.catalog_artifact(artifact)
    except NzbBrokerError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return await _manual_import_response(
        request,
        b64config,
        config,
        codec,
        partition,
        artifact,
        assets,
        selection_request,
        candidate_id=candidate_id,
        origin_kind=origin_kind,
        persist_origin=False,
        status_code=200,
    )


@router.api_route(
    "/{b64config}/nzb/v1/{capability}.nzb",
    methods=["GET", "HEAD"],
    tags=["Stremio"],
    summary="Brokered NZB artifact",
)
async def artifact(b64config: str, capability: str):
    config = config_check(b64config)
    if config is None:
        raise HTTPException(status_code=400, detail="Invalid configuration")
    codec = _codec()
    try:
        partition = codec.configuration_partition_for_config(config)
        grant_id = _artifact_grant_from_token(capability, partition, codec)
        await _enforce_nzb_handoff_rate(
            partition,
            "nzb_artifact",
            (grant_id,),
        )
        reader = await _broker().acquire_granted_artifact(
            grant_id, owner_configuration_partition=partition
        )
    except (NzbBrokerError, ValueError):
        raise HTTPException(
            status_code=404, detail="NZB artifact is unavailable"
        ) from None
    return _artifact_response(reader, f"{grant_id}.nzb")


@router.api_route(
    "/{b64config}/nzb/intent/v2/{capability}.nzb",
    methods=["GET", "HEAD"],
    tags=["Stremio"],
    summary="Lazy owner-bound NZB handoff",
)
async def artifact_intent(request: Request, b64config: str, capability: str):
    """Broker one signed transform without accepting an origin from the client."""
    if not capability.startswith("ni2."):
        raise HTTPException(status_code=404, detail="NZB handoff is unavailable")
    config = config_check(b64config)
    if config is None:
        raise HTTPException(status_code=400, detail="Invalid configuration")
    codec = _codec()
    partition = codec.configuration_partition_for_config(config)
    session = await http_client_manager.get_session()
    user_session = await http_client_manager.get_user_session()
    broker = _broker()
    try:
        resolution = await resolve_nzb_handoff_intent(
            capability,
            config,
            database,
            session,
            client_ip=get_client_ip(request),
        )
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail="NZB handoff is unavailable",
        ) from None
    selector = handoff_selector(
        resolution.release.title,
        resolution.intent.selection_intent,
    )
    if selector is None:
        raise HTTPException(
            status_code=404,
            detail="NZB handoff is unavailable",
        )
    await _enforce_nzb_handoff_rate(
        partition,
        "nzb_intent",
        (
            resolution.intent.candidate_id,
            resolution.intent.provider_configuration_id,
            *resolution.intent.locator_ids,
        ),
    )
    try:
        async with asyncio.timeout(12):
            transformed = await broker_nzb_release(
                resolution.release,
                broker,
                database,
                build_discovery_adapters(
                    config,
                    session,
                    user_session=user_session,
                    database=database,
                    account_partition=partition,
                ),
                provider_configuration_id=(resolution.intent.provider_configuration_id),
                provider_kind="stremio_nntp",
                owner_configuration_partition=partition,
            )
            locators = transformed.locators
            payload = locators[0]["payload"]
            artifact_sha256 = payload["artifact_sha256"]
            artifact = await broker.resolve_owned_artifact(
                artifact_sha256,
                owner_configuration_partition=partition,
            )
            selection_hint = artifact_selection_hint(payload)
            try:
                validate_handoff_manifest(
                    artifact.manifest,
                    selector,
                    selection_hint,
                )
            except ValueError:
                raise HTTPException(
                    status_code=404,
                    detail="NZB handoff is unavailable",
                ) from None
            reader = await broker.acquire_owned_artifact(
                artifact_sha256,
                owner_configuration_partition=partition,
            )
    except NzbSourceError as exc:
        if exc.auth_failed or exc.retryable:
            await record_discovery_capability_failure(
                config,
                codec,
                database,
                exc.source_configuration_id,
                state=("auth_failed" if exc.auth_failed else "transiently_unreachable"),
                error_code=("credentials_rejected" if exc.auth_failed else exc.code),
                retry_after=(
                    None
                    if exc.auth_failed
                    else (exc.retry_after if exc.retry_after is not None else 30)
                ),
            )
        raise HTTPException(
            status_code=404,
            detail="NZB handoff is unavailable",
        ) from None
    except (
        TimeoutError,
        NzbBrokerError,
    ):
        raise HTTPException(
            status_code=404,
            detail="NZB handoff is unavailable",
        ) from None
    return _artifact_response(reader, f"{artifact_sha256}.nzb")


@export_router.api_route(
    "/nzb/export/v1/{capability}.nzb",
    methods=["GET", "HEAD"],
    tags=["Stremio"],
    summary="Provider-scoped NZB export",
)
async def provider_export(capability: str):
    if not capability.startswith("nx1."):
        raise HTTPException(status_code=404, detail="NZB export is unavailable")
    try:
        export = await _exports().resolve(capability.removeprefix("nx1."))
        reader = await _broker().acquire_granted_artifact(
            export.grant_id,
            owner_configuration_partition=export.owner_configuration_partition,
        )
    except (NzbProviderExportError, NzbBrokerError, ValueError):
        raise HTTPException(
            status_code=404, detail="NZB export is unavailable"
        ) from None
    return _artifact_response(reader, f"{export.artifact_sha256}.nzb")


def _artifact_response(reader, filename: str) -> FileResponse:
    response = FileResponse(
        reader.path,
        media_type="application/x-nzb",
        filename=filename,
        background=BackgroundTask(reader.close),
    )
    response.headers["Content-Length"] = str(reader.byte_size)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response
