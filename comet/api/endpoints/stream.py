import time
import uuid
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Request

from comet.core.capability_bindings import resolve_capability_options
from comet.core.config_validation import config_check
from comet.core.models import database, settings
from comet.core.sources import (
    SERVER_USENET_PROVIDER_KINDS,
    LocatorKind,
    ReleaseCandidate,
)
from comet.debrid.manager import get_debrid_extension
from comet.observability import metrics
from comet.playback.presentation import issue_nzb_handoff_capability
from comet.playback.providers.stremio_nntp import (
    StremioNntpProvider,
    handoff_selector,
)
from comet.playback.tokens import CapabilityCodec
from comet.services.media_search import (
    MediaSearchResult,
    MediaSearchStatus,
    search_media,
)
from comet.services.trackers import trackers
from comet.usenet.engine_client import EngineClient
from comet.usenet.nzb_broker import NzbBroker, NzbBrokerError
from comet.utils.cache import CachePolicies, cached_json_response
from comet.utils.formatting import (
    format_chilllink,
    format_title,
    get_formatted_components,
    get_formatted_components_plain,
)
from comet.utils.network import get_client_ip

streams = APIRouter()
STREMIO_API_PREFIX = settings.STREMIO_API_PREFIX

RESOLUTION_TO_DIMENSIONS = {
    "4K": (2160, 3840),
    "2160P": (2160, 3840),
    "1440P": (1440, 2560),
    "1080P": (1080, 1920),
    "720P": (720, 1280),
    "576P": (576, 720),
    "480P": (480, 640),
    "360P": (360, 480),
    "240P": (240, 320),
}


def _first_meta_value(value):
    if isinstance(value, list):
        return value[0] if value else ""
    return "" if value is None else value


def _build_kodi_meta(parsed, formatted_components: dict):
    resolution_value = parsed.resolution
    resolution = str(resolution_value).upper() if resolution_value else ""
    height, width = RESOLUTION_TO_DIMENSIONS.get(resolution, (0, 0))
    languages = parsed.languages

    return {
        "width": width,
        "height": height,
        "resolution": resolution,
        "codec": _first_meta_value(parsed.codec),
        "hdr": _first_meta_value(parsed.hdr),
        "audio": _first_meta_value(parsed.audio),
        "channels": _first_meta_value(parsed.channels),
        "language": languages[0] if languages else "",
        "languages": languages,
        "title": formatted_components.get("title", ""),
        "videoInfo": formatted_components.get("video", ""),
        "audioInfo": formatted_components.get("audio", ""),
        "qualityInfo": formatted_components.get("quality", ""),
        "groupInfo": formatted_components.get("group", ""),
        "seedersInfo": formatted_components.get("seeders", ""),
        "sizeInfo": formatted_components.get("size", ""),
        "trackerInfo": formatted_components.get("tracker", ""),
        "languagesInfo": formatted_components.get("languages", ""),
    }


def _stream_notice_name(kodi: bool, emoji_name: str, plain_name: str):
    return plain_name if kodi else emoji_name


def _render_discovery_diagnostics(
    diagnostics: tuple[str, ...],
    *,
    configure_url: str,
    kodi: bool,
) -> list[dict]:
    """Turn bounded safe capability failures into guided configuration rows."""
    streams = []
    for diagnostic in diagnostics:
        streams.append(
            {
                "name": _stream_notice_name(
                    kodi,
                    "[⚠️] Comet setup",
                    "[WARN] Comet setup",
                ),
                "description": (
                    f"{diagnostic.rstrip('.')}. "
                    "Open the addon configuration to review this connection."
                ),
                "url": configure_url,
            }
        )
    return streams


