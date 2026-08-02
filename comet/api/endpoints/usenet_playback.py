"""Owner-bound v2 Usenet playback capabilities."""

import asyncio
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import mediaflow_proxy.utils.http_utils
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response, StreamingResponse

from comet.api.endpoints.config import config_check
from comet.core.capability_bindings import (
    native_instance_credential_material,
    record_playback_capability_failure,
)
from comet.core.models import database, settings
from comet.core.sources import TORRENT_PROVIDER_KINDS
from comet.debrid.exceptions import DebridLinkGenerationError
from comet.debrid.link_cache import (
    cache_download_link_best_effort,
    get_cached_download_link,
    valid_download_url,
)
from comet.debrid.manager import build_account_key_hash, build_playback_media_id
from comet.discovery import build_discovery_adapters
from comet.discovery.capabilities import record_discovery_capability_failure
from comet.metadata.manager import MetadataScraper
from comet.observability import log
from comet.observability.boundaries import playback_boundary
from comet.observability.context import create_detached_task
from comet.playback.base import (
    REMOTE_PREPARATION_TIMEOUT_SECONDS,
    ProviderRuntimeError,
)
from comet.playback.manager import (
    NzbSourceError,
    PreparedPlaybackIntent,
    broker_nzb_sources,
    create_playback_preparation,
    poll_nzbdav,
    poll_stremthru_newz,
    poll_torbox_usenet,
    prepare_altmount,
    prepare_easynews,
    prepare_native_usenet,
    prepare_nzbdav,
    prepare_stremthru_newz,
    prepare_torbox_usenet,
    remote_download_url,
    resolve_prepared_asset,
)
from comet.playback.preparations import PlaybackPreparationRepository
from comet.playback.tokens import CapabilityCodec
from comet.services.lock import DistributedLock
from comet.services.status_video import build_status_video_response
from comet.services.streaming.manager import (
    custom_handle_stream_request,
)
from comet.services.usenet_operations import usenet_operation_monitor
from comet.usenet.engine_client import (
    EngineArchiveError,
    EngineClient,
    EngineNntpError,
)
from comet.usenet.engine_transport import EngineUnavailable
from comet.usenet.materialized_artifacts import MaterializedArtifactRepository
from comet.usenet.nzb_broker import NzbBroker
from comet.utils.cache import check_etag_match
from comet.utils.http_client import http_client_manager
from comet.utils.network import get_client_ip, get_client_ip_any

router = APIRouter()

_SAFE_FAILURE_REASON = re.compile(r"^[a-z][a-z0-9_]{0,63}$", re.ASCII)
_PLAYBACK_EVENT_MESSAGES = {
    "selected": "Usenet playback selected",
    "preparation_started": "Usenet playback preparation started",
    "preparation_complete": "Usenet playback preparation completed",
    "delivery_started": "Usenet playback delivery started",
    "delivery_ready": "Usenet playback delivery ready",
    "range_rejected": "Usenet playback range rejected",
    "failed": "Usenet playback failed",
}


_NATIVE_RANGE_CHUNK_BYTES = 8 * 1024 * 1024
_NATIVE_INITIAL_RANGE_CHUNK_BYTES = 1024 * 1024
_NATIVE_RANGE_MAX_DIGITS = 19
_NATIVE_RANGE_RETRY_DELAYS = (0.2, 0.5, 1.0)
_REMOTE_PREPARATION_TASKS: dict[str, asyncio.Task[str]] = {}
_PLAYBACK_RESPONSE_HEADERS = {
    "Cache-Control": "private, no-store",
    "Referrer-Policy": "no-referrer",
}
_USENET_FAILURE_STATUS_KEYS = {
    "api_key_invalid": "AUTH_BAD_APIKEY",
    "credentials_rejected": "INVALID_ACCOUNT_OR_PASSWORD",
    "file_selection_ambiguous": "UNPROCESSABLE_ENTITY",
    "file_selection_invalid": "UNPROCESSABLE_ENTITY",
    "native_engine_unavailable": "SERVICE_UNAVAILABLE",
    "nntp_article_missing": "LINK_OFFLINE",
    "nntp_auth_failed": "INVALID_ACCOUNT_OR_PASSWORD",
    "nntp_auth_required": "INVALID_ACCOUNT_OR_PASSWORD",
    "nzb_source_unavailable": "BAD_GATEWAY",
    "provider_error": "BAD_GATEWAY",
    "provider_limit_exhausted": "TOO_MANY_REQUESTS",
    "provider_redirect_invalid": "BAD_GATEWAY",
    "provider_response_invalid": "BAD_GATEWAY",
    "provider_response_too_large": "UNPROCESSABLE_ENTITY",
    "provider_unavailable": "SERVICE_UNAVAILABLE",
}


class NativeRangeError(ValueError):
    """A client-supplied Range that cannot address the materialization."""

    def __init__(self, size: int):
        super().__init__("native Range is invalid")
        self.size = size


