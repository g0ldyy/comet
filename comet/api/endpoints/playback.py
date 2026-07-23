import re
import time
from urllib.parse import urlsplit

import mediaflow_proxy.utils.http_utils
import orjson
from fastapi import APIRouter, Query, Request
from fastapi.responses import RedirectResponse

from comet.core.config_validation import config_check
from comet.core.database import (
    DOWNLOAD_LINK_CACHE_TTL,
    build_scope_lookup_params,
    build_scope_params,
    database,
)
from comet.core.logger import logger
from comet.core.models import settings
from comet.debrid.exceptions import DebridLinkGenerationError
from comet.debrid.manager import (
    build_account_key_hash,
    get_debrid,
    get_debrid_credentials,
)
from comet.metadata.manager import MetadataScraper
from comet.services.status_video import build_status_video_response
from comet.services.streaming.manager import custom_handle_stream_request
from comet.utils.http_client import http_client_manager
from comet.utils.network import get_client_ip

router = APIRouter()
_INFO_HASH_PATTERN = re.compile(r"[0-9a-f]{40}")
_NONNEGATIVE_INTEGER_PATTERN = re.compile(r"0|[1-9][0-9]*")


def _parse_optional_path_integer(value: str) -> int | None:
    if value == "n":
        return None
    if type(value) is not str or _NONNEGATIVE_INTEGER_PATTERN.fullmatch(value) is None:
        raise ValueError("path integer must be canonical, non-negative, or 'n'")
    return int(value)


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