def _build_stream_name(
    kodi: bool,
    service: str,
    resolution: object,
    icon: str = "",
    formatted_components: dict | None = None,
    seeders: int | None = None,
    status: str = "",
):
    resolution = str(resolution).strip() if resolution else ""
    if resolution.casefold() == "unknown":
        resolution = ""
    if not kodi:
        return " ".join(
            part for part in (f"[{service}{icon}]", "Comet", resolution) if part
        )

    prefix = " ".join(
        part for part in (f"[{f'{service} {status}'.strip()}]", resolution) if part
    )

    if formatted_components is None:
        return prefix

    details = [
        formatted_components.get("size", "").removeprefix("Size: "),
        f"S:{seeders}" if seeders is not None else "",
        formatted_components.get("video", ""),
        formatted_components.get("audio", ""),
        formatted_components.get("quality", ""),
        formatted_components.get("group", ""),
    ]
    details = [d for d in details if d]
    return f"{prefix} | {' | '.join(details)}" if details else prefix


def _format_release(
    parsed,
    title: str,
    *,
    seeders: int | None,
    size: int | None,
    source: str,
    result_format: list | tuple,
    kodi: bool,
) -> tuple[dict, str]:
    formatter = get_formatted_components_plain if kodi else get_formatted_components
    components = formatter(
        parsed,
        title,
        seeders,
        size,
        source,
        result_format,
    )
    return components, format_title(components)


def _release_behavior_hints(
    *,
    binge_group: str,
    filename: str,
    size: int | None,
    parsed,
    formatted_components: dict,
    kodi: bool,
) -> dict:
    hints = {
        "bingeGroup": binge_group,
        "filename": filename,
    }
    if size is not None:
        hints["videoSize"] = size
    if kodi and parsed is not None:
        hints["cometKodiMetaV1"] = _build_kodi_meta(
            parsed,
            formatted_components,
        )
    return hints


def _candidate_presentation(
    candidate: ReleaseCandidate,
    config: dict,
    *,
    kodi: bool,
):
    parsed = candidate.parsed
    title = candidate.title
    size = candidate.size
    source = candidate.source
    result_format = config["resultFormat"]
    components, description = _format_release(
        parsed,
        title,
        seeders=None,
        size=size,
        source=source,
        result_format=result_format,
        kodi=kodi,
    )
    if not components:
        components, description = _format_release(
            parsed,
            title,
            seeders=None,
            size=size,
            source=source,
            result_format=("title",),
            kodi=kodi,
        )
    return (
        parsed,
        size,
        parsed.resolution if parsed is not None else "",
        components,
        description,
    )


def _usenet_chilllink_metadata(
    provider_label: str,
    formatted_components: dict,
) -> list[str]:
    return [
        f"📰 {provider_label}",
        "Usenet",
        *(value for key, value in formatted_components.items() if key != "title"),
    ]


def _build_stream_response(
    request: Request,
    content: dict,
    is_empty: bool = False,
    vary_headers: list | None = None,
    cache_policy=None,
):
    if cache_policy is None:
        cache_policy = (
            CachePolicies.empty_results() if is_empty else CachePolicies.streams()
        )

    return cached_json_response(
        request,
        content,
        cache_policy=cache_policy,
        vary=list(dict.fromkeys(["Accept", *(vary_headers or ())])),
    )


def _encode_playback_scope(value: int | None) -> str:
    return str(value) if value is not None else "n"


def _render_server_usenet_options(
    search_result: MediaSearchResult,
    config: dict,
    playback_base_url: str,
    *,
    chilllink: bool = False,
    kodi: bool = False,
) -> dict[tuple[str, str], dict]:
    """Render validated server-side providers with a committed v2 capability."""
    candidates = {
        candidate.candidate_id: candidate for candidate in search_result.candidates
    }
    entries = {entry["configurationId"]: entry for entry in config["playbackProviders"]}
    streams = {}
    for option in search_result.provider_options:
        if option.provider.kind not in SERVER_USENET_PROVIDER_KINDS:
            continue
        capability = search_result.provider_capabilities.get(
            (option.candidate_id, option.provider.configuration_id)
        )
        if capability is None:
            continue
        candidate = candidates[option.candidate_id]
        entry = entries[option.provider.configuration_id]
        provider_label = entry["displayName"]
        parsed, size, resolution, components, description = _candidate_presentation(
            candidate,
            config,
            kodi=kodi,
        )
        stream = {
            "name": _build_stream_name(
                kodi,
                provider_label,
                resolution,
                icon="📰",
                formatted_components=components,
                status="NZB",
            ),
            "description": description,
            "behaviorHints": _release_behavior_hints(
                binge_group=(
                    f"comet|{option.provider.configuration_id}|{option.candidate_id}"
                ),
                filename=candidate.title,
                size=size,
                parsed=parsed,
                formatted_components=components,
                kodi=kodi,
            ),
            "url": f"{playback_base_url}/playback/v2/{capability}",
        }
        if chilllink:
            stream["_chilllink"] = _usenet_chilllink_metadata(
                provider_label,
                components,
            )
        streams[(option.candidate_id, option.provider.configuration_id)] = stream
    return streams