def _log_playback_event(
    prepared: PreparedPlaybackIntent,
    event: str,
    *,
    level: str = "PLAYBACK",
    exception: BaseException | None = None,
    **fields: str | float | bool | None,
) -> None:
    message = _PLAYBACK_EVENT_MESSAGES[event]
    payload: dict[str, str | int | float | bool] = {
        "provider_name": prepared.preparation.provider_kind,
        "content_id": prepared.resolution.release.media_id,
    }
    duration_s = fields.get("duration_s")
    if type(duration_s) in {int, float} and duration_s >= 0:
        payload["duration_ms"] = duration_s * 1000
    status = fields.get("status")
    if type(status) is int and status >= 0:
        payload["http_status"] = status
    state = fields.get("state")
    if state in {"failed", "pending", "ready", "reprepare"}:
        payload["preparation_state"] = state
    retryable = fields.get("retryable")
    if type(retryable) is bool:
        payload["retryable"] = retryable
    mode = fields.get("mode")
    if mode == "native":
        payload["playback_mode"] = "native"
    elif mode == "direct_redirect":
        payload["playback_mode"] = "redirect"
    operation = fields.get("operation")
    if isinstance(operation, str) and _SAFE_FAILURE_REASON.fullmatch(operation):
        payload["operation"] = operation
    failure = fields.get("error")
    if event == "failed":
        payload["failure_reason"] = (
            failure
            if isinstance(failure, str) and _SAFE_FAILURE_REASON.fullmatch(failure)
            else "provider_failure"
        )
    emit = (
        log.warning
        if level == "WARNING"
        else log.debug
        if level == "DEBUG"
        else log.info
    )
    emit(
        f"usenet.playback.{event}",
        message,
        exc=exception,
        **payload,
    )


def _log_playback_failure(
    prepared: PreparedPlaybackIntent,
    error: str,
    *,
    retryable: bool,
    operation: str | None = None,
    exception: BaseException | None = None,
) -> None:
    _log_playback_event(
        prepared,
        "failed",
        level="WARNING",
        error=error,
        retryable=retryable,
        operation=operation,
        exception=exception,
    )


@dataclass(frozen=True, slots=True)
class _NativePlaybackTarget:
    source_kind: str
    identity: str
    size: int
    strong_revision: str | None
    etag: str
    media_type: str
    content_disposition: str
    member_path: str


def _torrent_debrid_selection(
    prepared: PreparedPlaybackIntent,
) -> tuple[dict, int | None, int | None]:
    selection = prepared.preparation.selection_intent
    if selection == (0,):
        season = episode = None
    elif (
        len(selection) == 3
        and selection[0] == 1
        and type(selection[1]) is int
        and type(selection[2]) is int
    ):
        season, episode = selection[1], selection[2]
    else:
        raise ValueError("torrent playback selection is invalid")
    locators = [
        locator
        for locator in prepared.resolution.release.locators
        if locator["kind"] == "torrent"
    ]
    if not locators:
        raise ValueError("torrent playback locator is invalid")
    hashes = [locator["payload"]["info_hash"] for locator in locators]
    if any(value != hashes[0] for value in hashes[1:]):
        raise ValueError("torrent playback selection is ambiguous")
    exact = [
        locator
        for locator in locators
        if locator["payload"].get("season_norm", -1)
        == (-1 if season is None else season)
        and locator["payload"].get("episode_norm", -1)
        == (-1 if episode is None else episode)
    ]
    if len(exact) == 1:
        return exact[0], season, episode
    if len(locators) == 1:
        return locators[0], season, episode
    raise ValueError("torrent playback selection is ambiguous")


async def _serve_torrent_debrid(
    request: Request,
    prepared: PreparedPlaybackIntent,
    config: dict,
    session,
    *,
    owner_configuration_partition: bytes,
    client_ip: str,
) -> Response:
    provider = prepared.resolution.provider
    locator, season, episode = _torrent_debrid_selection(prepared)
    payload = locator["payload"]
    info_hash = payload["info_hash"]
    file_index = payload.get("file_index")
    selection_title = payload["selection_title"]
    should_proxy = (
        settings.PROXY_DEBRID_STREAM
        and settings.PROXY_DEBRID_STREAM_PASSWORD == config["debridStreamProxyPassword"]
    )
    link_client_ip = "" if should_proxy else client_ip
    cache_arguments = {
        "debrid_service": provider.descriptor.kind,
        "account_key_hash": build_account_key_hash(provider.account_key),
        "info_hash": info_hash,
        "season": season,
        "episode": episode,
        "selection_key": "" if file_index is None else str(file_index),
        "client_ip": link_client_ip,
    }
    download_url = await get_cached_download_link(
        database,
        **cache_arguments,
    )
    if download_url is None:
        media_type = "movie" if season is None and episode is None else "series"
        video_id = build_playback_media_id(
            prepared.resolution.release.media_id,
            media_type,
            season,
            episode,
        )
        metadata_result = await MetadataScraper(session).fetch_metadata_and_aliases(
            media_type,
            video_id,
        )
        metadata = metadata_result.metadata
        aliases = metadata_result.aliases
        display_title = (
            metadata["title"]
            if metadata is not None
            else prepared.resolution.release.title
        )
        try:
            download_url = await provider.generate_download_link(
                info_hash=info_hash,
                file_index=file_index,
                selection_title=selection_title,
                display_title=display_title,
                video_id=video_id,
                media_only_id=prepared.resolution.release.media_id,
                season=season,
                episode=episode,
                aliases=aliases,
                client_ip=link_client_ip,
            )
        except DebridLinkGenerationError as error:
            return _secure_playback_response(
                build_status_video_response(
                    error.status_keys,
                    default_key="UNKNOWN",
                )
            )
        download_url = valid_download_url(download_url)
        if download_url is None:
            return _secure_playback_response(
                build_status_video_response([], default_key="UNKNOWN")
            )
        await cache_download_link_best_effort(
            database,
            **cache_arguments,
            download_url=download_url,
        )
    if prepared.preparation.state == "pending":
        await PlaybackPreparationRepository(database).mark_ready(
            prepared.preparation.preparation_id,
            owner_configuration_partition=owner_configuration_partition,
            provider_account_partition=prepared.resolution.account_partition,
            target_kind="relay" if should_proxy else "cloud",
            target_ref={
                "info_hash": info_hash,
                "file_index": file_index,
                "season": season,
                "episode": episode,
            },
        )
    if should_proxy:
        response = await custom_handle_stream_request(
            request.method,
            download_url,
            mediaflow_proxy.utils.http_utils.get_proxy_headers(request),
            media_id=prepared.resolution.release.title,
            ip=get_client_ip_any(request)[0],
            source_type="torrent",
            service=prepared.preparation.provider_kind,
        )
        response.comet_source_type = "torrent"
        return _secure_playback_response(response)
    response = RedirectResponse(
        download_url, status_code=307, headers=_PLAYBACK_RESPONSE_HEADERS
    )
    response.comet_source_type = "torrent"
    return response


