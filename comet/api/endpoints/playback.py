import re

import mediaflow_proxy.utils.http_utils
from fastapi import APIRouter, BackgroundTasks, Query, Request
from fastapi.responses import RedirectResponse

from comet.core.config_validation import config_check
from comet.core.database import database
from comet.core.models import settings
from comet.core.sources import MAX_SIGNED_BIGINT
from comet.debrid.exceptions import DebridLinkGenerationError
from comet.debrid.link_cache import (
    cache_download_link_best_effort,
    get_cached_download_link,
    valid_download_url,
)
from comet.debrid.manager import (
    build_account_key_hash,
    build_playback_media_id,
    get_debrid,
    get_debrid_credentials,
)
from comet.discovery.torrent_repository import TorrentReleaseRepository
from comet.metadata.manager import MetadataScraper
from comet.observability.boundaries import playback_boundary
from comet.services.status_video import build_status_video_response
from comet.services.streaming.manager import custom_handle_stream_request
from comet.utils.http_client import http_client_manager
from comet.utils.network import get_client_ip, get_client_ip_any

router = APIRouter()
_INFO_HASH_PATTERN = re.compile(r"[0-9a-f]{40}")
_NONNEGATIVE_INTEGER_PATTERN = re.compile(r"0|[1-9][0-9]*")
_build_playback_media_id = build_playback_media_id
_valid_download_url = valid_download_url


def _bounded_query_text(value, *, maximum_bytes: int) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return False
    return size <= maximum_bytes and all(
        ord(character) >= 32 and ord(character) != 127 for character in value
    )


def _parse_optional_path_integer(value: str) -> int | None:
    if value == "n":
        return None
    if (
        type(value) is not str
        or len(value) > 19
        or _NONNEGATIVE_INTEGER_PATTERN.fullmatch(value) is None
    ):
        raise ValueError("path integer must be canonical, non-negative, or 'n'")
    parsed = int(value)
    if parsed > MAX_SIGNED_BIGINT:
        raise ValueError("path integer is too large")
    return parsed


def _parse_playback_path(
    info_hash: str,
    service_index: str,
    file_index: str,
    season: str,
    episode: str,
) -> tuple[str, int, str, int | None, int | None]:
    if type(info_hash) is not str or _INFO_HASH_PATTERN.fullmatch(info_hash) is None:
        raise ValueError("info hash must be 40 lowercase hexadecimal characters")
    parsed_service_index = _parse_optional_path_integer(service_index)
    if parsed_service_index is None:
        raise ValueError("service index is required")
    parsed_file_index = _parse_optional_path_integer(file_index)
    return (
        info_hash,
        parsed_service_index,
        "n" if parsed_file_index is None else str(parsed_file_index),
        _parse_optional_path_integer(season),
        _parse_optional_path_integer(episode),
    )