async def _render_stremio_nntp_options(
    search_result: MediaSearchResult,
    config: dict,
    playback_base_url: str,
    *,
    kodi: bool = False,
) -> dict[tuple[str, str], dict]:
    """Render direct artifacts or replay-safe lazy client-native handoffs."""
    if not settings.USENET_ENABLED or not settings.COMET_CAPABILITY_SECRET:
        return {}
    codec = CapabilityCodec(settings.COMET_CAPABILITY_SECRET)
    partition = codec.configuration_partition_for_config(config)
    entries = {
        entry["configurationId"]: entry
        for entry in config["playbackProviders"]
        if entry["enabled"] and entry["kind"] == "stremio_nntp"
    }
    accounts = config["accounts"]
    broker = NzbBroker(
        settings.USENET_ARTIFACT_DIR,
        database,
        EngineClient(Path(settings.USENET_RUNTIME_DIR) / "engine.json"),
    )
    candidates = {
        candidate.candidate_id: candidate for candidate in search_result.candidates
    }
    season = search_result.search_season
    episode = search_result.search_episode
    if season is None and episode is None:
        selection_intent = (0,)
    elif season is not None and episode is not None:
        selection_intent = (1, season, episode)
    else:
        selection_intent = ()
    persisted_candidates = search_result.rendered_candidate_ids
    provider = StremioNntpProvider()
    wanted_artifacts = [
        locator.artifact_sha256
        for option in search_result.provider_options
        if option.provider.kind == "stremio_nntp"
        for locator in option.locators
        if locator.kind is LocatorKind.NZB_ARTIFACT
    ]
    resolved_artifacts = (
        await broker.resolve_owned_artifacts(
            wanted_artifacts,
            owner_configuration_partition=partition,
        )
        if wanted_artifacts
        else {}
    )
    streams = {}
    for option in search_result.provider_options:
        if option.provider.kind != "stremio_nntp":
            continue
        entry = entries[option.provider.configuration_id]
        candidate = candidates[option.candidate_id]
        artifacts = [
            locator
            for locator in option.locators
            if locator.kind is LocatorKind.NZB_ARTIFACT
        ]
        options = resolve_capability_options(entry, accounts)
        try:
            if artifacts:
                artifact = None
                for locator in artifacts:
                    artifact = resolved_artifacts.get(locator.artifact_sha256)
                    if artifact is not None:
                        break
                if artifact is None:
                    raise NzbBrokerError("artifact_grant_unavailable")
                capability = codec.encode(
                    "na1",
                    partition=partition,
                    suffix=[uuid.UUID(artifact.grant_id).bytes],
                    ttl=6 * 60 * 60,
                )
                handoff = provider.render_resolved(
                    options,
                    f"{playback_base_url}/nzb/v1/{capability}.nzb",
                    artifact.manifest,
                    handoff_selector(candidate.title, selection_intent),
                )
            else:
                transform_sources = tuple(
                    locator
                    for locator in option.locators
                    if locator.kind in {LocatorKind.REAL_NZB, LocatorKind.EASYNEWS_HTTP}
                )
                selector = handoff_selector(
                    candidate.title,
                    selection_intent,
                )
                if selector is None:
                    continue
                persisted = persisted_candidates[option.candidate_id]
                ttl = 6 * 60 * 60
                expiries = [
                    locator.policy.expires_at
                    for locator in transform_sources
                    if locator.policy.expires_at is not None
                ]
                if expiries:
                    ttl = min(ttl, min(expiries) - int(time.time()) - 30)
                if ttl < 1:
                    continue
                capability = issue_nzb_handoff_capability(
                    codec,
                    partition=partition,
                    option=option,
                    persisted=persisted,
                    selection_intent=list(selection_intent),
                    ttl=ttl,
                )
                handoff = provider.render_unresolved(
                    options,
                    f"{playback_base_url}/nzb/intent/v2/{capability}.nzb",
                    selector,
                )
        except NzbBrokerError:
            continue
        provider_label = entry["displayName"]
        parsed, size, resolution, components, description = _candidate_presentation(
            candidate,
            config,
            kodi=kodi,
        )
        handoff["name"] = _build_stream_name(
            kodi,
            provider_label,
            resolution,
            icon="📰",
            formatted_components=components,
            status="NZB",
        )
        handoff["description"] = description
        handoff["behaviorHints"] = _release_behavior_hints(
            binge_group=(
                f"comet|{option.provider.configuration_id}|{option.candidate_id}"
            ),
            filename=candidate.title,
            size=size,
            parsed=parsed,
            formatted_components=components,
            kodi=kodi,
        )
        streams[(option.candidate_id, option.provider.configuration_id)] = handoff
    return streams