def _secure_playback_response(response: Response) -> Response:
    response.headers.update(_PLAYBACK_RESPONSE_HEADERS)
    return response


def _native_playback_response(response: Response) -> Response:
    response.comet_playback_mode = "native"
    response.comet_source_type = "usenet"
    return response


def _usenet_redirect_response(url: str) -> RedirectResponse:
    response = RedirectResponse(
        url,
        status_code=307,
        headers=_PLAYBACK_RESPONSE_HEADERS,
    )
    response.comet_source_type = "usenet"
    return response


def _status_playback_response(
    status_key: str,
    *,
    retry_after: int | None = None,
) -> Response:
    response = build_status_video_response(
        [status_key],
        default_key="UNKNOWN",
    )
    if retry_after is not None:
        response.headers["Retry-After"] = str(retry_after)
    response.comet_playback_mode = "status"
    response.comet_source_type = "usenet"
    return _secure_playback_response(response)


def _retryable_playback_response(retry_after: int = 30) -> Response:
    return _status_playback_response(
        "MEDIA_NOT_CACHED_YET",
        retry_after=retry_after,
    )


def _usenet_failure_response(code: str, *, retry_after: int | None = None) -> Response:
    return _status_playback_response(
        _USENET_FAILURE_STATUS_KEYS.get(code, "UNKNOWN"),
        retry_after=retry_after,
    )


def _preparation_state_response(
    state: str,
    *,
    prepared: PreparedPlaybackIntent,
    retry_after: int = 2,
) -> Response:
    """Expose retries only while a provider transition remains unfinished."""
    if state in {"pending", "reprepare"}:
        return _retryable_playback_response(retry_after)
    if state == "failed":
        return _usenet_failure_response(prepared.preparation.target_ref["failure_code"])
    raise ValueError("invalid playback preparation result")


async def _resolved_remote_download_url(prepared) -> str:
    if prepared.preparation.provider_kind not in {
        "stremthru_newz",
        "torbox_usenet",
    }:
        return await remote_download_url(prepared)
    cache_arguments = {
        "debrid_service": prepared.preparation.provider_kind,
        "account_key_hash": prepared.resolution.account_partition.hex(),
        "info_hash": prepared.preparation.preparation_id,
        "season": None,
        "episode": None,
        "selection_key": "",
        "client_ip": "",
    }
    download_url = await get_cached_download_link(database, **cache_arguments)
    if download_url is not None:
        return download_url
    download_url = await remote_download_url(prepared)
    await cache_download_link_best_effort(
        database,
        **cache_arguments,
        download_url=download_url,
    )
    return download_url


def _playback_http_exception(
    status_code: int,
    detail: str,
    *,
    headers: dict[str, str] | None = None,
) -> HTTPException:
    response_headers = dict(_PLAYBACK_RESPONSE_HEADERS)
    if headers is not None:
        response_headers.update(headers)
    return HTTPException(
        status_code=status_code,
        detail=detail,
        headers=response_headers,
    )


def _native_media_metadata(target: dict) -> tuple[str, str]:
    filename = target["relative_path"].rsplit("/", 1)[-1]
    disposition = f"inline; filename*=UTF-8''{quote(filename, safe='')}"
    return "application/octet-stream", disposition


def _native_weak_etag(preparation_id: str, revision: str) -> str:
    """Revalidate one blueprint without claiming a strong byte revision."""
    return f'W/"pa2-{preparation_id}-{revision}"'


def _native_playback_target(prepared: PreparedPlaybackIntent) -> _NativePlaybackTarget:
    target = prepared.preparation.target_ref
    source_kind = target["source_kind"]
    size = target["byte_size"]
    strong_revision = target.get("strong_asset_revision")
    if source_kind == "session":
        identity = target["session_id"]
        revision = target["session_revision"]
    else:
        identity = target["raw_composite_id"]
        revision = target["asset_revision"]
    etag = (
        f'"ar1-{strong_revision}"'
        if strong_revision is not None
        else _native_weak_etag(
            prepared.preparation.preparation_id,
            revision,
        )
    )
    media_type, content_disposition = _native_media_metadata(target)
    return _NativePlaybackTarget(
        source_kind,
        identity,
        size,
        strong_revision,
        etag,
        media_type,
        content_disposition,
        target["relative_path"],
    )


async def _acquire_native_artifact_leases(
    prepared: PreparedPlaybackIntent,
    partition: bytes,
    target: _NativePlaybackTarget,
) -> tuple:
    if target.source_kind != "raw_composite":
        return ()
    return await MaterializedArtifactRepository(
        settings.USENET_ARTIFACT_DIR,
        database,
    ).acquire_for_preparation(
        prepared.preparation.preparation_id,
        owner_configuration_partition=partition,
    )


async def _open_native_reader(
    engine: EngineClient,
    target: _NativePlaybackTarget,
) -> str:
    if target.source_kind == "session":
        return await engine.open_session_reader(target.identity)
    return await engine.open_raw_composite_reader(target.identity)