@router.get(
    "/{b64config}/playback/{hash}/{service_index}/{index}/{season}/{episode}",
    tags=["Stremio"],
    summary="Playback Proxy",
    description="Proxies the playback request to the Debrid service or returns a cached link.",
)
@playback_boundary(default_mode="proxy", default_source_type="torrent")
async def playback(
    request: Request,
    b64config: str,
    hash: str,
    service_index: str,
    index: str,
    season: str,
    episode: str,
    torrent_name: str = Query(max_length=2_048),
    name: str = Query(max_length=2_048),
    media_id: str | None = Query(default=None, max_length=128),
    media_type: str | None = Query(default=None, max_length=16),
    background_tasks: BackgroundTasks = None,
):
    config = config_check(b64config)
    if config is None:
        return build_status_video_response(
            ["BAD_REQUEST"],
            default_key="BAD_REQUEST",
        )
    if config.get("schemaVersion") == 2:
        return build_status_video_response(
            ["BAD_REQUEST"],
            default_key="BAD_REQUEST",
        )

    if media_id:
        request.state.comet_content_id = media_id
    if (
        not _bounded_query_text(torrent_name, maximum_bytes=2_048)
        or not _bounded_query_text(name, maximum_bytes=2_048)
        or (
            media_id is not None
            and not _bounded_query_text(media_id, maximum_bytes=128)
        )
        or media_type not in {None, "movie", "series"}
    ):
        return build_status_video_response(
            ["BAD_REQUEST"],
            default_key="BAD_REQUEST",
        )

    try:
        hash, parsed_service_index, index, season, episode = _parse_playback_path(
            hash,
            service_index,
            index,
            season,
            episode,
        )
        debrid_service, debrid_api_key = get_debrid_credentials(
            config, parsed_service_index
        )
    except ValueError:
        return build_status_video_response(
            ["BAD_REQUEST"],
            default_key="BAD_REQUEST",
        )
    account_key_hash = build_account_key_hash(debrid_api_key)

    session = await http_client_manager.get_session()
    ip = get_client_ip(request)
    should_proxy = (
        settings.PROXY_DEBRID_STREAM
        and settings.PROXY_DEBRID_STREAM_PASSWORD == config["debridStreamProxyPassword"]
    )
    link_client_ip = "" if should_proxy else ip
    download_url = await get_cached_download_link(
        database,
        debrid_service=debrid_service,
        account_key_hash=account_key_hash,
        info_hash=hash,
        season=season,
        episode=episode,
        selection_key=index,
        client_ip=link_client_ip,
    )
    if download_url is None:
        repository = TorrentReleaseRepository(database)
        torrent_data = await repository.find_context(hash, media_id=media_id)
        if torrent_data is None and media_id is not None:
            torrent_data = await repository.find_context(hash)

        sources = []
        context_media_id = media_id
        if torrent_data is not None:
            sources = torrent_data["sources"]
            if context_media_id is None:
                context_media_id = torrent_data["media_id"]

        aliases = {}
        debrid_video_id = None
        debrid_media_only_id = context_media_id
        if context_media_id:
            metadata_scraper = MetadataScraper(session)
            resolved_media_type = media_type or (
                "series" if season is not None else "movie"
            )
            try:
                full_media_id = build_playback_media_id(
                    context_media_id,
                    resolved_media_type,
                    season,
                    episode,
                )
            except ValueError:
                return build_status_video_response(
                    ["BAD_REQUEST"],
                    default_key="BAD_REQUEST",
                )

            debrid_video_id = full_media_id
            metadata_result = await metadata_scraper.fetch_metadata_and_aliases(
                resolved_media_type, full_media_id
            )
            aliases = metadata_result.aliases

        debrid = get_debrid(
            session,
            debrid_video_id,
            debrid_media_only_id,
            debrid_service,
            debrid_api_key,
            link_client_ip,
        )
        if debrid is None:
            return build_status_video_response(
                ["BAD_REQUEST"],
                default_key="BAD_REQUEST",
            )
        try:
            download_url = await debrid.generate_download_link(
                hash,
                index,
                name,
                torrent_name,
                season,
                episode,
                sources,
                aliases,
            )
        except DebridLinkGenerationError as error:
            status_keys = error.status_keys
            return build_status_video_response(
                status_keys,
                default_key=status_keys[0] if status_keys else "UNKNOWN",
            )

        download_url = valid_download_url(download_url)
        if download_url is None:
            return build_status_video_response(
                [],
                default_key="UNKNOWN",
            )

        await cache_download_link_best_effort(
            database,
            add_background_task=(
                background_tasks.add_task if background_tasks is not None else None
            ),
            debrid_service=debrid_service,
            account_key_hash=account_key_hash,
            info_hash=hash,
            season=season,
            episode=episode,
            selection_key=index,
            client_ip=link_client_ip,
            download_url=download_url,
        )

    if should_proxy:
        return await custom_handle_stream_request(
            request.method,
            download_url,
            mediaflow_proxy.utils.http_utils.get_proxy_headers(request),
            media_id=torrent_name,
            ip=get_client_ip_any(request)[0],
            source_type="torrent",
            service=debrid_service,
        )

    return RedirectResponse(download_url, status_code=302)