def _select_info_hashes_by_resolution(
    ranked_info_hashes,
    torrents: dict,
    service_cache_status: dict,
    max_results: int,
    cached_only: bool,
    prioritize_cached: bool,
):
    if max_results <= 0:
        return None

    per_resolution_count = defaultdict(int)
    selected_info_hashes = []

    def try_select(info_hash: str):
        resolution = str(torrents[info_hash]["parsed"].resolution)
        if per_resolution_count[resolution] >= max_results:
            return
        selected_info_hashes.append(info_hash)
        per_resolution_count[resolution] += 1

    is_cached_by_hash = {}
    if prioritize_cached or cached_only:
        is_cached_by_hash = {
            info_hash: any(service_cache_status.get(info_hash, {}).values())
            for info_hash in ranked_info_hashes
        }

    if prioritize_cached:
        for info_hash in ranked_info_hashes:
            if not is_cached_by_hash[info_hash]:
                continue
            try_select(info_hash)

        if cached_only:
            return selected_info_hashes

        for info_hash in ranked_info_hashes:
            if is_cached_by_hash[info_hash]:
                continue
            try_select(info_hash)

        return selected_info_hashes

    for info_hash in ranked_info_hashes:
        if cached_only and not is_cached_by_hash[info_hash]:
            continue
        try_select(info_hash)

    return selected_info_hashes