def _broker() -> NzbBroker:
    return NzbBroker(
        settings.USENET_ARTIFACT_DIR,
        database,
        EngineClient(Path(settings.USENET_RUNTIME_DIR) / "engine.json"),
    )


def _playback_url(b64config: str, capability: str) -> str:
    path = f"{settings.STREMIO_API_PREFIX}/{b64config}/playback/v2/{capability}"
    return f"{settings.PUBLIC_BASE_URL}{path}" if settings.PUBLIC_BASE_URL else path


async def _with_preparation_lock(
    prepared,
    timeout: float,
    operation,
    *,
    wait_timeout: float = 10,
):
    lock = DistributedLock(
        f"usenet-preparation:{prepared.preparation.preparation_id}",
        timeout=timeout,
        database=database,
    )
    if not await lock.acquire(wait_timeout=wait_timeout):
        return "pending"
    try:
        return await lock.run(operation())
    finally:
        await lock.release()


def _discard_remote_preparation_task(
    preparation_id: str,
    task: asyncio.Task[str],
) -> None:
    if _REMOTE_PREPARATION_TASKS.get(preparation_id) is task:
        _REMOTE_PREPARATION_TASKS.pop(preparation_id)


async def _run_remote_preparation(
    prepared,
    partition: bytes,
    prepare,
    poll,
):
    """Share one durable remote-preparation driver between local requests."""
    preparation_id = prepared.preparation.preparation_id
    task = _REMOTE_PREPARATION_TASKS.get(preparation_id)
    if task is None:

        async def drive():
            deadline = time.monotonic() + REMOTE_PREPARATION_TIMEOUT_SECONDS

            async def operation():
                persisted = await PlaybackPreparationRepository(database).resolve(
                    preparation_id,
                    owner_configuration_partition=partition,
                )
                if persisted.state != "pending":
                    return persisted.state
                current = PreparedPlaybackIntent(
                    prepared.resolution,
                    persisted,
                    prepared.capability,
                )

                async def bounded_poll(polling):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return "pending"
                    return await poll(polling, remaining)

                return await _advance_remote_preparation(
                    current,
                    partition,
                    prepare,
                    bounded_poll,
                )

            return await _with_preparation_lock(
                prepared,
                180,
                operation,
                wait_timeout=REMOTE_PREPARATION_TIMEOUT_SECONDS,
            )

        task = create_detached_task(
            drive(),
            name="usenet-remote-preparation",
        )
        _REMOTE_PREPARATION_TASKS[preparation_id] = task
        task.add_done_callback(
            lambda completed: _discard_remote_preparation_task(
                preparation_id,
                completed,
            )
        )
    return await asyncio.shield(task)