def _valid_download_url(value) -> str | None:
    if type(value) is not str or not value or any(ord(char) < 32 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        parsed.port
    except (ValueError, UnicodeError):
        return None
    return value


def _decode_sources(sources_json) -> list[str]:
    if not sources_json:
        return []

    try:
        sources = orjson.loads(sources_json)
    except (TypeError, orjson.JSONDecodeError):
        return []

    if not isinstance(sources, list):
        return []
    return [source for source in sources if isinstance(source, str) and source]


async def cache_download_link(
    *,
    debrid_service: str,
    account_key_hash: str,
    info_hash: str,
    season: int | None,
    episode: int | None,
    download_url: str,
):
    params = {
        "debrid_service": debrid_service,
        "account_key_hash": account_key_hash,
        "info_hash": info_hash,
        "download_url": download_url,
        "updated_at": time.time(),
        **build_scope_params(season, episode),
    }
    await database.execute(
        """
        INSERT INTO download_links_cache (
            debrid_service,
            account_key_hash,
            info_hash,
            season,
            episode,
            season_norm,
            episode_norm,
            download_url,
            updated_at
        )
        VALUES (
            :debrid_service,
            :account_key_hash,
            :info_hash,
            :season,
            :episode,
            :season_norm,
            :episode_norm,
            :download_url,
            :updated_at
        )
        ON CONFLICT (
            debrid_service,
            account_key_hash,
            info_hash,
            season_norm,
            episode_norm
        ) DO UPDATE SET
            download_url = EXCLUDED.download_url,
            updated_at = EXCLUDED.updated_at
        """,
        params,
    )


async def _cache_download_link_safely(**kwargs) -> None:
    try:
        await cache_download_link(**kwargs)
    except Exception as exc:
        logger.warning(
            "Failed to cache generated download link for "
            f"{kwargs['debrid_service']}:{kwargs['info_hash']} "
            f"({type(exc).__name__})"
        )


@router.get(
    "/{b64config}/playback/{hash}/{service_index}/{index}/{season}/{episode}",
    tags=["Stremio"],
    summary="Playback Proxy",
    description="Proxies the playback request to the Debrid service or returns a cached link.",
)
async def playback(
    request: Request,
    b64config: str,
    hash: str,
    service_index: str,
    index: str,
    season: str,
    episode: str,
    torrent_name: str = Query(),
    name: str = Query(),
    media_id: str | None = Query(default=None),
):
    config = config_check(b64config, strict_b64config=True)
    if not config:
        return build_status_video_response(
            ["BAD_REQUEST"],
            default_key="BAD_REQUEST",
        )

    torrent_name = torrent_name.strip()
    name = name.strip()
    media_id = media_id.strip() if media_id else None
    if not torrent_name or not name:
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
    min_timestamp = time.time() - DOWNLOAD_LINK_CACHE_TTL
    scope_params = build_scope_lookup_params(season, episode)
    cached_link = await database.fetch_one(
        """
        SELECT download_url
        FROM download_links_cache
        WHERE debrid_service = :debrid_service
        AND account_key_hash = :account_key_hash
        AND info_hash = :info_hash
        AND season_norm = :season_norm
        AND episode_norm = :episode_norm
        AND updated_at >= :min_timestamp
        """,
        {
            "debrid_service": debrid_service,
            "account_key_hash": account_key_hash,
            "info_hash": hash,
            "min_timestamp": min_timestamp,
            **scope_params,
        },
    )

    download_url = None
    if cached_link:
        download_url = _valid_download_url(cached_link["download_url"])

    ip = get_client_ip(request)
    should_proxy = (
        settings.PROXY_DEBRID_STREAM
        and settings.PROXY_DEBRID_STREAM_PASSWORD == config["debridStreamProxyPassword"]
    )
    if download_url is None:
        # Retrieve torrent sources from database for private trackers.
        if media_id:
            torrent_data = await database.fetch_one(
                """
                SELECT sources_json
                FROM torrents
                WHERE info_hash = :info_hash
                AND media_id = :media_id
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                {"info_hash": hash, "media_id": media_id},
            )
            if torrent_data is None:
                torrent_data = await database.fetch_one(
                    """
                    SELECT sources_json
                    FROM torrents
                    WHERE info_hash = :info_hash
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    {"info_hash": hash},
                )
        else:
            torrent_data = await database.fetch_one(
                """
                SELECT sources_json, media_id
                FROM torrents
                WHERE info_hash = :info_hash
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                {"info_hash": hash},
            )

        sources = []
        context_media_id = media_id
        if torrent_data:
            sources = _decode_sources(torrent_data["sources_json"])
            if context_media_id is None:
                context_media_id = torrent_data["media_id"]

        aliases = {}
        debrid_video_id = None
        debrid_media_only_id = context_media_id
        if context_media_id:
            metadata_scraper = MetadataScraper(session)
            media_type = "series" if season is not None else "movie"

            if "tt" in context_media_id:
                full_media_id = (
                    f"{context_media_id}:{season}:{episode}"
                    if media_type == "series"
                    else context_media_id
                )
            else:
                full_media_id = (
                    f"kitsu:{context_media_id}:{episode}"
                    if media_type == "series"
                    else f"kitsu:{context_media_id}"
                )

            debrid_video_id = full_media_id
            _, aliases = await metadata_scraper.fetch_metadata_and_aliases(
                media_type, full_media_id
            )

        debrid = get_debrid(
            session,
            debrid_video_id,
            debrid_media_only_id,
            debrid_service,
            debrid_api_key,
            ip if not should_proxy else "",
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

        if not download_url:
            return build_status_video_response(
                [],
                default_key="UNKNOWN",
            )
        download_url = _valid_download_url(download_url)
        if download_url is None:
            return build_status_video_response(
                ["BAD_REQUEST"],
                default_key="BAD_REQUEST",
            )

        await _cache_download_link_safely(
            debrid_service=debrid_service,
            account_key_hash=account_key_hash,
            info_hash=hash,
            season=season,
            episode=episode,
            download_url=download_url,
        )

    if should_proxy:
        return await custom_handle_stream_request(
            request.method,
            download_url,
            mediaflow_proxy.utils.http_utils.get_proxy_headers(request),
            media_id=torrent_name,
            ip=ip,
        )

    return RedirectResponse(download_url, status_code=302)