@streams.get(
    "/stream/{media_type}/{media_id}.json",
    tags=["Stremio"],
    summary="Stream Provider",
    description="Returns a list of streams for the specified media.",
)
@streams.get(
    "/{b64config}/stream/{media_type}/{media_id}.json",
    tags=["Stremio"],
    summary="Stream Provider",
    description="Returns a list of streams for the specified media with existing configuration.",
)
async def stream(
    request: Request,
    media_type: str,
    media_id: str,
    background_tasks: BackgroundTasks,
    b64config: str | None = None,
    chilllink: bool = False,
    kodi: bool = False,
):
    if media_type not in {"movie", "series"}:
        return _build_stream_response(request, {"streams": []}, is_empty=True)

    if media_id.startswith("tmdb:"):
        return _build_stream_response(request, {"streams": []}, is_empty=True)

    config = config_check(b64config)
    if not config:
        error_response = {
            "streams": [
                {
                    "name": _stream_notice_name(kodi, "[❌] Comet", "[ERROR] Comet"),
                    "description": (
                        f"OBSOLETE CONFIGURATION, PLEASE RE-CONFIGURE ON {request.url.scheme}://{request.url.netloc}"
                        if kodi
                        else f"⚠️ OBSOLETE CONFIGURATION, PLEASE RE-CONFIGURE ON {request.url.scheme}://{request.url.netloc} ⚠️"
                    ),
                    "url": "https://comet.feels.legal",
                }
            ]
        }
        return _build_stream_response(request, error_response, is_empty=True)

    debrid_entries = config["_debridEntries"]
    enable_torrent = config["_enableTorrent"]
    is_v2 = config.get("schemaVersion") == 2
    use_account_scrape = bool(debrid_entries and config["scrapeDebridAccountTorrents"])
    response_cache_policy = CachePolicies.no_cache() if use_account_scrape else None
    stream_cache_state = "unknown"
    stream_client = "kodi" if kodi else ("chilllink" if chilllink else "stremio")

    def _stream_response(content: dict, is_empty: bool = False):
        result_count = len(content["streams"])
        metrics.observe_stream(
            media_type,
            stream_client,
            stream_cache_state,
            "empty" if is_empty else "success",
            result_count,
        )
        if chilllink:
            # ChillLink translates this internal Stremio shape into its own
            # response and must not inherit conditional/cache semantics from
            # the outer request.
            return content
        return _build_stream_response(
            request,
            content,
            is_empty=is_empty,
            cache_policy=response_cache_policy,
        )

    search_result = await search_media(
        media_type,
        media_id,
        config,
        get_client_ip(request),
        background_tasks.add_task,
        client_type=stream_client,
    )
    stream_cache_state = search_result.cache_state

    if search_result.status is MediaSearchStatus.INVALID:
        return _stream_response({"streams": []}, is_empty=True)

    if search_result.status is MediaSearchStatus.DISABLED:
        placeholder_stream = {
            "name": settings.TORRENT_DISABLED_STREAM_NAME,
            "description": settings.TORRENT_DISABLED_STREAM_DESCRIPTION,
        }
        if settings.TORRENT_DISABLED_STREAM_URL:
            placeholder_stream["url"] = settings.TORRENT_DISABLED_STREAM_URL

        return _stream_response({"streams": [placeholder_stream]})

    if search_result.status is MediaSearchStatus.UNRELEASED:
        return _stream_response(
            {
                "streams": [
                    {
                        "name": _stream_notice_name(
                            kodi, "[🚫] Comet", "[BLOCKED] Comet"
                        ),
                        "description": "Content not digitally released yet.",
                        "url": "https://comet.feels.legal",
                    }
                ]
            },
            is_empty=True,
        )

    if search_result.status in {
        MediaSearchStatus.METADATA_UNAVAILABLE,
        MediaSearchStatus.METADATA_NOT_FOUND,
        MediaSearchStatus.METADATA_UNSUPPORTED,
    }:
        return _stream_response(
            {
                "streams": [
                    {
                        "name": _stream_notice_name(kodi, "[⚠️] Comet", "[WARN] Comet"),
                        "description": "Unable to get metadata.",
                        "url": "https://comet.feels.legal",
                    }
                ]
            },
            is_empty=True,
        )

    if search_result.status is MediaSearchStatus.BUSY:
        response_cache_policy = CachePolicies.no_cache()
        return _stream_response(
            {
                "streams": [
                    {
                        "name": _stream_notice_name(kodi, "[🔄] Comet", "[INFO] Comet"),
                        "description": (
                            "Scraping in progress, please try again in a few seconds..."
                        ),
                        "url": "https://comet.feels.legal",
                    }
                ]
            },
            is_empty=True,
        )

    metadata = search_result.metadata
    title = metadata["title"]
    media_only_id = search_result.media_only_id
    search_season = search_result.search_season
    search_episode = search_result.search_episode
    service_cache_status = search_result.service_cache_status
    debrid_errors = search_result.debrid_errors
    torrents = search_result.torrents
    debrid_labels = {
        entry.get("configurationId", entry["service"]): get_debrid_extension(
            entry["service"]
        )
        for entry in debrid_entries
    }
    base_streams = [
        {
            "name": (
                f"[ERROR] {debrid_labels.get(provider_key, provider_key)}"
                if kodi
                else f"[❌] {debrid_labels.get(provider_key, provider_key)}"
            ),
            "description": error.display_message,
            "url": "https://comet.feels.legal",
        }
        for provider_key, error in search_result.debrid_errors.items()
    ]
    provider_streams: dict[tuple[str, str], dict] = {}
    debrid_stream_specs = [
        (
            entry_index,
            entry.get("configurationId", entry["service"]),
            entry["service"],
            get_debrid_extension(entry["service"]),
        )
        for entry_index, entry in enumerate(debrid_entries)
    ]

    if (
        config["debridStreamProxyPassword"] != ""
        and settings.PROXY_DEBRID_STREAM
        and settings.PROXY_DEBRID_STREAM_PASSWORD != config["debridStreamProxyPassword"]
    ):
        base_streams.append(
            {
                "name": _stream_notice_name(kodi, "[⚠️] Comet", "[WARN] Comet"),
                "description": "Debrid Stream Proxy Password incorrect.\nStreams will not be proxied.",
                "url": "https://comet.feels.legal",
            }
        )

    result_season = _encode_playback_scope(search_season)
    result_episode = _encode_playback_scope(search_episode)
    quoted_media_only_id = quote(media_only_id, safe="")

    base_playback_host = (
        settings.PUBLIC_BASE_URL
        if settings.PUBLIC_BASE_URL
        else f"{request.url.scheme}://{request.url.netloc}"
    )
    api_prefix = STREMIO_API_PREFIX
    config_segment = f"/{b64config}" if b64config else ""
    playback_base_url = f"{base_playback_host}{api_prefix}{config_segment}"
    if b64config and is_v2:
        base_streams[:0] = _render_discovery_diagnostics(
            search_result.discovery_diagnostics,
            configure_url=f"{playback_base_url}/configure",
            kodi=kodi,
        )
    quoted_title = quote(title)
    torrent_extension = get_debrid_extension("torrent")
    torrent_service = "" if kodi else torrent_extension

    if search_result.show_account_sync_trigger:
        for entry_index, _, _, debrid_extension in debrid_stream_specs:
            base_streams.append(
                {
                    "name": (
                        f"[{debrid_extension}] Comet Sync"
                        if kodi
                        else f"[{debrid_extension}🔄] Comet Sync"
                    ),
                    "description": (
                        "Sync debrid account library now.\n"
                        "Select this stream, then retry this title in a few seconds."
                    ),
                    "url": f"{playback_base_url}/debrid-sync/{entry_index}",
                }
            )

    ranked_info_hashes = search_result.ranked_info_hashes
    direct_options = (
        {
            option.candidate_id: option
            for option in search_result.provider_options
            if option.provider.kind == "direct_torrent"
        }
        if is_v2 and enable_torrent
        else {}
    )
    if not is_v2:
        selected_info_hashes = _select_info_hashes_by_resolution(
            ranked_info_hashes=search_result.ranked_info_hashes,
            torrents=torrents,
            service_cache_status=service_cache_status,
            max_results=config["maxResultsPerResolution"],
            cached_only=bool(
                config["cachedOnly"] and debrid_entries and not enable_torrent
            ),
            prioritize_cached=False,
        )
        ranked_info_hashes = (
            selected_info_hashes
            if selected_info_hashes is not None
            else search_result.ranked_info_hashes
        )

    for info_hash in ranked_info_hashes:
        torrent = torrents[info_hash]
        rtn_data = torrent["parsed"]
        torrent_title = torrent["title"]
        torrent_size = torrent["size"]
        formatted_components, formatted_title = _format_release(
            rtn_data,
            torrent_title,
            seeders=torrent["seeders"],
            size=torrent_size,
            source=torrent["tracker"],
            result_format=config["resultFormat"],
            kodi=kodi,
        )
        info_hash_cache_status = service_cache_status.get(info_hash)
        quoted_torrent_title = quote(torrent_title)

        for (
            entry_index,
            provider_configuration_id,
            service,
            debrid_extension,
        ) in debrid_stream_specs:
            if provider_configuration_id in debrid_errors:
                continue

            is_cached = (
                info_hash_cache_status.get(provider_configuration_id, False)
                if info_hash_cache_status
                else False
            )

            if not is_v2 and config["cachedOnly"] and not is_cached:
                continue

            stream_name = _build_stream_name(
                kodi,
                debrid_extension,
                rtn_data.resolution,
                icon="⚡" if is_cached else "⬇️",
                formatted_components=formatted_components,
                seeders=torrent["seeders"],
                status="Cached" if is_cached else "On demand",
            )

            the_stream = {
                "name": stream_name,
                "description": formatted_title,
                "behaviorHints": _release_behavior_hints(
                    binge_group=f"comet|{service}|{info_hash}",
                    filename=rtn_data.raw_title,
                    size=torrent_size,
                    parsed=rtn_data,
                    formatted_components=formatted_components,
                    kodi=kodi,
                ),
            }

            if chilllink:
                the_stream["_chilllink"] = format_chilllink(
                    formatted_components, is_cached
                )

            file_index = torrent.get("fileIndex")
            file_index_str = (
                str(file_index) if is_cached and file_index is not None else "n"
            )
            if is_v2:
                presentation_key = (
                    f"btih:{info_hash}",
                    provider_configuration_id,
                )
                capability = search_result.provider_capabilities.get(presentation_key)
                if capability is None:
                    continue
                the_stream["url"] = f"{playback_base_url}/playback/v2/{capability}"
            else:
                the_stream["url"] = (
                    f"{playback_base_url}/playback/{info_hash}/{entry_index}/{file_index_str}/{result_season}/{result_episode}"
                    f"?torrent_name={quoted_torrent_title}&name={quoted_title}"
                    f"&media_id={quoted_media_only_id}&media_type={media_type}"
                )

            if is_v2:
                provider_streams[presentation_key] = the_stream
            else:
                base_streams.append(the_stream)

        if enable_torrent:
            direct_option = direct_options[f"btih:{info_hash}"] if is_v2 else None
            stream_name = _build_stream_name(
                kodi,
                torrent_service,
                rtn_data.resolution,
                icon="🧲",
                formatted_components=formatted_components,
                seeders=torrent["seeders"],
                status="P2P",
            )

            the_stream = {
                "name": stream_name,
                "description": formatted_title,
                "behaviorHints": _release_behavior_hints(
                    binge_group=f"comet|torrent|{info_hash}",
                    filename=rtn_data.raw_title,
                    size=torrent_size,
                    parsed=rtn_data,
                    formatted_components=formatted_components,
                    kodi=kodi,
                ),
                "infoHash": info_hash,
            }

            if chilllink:
                the_stream["_chilllink"] = format_chilllink(formatted_components, False)

            if torrent.get("fileIndex") is not None:
                the_stream["fileIdx"] = torrent["fileIndex"]

            sources = torrent.get("sources") or trackers
            if sources:
                the_stream["sources"] = sources

            if direct_option is not None:
                provider_streams[
                    (
                        direct_option.candidate_id,
                        direct_option.provider.configuration_id,
                    )
                ] = the_stream
            else:
                base_streams.append(the_stream)

    if b64config and is_v2:
        server_usenet_streams = _render_server_usenet_options(
            search_result,
            config,
            playback_base_url,
            chilllink=chilllink,
            kodi=kodi,
        )
        provider_streams.update(server_usenet_streams)
        nntp_handoffs = await _render_stremio_nntp_options(
            search_result,
            config,
            playback_base_url,
            kodi=kodi,
        )
        if nntp_handoffs:
            # These objects intentionally carry client NNTP credentials.
            response_cache_policy = CachePolicies.no_cache()
            provider_streams.update(nntp_handoffs)

    if is_v2:
        ordered_provider_streams = [
            stream
            for option in search_result.provider_options
            if (
                stream := provider_streams.get(
                    (
                        option.candidate_id,
                        option.provider.configuration_id,
                    )
                )
            )
            is not None
        ]
        final_streams = base_streams + ordered_provider_streams
    else:
        final_streams = base_streams

    has_results = bool(final_streams)

    return _stream_response(
        {"streams": final_streams},
        is_empty=not has_results,
    )