async def _broker_nzb_transform(
    prepared,
    partition: bytes,
    config: dict,
    session,
    broker: NzbBroker,
):
    if not any(
        locator["kind"] in {"real_nzb", "easynews_http"}
        for locator in prepared.resolution.release.locators
    ):
        return prepared
    try:
        user_session = await http_client_manager.get_user_session()
        return await broker_nzb_sources(
            prepared,
            broker,
            database,
            build_discovery_adapters(
                config,
                session,
                user_session=user_session,
                database=database,
                account_partition=partition,
            ),
            owner_configuration_partition=partition,
        )
    except NzbSourceError as exc:
        if exc.auth_failed or exc.retryable:
            await record_discovery_capability_failure(
                config,
                CapabilityCodec(settings.COMET_CAPABILITY_SECRET),
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
        raise


async def _advance_remote_preparation(prepared, partition: bytes, prepare, poll):
    """Continue polling a remote job during the request that submitted it."""
    if prepared.preparation.target_ref is None:
        state = await prepare(prepared)
        if state != "pending":
            return state
        preparation = await PlaybackPreparationRepository(database).resolve(
            prepared.preparation.preparation_id,
            owner_configuration_partition=partition,
        )
        if preparation.target_ref is None:
            return "pending"
        prepared = PreparedPlaybackIntent(
            prepared.resolution,
            preparation,
            prepared.capability,
        )
    return await poll(prepared)


async def _advance_nzbdav(
    prepared,
    partition: bytes,
    config: dict,
    session,
):
    async def prepare(current):
        broker = _broker()
        transformed = await _broker_nzb_transform(
            current,
            partition,
            config,
            session,
            broker,
        )
        return await prepare_nzbdav(
            transformed,
            broker,
            database,
            owner_configuration_partition=partition,
        )

    async def poll(current, deadline_seconds):
        return await _poll_nzbdav_interactively(
            current,
            database,
            owner_configuration_partition=partition,
            deadline_seconds=deadline_seconds,
        )

    return await _run_remote_preparation(prepared, partition, prepare, poll)


async def _advance_altmount(prepared, partition: bytes, config: dict, session):
    async def operation():
        broker = _broker()
        transformed = await _broker_nzb_transform(
            prepared,
            partition,
            config,
            session,
            broker,
        )
        return await prepare_altmount(
            transformed,
            broker,
            database,
            owner_configuration_partition=partition,
        )

    return await _with_preparation_lock(prepared, 180, operation)


async def _advance_easynews(prepared, partition: bytes):
    async def operation():
        return await prepare_easynews(
            prepared, database, owner_configuration_partition=partition
        )

    return await _with_preparation_lock(prepared, 30, operation)


async def _advance_stremthru_newz(
    prepared,
    partition: bytes,
    config: dict,
    session,
):
    async def prepare(current):
        broker = _broker()
        transformed = await _broker_nzb_transform(
            current,
            partition,
            config,
            session,
            broker,
        )
        return await prepare_stremthru_newz(
            transformed,
            broker,
            database,
            owner_configuration_partition=partition,
        )

    async def poll(current, deadline_seconds):
        return await _poll_stremthru_interactively(
            current,
            database,
            owner_configuration_partition=partition,
            deadline_seconds=deadline_seconds,
        )

    return await _run_remote_preparation(prepared, partition, prepare, poll)


async def _advance_torbox_usenet(
    prepared,
    partition: bytes,
    config: dict,
    session,
):
    async def prepare(current):
        broker = _broker()
        transformed = await _broker_nzb_transform(
            current,
            partition,
            config,
            session,
            broker,
        )
        return await prepare_torbox_usenet(
            transformed,
            broker,
            database,
            owner_configuration_partition=partition,
        )

    async def poll(current, deadline_seconds):
        return await _poll_torbox_interactively(
            current,
            database,
            owner_configuration_partition=partition,
            deadline_seconds=deadline_seconds,
        )

    return await _run_remote_preparation(prepared, partition, prepare, poll)


async def _poll_torbox_interactively(
    prepared,
    provider_database,
    *,
    owner_configuration_partition: bytes,
    deadline_seconds: float = REMOTE_PREPARATION_TIMEOUT_SECONDS,
    _clock=time.monotonic,
    _sleep=asyncio.sleep,
):
    """Poll at 1/2/4/5-second cadence within one interactive request."""

    async def operation():
        return await poll_torbox_usenet(
            prepared,
            provider_database,
            owner_configuration_partition=owner_configuration_partition,
        )

    return await _poll_provider_interactively(
        operation,
        deadline_seconds=deadline_seconds,
        _clock=_clock,
        _sleep=_sleep,
    )


async def _poll_nzbdav_interactively(
    prepared,
    provider_database,
    *,
    owner_configuration_partition: bytes,
    deadline_seconds: float = REMOTE_PREPARATION_TIMEOUT_SECONDS,
    _clock=time.monotonic,
    _sleep=asyncio.sleep,
):
    """Poll one sealed SAB job under the shared interactive deadline."""

    async def operation():
        return await poll_nzbdav(
            prepared,
            provider_database,
            owner_configuration_partition=owner_configuration_partition,
        )

    return await _poll_provider_interactively(
        operation,
        deadline_seconds=deadline_seconds,
        _clock=_clock,
        _sleep=_sleep,
    )


async def _poll_stremthru_interactively(
    prepared,
    provider_database,
    *,
    owner_configuration_partition: bytes,
    deadline_seconds: float = REMOTE_PREPARATION_TIMEOUT_SECONDS,
    _clock=time.monotonic,
    _sleep=asyncio.sleep,
):
    """Poll StremThru without exceeding the shared interactive deadline."""

    async def operation():
        return await poll_stremthru_newz(
            prepared,
            provider_database,
            owner_configuration_partition=owner_configuration_partition,
        )

    return await _poll_provider_interactively(
        operation,
        deadline_seconds=deadline_seconds,
        _clock=_clock,
        _sleep=_sleep,
    )


async def _poll_provider_interactively(
    operation,
    *,
    deadline_seconds: float,
    _clock,
    _sleep,
):
    deadline = _clock() + deadline_seconds
    delay = 1.0
    while True:
        remaining = deadline - _clock()
        if remaining <= 0:
            return "pending"
        deadline_scope = asyncio.timeout(remaining)
        try:
            async with deadline_scope:
                state = await operation()
        except TimeoutError:
            if deadline_scope.expired():
                return "pending"
            raise
        if state != "pending":
            return state
        remaining = deadline - _clock()
        if remaining <= 0:
            return "pending"
        await _sleep(min(delay, remaining))
        delay = min(delay * 2, 5.0)


async def _advance_native_usenet(
    prepared,
    partition: bytes,
    config: dict,
    session,
):
    async def operation():
        broker = _broker()
        transformed = await _broker_nzb_transform(
            prepared,
            partition,
            config,
            session,
            broker,
        )
        await prepare_native_usenet(
            transformed,
            broker,
            database,
            EngineClient(Path(settings.USENET_RUNTIME_DIR) / "engine.json"),
            owner_configuration_partition=partition,
        )
        return "ready"

    return await _with_preparation_lock(
        prepared,
        180,
        operation,
        wait_timeout=0.1,
    )


def _native_range(request: Request, size: int) -> tuple[int, int, bool]:
    """Parse one RFC 7233 byte range without inventing a partial full response."""
    value = request.headers.get("range")
    if value is None:
        return 0, size - 1, False
    if not value.startswith("bytes=") or "," in value:
        raise NativeRangeError(size)
    range_spec = value[6:]
    if range_spec.count("-") != 1:
        raise NativeRangeError(size)
    start_text, end_text = range_spec.split("-", 1)
    if not start_text and not end_text:
        raise NativeRangeError(size)
    if not start_text:
        if (
            len(end_text) > _NATIVE_RANGE_MAX_DIGITS
            or not end_text.isascii()
            or not end_text.isdigit()
        ):
            raise NativeRangeError(size)
        suffix_length = int(end_text)
        if suffix_length < 1:
            raise NativeRangeError(size)
        suffix_length = min(suffix_length, size)
        return size - suffix_length, size - 1, True
    if (
        len(start_text) > _NATIVE_RANGE_MAX_DIGITS
        or len(end_text) > _NATIVE_RANGE_MAX_DIGITS
        or not start_text.isascii()
        or not start_text.isdigit()
        or (end_text and (not end_text.isascii() or not end_text.isdigit()))
    ):
        raise NativeRangeError(size)
    start = int(start_text)
    if start >= size:
        raise NativeRangeError(size)
    end = min(size - 1, int(end_text)) if end_text else size - 1
    if end < start:
        raise NativeRangeError(size)
    return start, end, True


async def _native_session_body(
    engine: EngineClient,
    identity: str,
    size: int,
    start: int,
    end: int,
    *,
    source_kind: str,
    client_ip: str,
    content_id: str,
    title: str,
    member_path: str,
    reader_lease_id: str | None = None,
    artifact_reader_leases: tuple = (),
):
    """Bridge bounded UDS reads into a backpressured public response stream."""
    if reader_lease_id is None:
        if source_kind == "session":
            reader_lease_id = await engine.open_session_reader(identity)
        else:
            reader_lease_id = await engine.open_raw_composite_reader(identity)
    operation_id = await usenet_operation_monitor.start(
        client_ip=client_ip,
        content_id=content_id,
        title=title,
        member_path=member_path,
        source_kind=source_kind,
        total_bytes=end - start + 1,
    )
    outcome = "completed"
    error_code = None
    try:
        position = start
        while position <= end:
            chunk_size = _NATIVE_RANGE_CHUNK_BYTES
            if position == start:
                chunk_size = min(chunk_size, _NATIVE_INITIAL_RANGE_CHUNK_BYTES)
            chunk_end = min(end, position + chunk_size - 1)
            for attempt in range(len(_NATIVE_RANGE_RETRY_DELAYS) + 1):
                try:
                    if source_kind == "session":
                        body = await usenet_operation_monitor.run_cancellable(
                            operation_id,
                            engine.read_session_range(
                                identity,
                                reader_lease_id,
                                size,
                                position,
                                chunk_end,
                            ),
                        )
                    else:
                        body = await usenet_operation_monitor.run_cancellable(
                            operation_id,
                            engine.read_raw_composite_range(
                                identity,
                                reader_lease_id,
                                size,
                                position,
                                chunk_end,
                            ),
                        )
                    break
                except (EngineArchiveError, EngineNntpError) as exc:
                    if not exc.retryable or attempt == len(_NATIVE_RANGE_RETRY_DELAYS):
                        raise
                    await usenet_operation_monitor.run_cancellable(
                        operation_id,
                        asyncio.sleep(_NATIVE_RANGE_RETRY_DELAYS[attempt]),
                    )
            if len(body) != chunk_end - position + 1:
                raise RuntimeError("native engine returned a truncated session range")
            usenet_operation_monitor.add_bytes(operation_id, len(body))
            yield body
            position = chunk_end + 1
    except asyncio.CancelledError:
        if usenet_operation_monitor.admin_cancelled(operation_id):
            outcome = "cancelled"
            return
        raise
    except Exception:
        outcome = "failed"
        error_code = "native_stream_failed"
        raise
    finally:
        try:
            if reader_lease_id is not None:
                close = (
                    engine.close_session_reader
                    if source_kind == "session"
                    else engine.close_raw_composite_reader
                )
                await asyncio.shield(
                    _close_native_reader_safely(
                        close,
                        identity,
                        reader_lease_id,
                    )
                )
        finally:
            try:
                await asyncio.shield(
                    _close_artifact_reader_leases(artifact_reader_leases)
                )
            finally:
                await asyncio.shield(
                    usenet_operation_monitor.finish(
                        operation_id,
                        outcome=outcome,
                        error_code=error_code,
                    )
                )


async def _close_native_reader_safely(close, identity: str, reader_lease_id: str):
    try:
        await close(identity, reader_lease_id)
    except Exception as error:
        log.warning(
            "usenet.native_reader.close_failed",
            "Usenet native reader close failed",
            exc=error,
        )


async def _close_artifact_reader_leases(artifact_reader_leases: tuple) -> None:
    if artifact_reader_leases:
        results = await asyncio.gather(
            *(reader.close() for reader in artifact_reader_leases),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                log.warning(
                    "usenet.artifact_reader.close_failed",
                    "Usenet artifact reader close failed",
                    exc=result,
                )


@router.api_route(
    "/{b64config}/playback/v2/{capability}",
    methods=["GET", "HEAD"],
    tags=["Stremio"],
    summary="Signed playback capability",
)
@playback_boundary(default_mode="proxy")
async def playback_v2(request: Request, b64config: str, capability: str):
    config = config_check(b64config)
    if config is None:
        raise _playback_http_exception(400, "Invalid configuration")
    if not settings.COMET_CAPABILITY_SECRET:
        raise _playback_http_exception(
            503,
            "Signed playback is unavailable",
        )
    session = await http_client_manager.get_session()
    client_ip = get_client_ip(request)
    prepared: PreparedPlaybackIntent | None = None
    try:
        if capability.startswith("pi2."):
            prepared = await create_playback_preparation(
                capability, config, database, session, client_ip=client_ip
            )
            provider_kind = prepared.preparation.provider_kind
            request.state.comet_source_type = (
                "torrent" if provider_kind in TORRENT_PROVIDER_KINDS else "usenet"
            )
            request.state.comet_content_id = prepared.resolution.release.media_id
            _log_playback_event(
                prepared,
                "selected",
                state=prepared.preparation.state,
            )
            return RedirectResponse(
                _playback_url(b64config, prepared.capability),
                status_code=307,
                headers=_PLAYBACK_RESPONSE_HEADERS,
            )
        if not capability.startswith("pa2."):
            raise ValueError("unknown playback capability")
        prepared = await resolve_prepared_asset(
            capability, config, database, session, client_ip=client_ip
        )
        provider_kind = prepared.preparation.provider_kind
        request.state.comet_source_type = (
            "torrent" if provider_kind in TORRENT_PROVIDER_KINDS else "usenet"
        )
        request.state.comet_content_id = prepared.resolution.release.media_id
        codec = CapabilityCodec(settings.COMET_CAPABILITY_SECRET)
        partition = codec.configuration_partition_for_config(config)
        if prepared.preparation.provider_kind in (
            TORRENT_PROVIDER_KINDS - {"direct_torrent"}
        ):
            return await _serve_torrent_debrid(
                request,
                prepared,
                config,
                session,
                owner_configuration_partition=partition,
                client_ip=client_ip,
            )
        native_session_opened = False
        if prepared.preparation.state == "pending":
            preparation_started = time.monotonic()
            _log_playback_event(prepared, "preparation_started")
            if prepared.preparation.provider_kind == "nzbdav":
                state = await _advance_nzbdav(
                    prepared,
                    partition,
                    config,
                    session,
                )
            elif prepared.preparation.provider_kind == "altmount":
                state = await _advance_altmount(
                    prepared,
                    partition,
                    config,
                    session,
                )
            elif prepared.preparation.provider_kind == "easynews":
                state = await _advance_easynews(prepared, partition)
            elif prepared.preparation.provider_kind == "stremthru_newz":
                state = await _advance_stremthru_newz(
                    prepared,
                    partition,
                    config,
                    session,
                )
            elif prepared.preparation.provider_kind == "comet_native_usenet":
                state = await _advance_native_usenet(
                    prepared,
                    partition,
                    config,
                    session,
                )
            elif prepared.preparation.provider_kind == "torbox_usenet":
                state = await _advance_torbox_usenet(
                    prepared,
                    partition,
                    config,
                    session,
                )
            else:
                raise ValueError("unsupported playback provider")
            _log_playback_event(
                prepared,
                "preparation_complete",
                state=state,
                duration_s=time.monotonic() - preparation_started,
            )
            if state != "ready":
                if state == "failed":
                    prepared = await resolve_prepared_asset(
                        capability,
                        config,
                        database,
                        session,
                        client_ip=client_ip,
                    )
                return _preparation_state_response(state, prepared=prepared)
            native_session_opened = (
                prepared.preparation.provider_kind == "comet_native_usenet"
            )
            prepared = await resolve_prepared_asset(
                capability, config, database, session, client_ip=client_ip
            )
        if prepared.preparation.state != "ready":
            return _preparation_state_response(
                prepared.preparation.state,
                prepared=prepared,
            )
        if prepared.preparation.provider_kind == "comet_native_usenet":
            native_target = _native_playback_target(prepared)
            if check_etag_match(request, native_target.etag):
                return _native_playback_response(
                    Response(
                        status_code=304,
                        headers={
                            "Accept-Ranges": "bytes",
                            "Content-Disposition": native_target.content_disposition,
                            "ETag": native_target.etag,
                            **_PLAYBACK_RESPONSE_HEADERS,
                        },
                    )
                )
            start, end, partial = _native_range(request, native_target.size)
            if (
                partial
                and (if_range := request.headers.get("if-range")) is not None
                and (
                    native_target.strong_revision is None
                    or if_range != native_target.etag
                )
            ):
                start, end, partial = 0, native_target.size - 1, False
            headers = {
                "Accept-Ranges": "bytes",
                "Content-Disposition": native_target.content_disposition,
                "Content-Length": str(end - start + 1),
                "ETag": native_target.etag,
                **_PLAYBACK_RESPONSE_HEADERS,
            }
            if native_target.source_kind == "session":
                headers["X-Comet-Usenet-Salvage"] = (
                    "bounded"
                    if settings.USENET_DEGRADED_PLAYBACK_ENABLED
                    else "disabled"
                )
            if partial:
                headers["Content-Range"] = f"bytes {start}-{end}/{native_target.size}"
            if request.method == "HEAD":
                return _native_playback_response(
                    StreamingResponse(
                        iter(()),
                        status_code=206 if partial else 200,
                        media_type=native_target.media_type,
                        headers=headers,
                    )
                )
            engine = EngineClient(Path(settings.USENET_RUNTIME_DIR) / "engine.json")
            artifact_reader_leases = await _acquire_native_artifact_leases(
                prepared,
                partition,
                native_target,
            )
            try:
                reader_lease_id = await _open_native_reader(engine, native_target)
            except (EngineNntpError, EngineArchiveError) as exc:
                await asyncio.shield(
                    _close_artifact_reader_leases(artifact_reader_leases)
                )
                if native_session_opened or not exc.source_unavailable:
                    raise
                state = await _advance_native_usenet(
                    prepared,
                    partition,
                    config,
                    session,
                )
                if state != "ready":
                    if state == "failed":
                        prepared = await resolve_prepared_asset(
                            capability,
                            config,
                            database,
                            session,
                            client_ip=client_ip,
                        )
                    return _preparation_state_response(state, prepared=prepared)
                native_session_opened = True
                prepared = await resolve_prepared_asset(
                    capability,
                    config,
                    database,
                    session,
                    client_ip=client_ip,
                )
                recreated_target = _native_playback_target(prepared)
                if (
                    recreated_target.size != native_target.size
                    or recreated_target.etag != native_target.etag
                    or recreated_target.media_type != native_target.media_type
                    or recreated_target.content_disposition
                    != native_target.content_disposition
                ):
                    raise ValueError("native media representation changed")
                native_target = recreated_target
                artifact_reader_leases = await _acquire_native_artifact_leases(
                    prepared,
                    partition,
                    native_target,
                )
                try:
                    reader_lease_id = await _open_native_reader(engine, native_target)
                except Exception:
                    await asyncio.shield(
                        _close_artifact_reader_leases(artifact_reader_leases)
                    )
                    raise
            except Exception:
                await asyncio.shield(
                    _close_artifact_reader_leases(artifact_reader_leases)
                )
                raise
            native_content = _native_session_body(
                engine,
                native_target.identity,
                native_target.size,
                start,
                end,
                source_kind=native_target.source_kind,
                client_ip=get_client_ip_any(request)[0],
                content_id=prepared.resolution.release.media_id,
                title=prepared.resolution.release.title,
                member_path=native_target.member_path,
                reader_lease_id=reader_lease_id,
                artifact_reader_leases=artifact_reader_leases,
            )
            if request.method == "GET":
                _log_playback_event(
                    prepared,
                    "delivery_started",
                    mode="native",
                    partial=partial,
                )
            return _native_playback_response(
                StreamingResponse(
                    native_content,
                    status_code=206 if partial else 200,
                    media_type=native_target.media_type,
                    headers=headers,
                )
            )
        download_url = await _resolved_remote_download_url(prepared)
        if request.method == "GET":
            _log_playback_event(
                prepared,
                "delivery_ready",
                mode="direct_redirect",
            )
        return _usenet_redirect_response(download_url)
    except NativeRangeError as exc:
        if prepared is not None:
            _log_playback_event(
                prepared,
                "range_rejected",
                level="DEBUG",
            )
        raise _playback_http_exception(
            416,
            "Requested range is not satisfiable",
            headers={"Content-Range": f"bytes */{exc.size}"},
        ) from None
    except ProviderRuntimeError as exc:
        if prepared is not None:
            _log_playback_failure(
                prepared,
                exc.code,
                retryable=exc.retryable,
                operation="playback_preparation",
                exception=exc,
            )
            if exc.auth_failed:
                await PlaybackPreparationRepository(database).mark_failed(
                    prepared.preparation.preparation_id,
                    owner_configuration_partition=partition,
                    provider_account_partition=prepared.resolution.account_partition,
                    code="credentials_rejected",
                )
            elif exc.terminal:
                await PlaybackPreparationRepository(database).mark_failed(
                    prepared.preparation.preparation_id,
                    owner_configuration_partition=partition,
                    provider_account_partition=prepared.resolution.account_partition,
                    code=exc.code,
                )
            if exc.auth_failed or exc.retryable:
                await record_playback_capability_failure(
                    config,
                    codec,
                    database,
                    prepared.preparation.provider_configuration_id,
                    state=(
                        "auth_failed" if exc.auth_failed else "transiently_unreachable"
                    ),
                    error_code=(
                        "credentials_rejected" if exc.auth_failed else exc.code
                    ),
                    retry_after=(None if exc.auth_failed else exc.retry_after or 30),
                )
        if exc.retryable:
            return _retryable_playback_response(exc.retry_after or 30)
        if exc.auth_failed or exc.terminal:
            raise _playback_http_exception(
                404,
                "Playback capability is unavailable",
            ) from None
        return _status_playback_response("UNKNOWN")
    except NzbSourceError as exc:
        if prepared is not None:
            _log_playback_failure(
                prepared,
                exc.code,
                retryable=exc.retryable,
                operation=exc.operation,
                exception=exc.__cause__ or exc,
            )
        return _usenet_failure_response(exc.code)
    except (EngineUnavailable, EngineNntpError, EngineArchiveError) as exc:
        if isinstance(exc, EngineUnavailable):
            code = "native_engine_unavailable"
            retryable = True
            auth_failed = False
        else:
            code = exc.code
            retryable = exc.retryable
            auth_failed = exc.auth_failed
        if prepared is None:
            return _usenet_failure_response(code)
        if prepared.preparation.provider_kind != "comet_native_usenet":
            raise
        _log_playback_failure(
            prepared,
            code,
            retryable=retryable,
            operation="native_engine",
            exception=exc,
        )
        instance_credential_material = {
            "comet_native_usenet": native_instance_credential_material(
                settings.USENET_NATIVE_ACCESS_TOKEN,
                settings.USENET_NATIVE_SERVERS,
            )
        }
        if auth_failed:
            await PlaybackPreparationRepository(database).mark_failed(
                prepared.preparation.preparation_id,
                owner_configuration_partition=partition,
                provider_account_partition=prepared.resolution.account_partition,
                code="credentials_rejected",
            )
            await record_playback_capability_failure(
                config,
                codec,
                database,
                prepared.preparation.provider_configuration_id,
                state="auth_failed",
                error_code="credentials_rejected",
                retry_after=None,
                instance_credential_material=instance_credential_material,
            )
            return _usenet_failure_response("credentials_rejected")
        if retryable:
            if isinstance(exc, EngineUnavailable):
                retry_after = 30
                await record_playback_capability_failure(
                    config,
                    codec,
                    database,
                    prepared.preparation.provider_configuration_id,
                    state="transiently_unreachable",
                    error_code=code,
                    retry_after=retry_after,
                    instance_credential_material=instance_credential_material,
                )
            else:
                retry_after = 2
            return _usenet_failure_response(code, retry_after=retry_after)
        await PlaybackPreparationRepository(database).mark_failed(
            prepared.preparation.preparation_id,
            owner_configuration_partition=partition,
            provider_account_partition=prepared.resolution.account_partition,
            code=code,
        )
        return _usenet_failure_response(code)
    except ValueError as exc:
        if prepared is not None:
            _log_playback_failure(
                prepared,
                "invalid_playback_state",
                retryable=False,
                operation="playback_preparation",
                exception=exc,
            )
            if prepared.preparation.provider_kind == "comet_native_usenet":
                return _usenet_failure_response("invalid_playback_state")
        raise _playback_http_exception(
            404,
            "Playback capability is unavailable",
        ) from None
