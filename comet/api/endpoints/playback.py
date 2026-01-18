import time

import aiohttp
import mediaflow_proxy.utils.http_utils
import orjson
from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, RedirectResponse

from comet.core.config_validation import config_check
from comet.core.models import database, settings
from comet.debrid.manager import get_debrid
from comet.metadata.manager import MetadataScraper
from comet.services.streaming.manager import custom_handle_stream_request
from comet.utils.cache import NO_CACHE_HEADERS
from comet.utils.network import get_client_ip
from comet.utils.parsing import parse_optional_int

router = APIRouter()


def _get_debrid_credentials(config: dict, service_index: int = None):
    debrid_entries = config.get("_debridEntries", [])

    if debrid_entries and service_index is not None:
        if 0 <= service_index < len(debrid_entries):
            entry = debrid_entries[service_index]
            return entry["service"], entry["apiKey"]

    if debrid_entries:
        entry = debrid_entries[0]
        return entry["service"], entry["apiKey"]

    # Legacy single-service format
    return config.get("debridService", "torrent"), config.get("debridApiKey", "")


@router.get(
    "/{b64config}/playback/{hash}/{service_index}/{index}/{season}/{episode}/{torrent_name:path}",
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
    torrent_name: str,
    name_query: str = Query("", alias="name"),
):
    config = config_check(b64config)

    parsed_service_index = parse_optional_int(service_index)
    season = parse_optional_int(season)
    episode = parse_optional_int(episode)

    debrid_service, debrid_api_key = _get_debrid_credentials(
        config, parsed_service_index
    )

    async with aiohttp.ClientSession() as session:
        cached_link = await database.fetch_one(
            """
            SELECT download_url
            FROM download_links_cache
            WHERE debrid_key = :debrid_key
            AND info_hash = :info_hash
            AND ((CAST(:season as INTEGER) IS NULL AND season IS NULL) OR season = CAST(:season as INTEGER))
            AND ((CAST(:episode as INTEGER) IS NULL AND episode IS NULL) OR episode = CAST(:episode as INTEGER))
            AND timestamp + 3600 >= :current_time
            """,
            {
                "debrid_key": debrid_api_key,
                "info_hash": hash,
                "season": season,
                "episode": episode,
                "current_time": time.time(),
            },
        )

        download_url = None
        if cached_link:
            download_url = cached_link["download_url"]

        ip = get_client_ip(request)
        should_proxy = (
            settings.PROXY_DEBRID_STREAM
            and settings.PROXY_DEBRID_STREAM_PASSWORD
            == config["debridStreamProxyPassword"]
        )

        if download_url is None:
            # Retrieve torrent sources from database for private trackers
            torrent_data = await database.fetch_one(
                """
                SELECT sources, media_id
                FROM torrents
                WHERE info_hash = :info_hash
                LIMIT 1
                """,
                {"info_hash": hash},
            )

            sources = []
            media_id = None
            if torrent_data:
                if torrent_data["sources"]:
                    sources = orjson.loads(torrent_data["sources"])
                media_id = torrent_data["media_id"]

            aliases = {}
            if media_id:
                metadata_scraper = MetadataScraper(session)
                media_type = "series" if season is not None else "movie"

                if "tt" in media_id:
                    full_media_id = (
                        f"{media_id}:{season}:{episode}"
                        if media_type == "series"
                        else media_id
                    )
                else:
                    full_media_id = (
                        f"kitsu:{media_id}:{episode}"
                        if media_type == "series"
                        else f"kitsu:{media_id}"
                    )

                _, aliases = await metadata_scraper.fetch_metadata_and_aliases(
                    media_type, full_media_id
                )

            debrid = get_debrid(
                session,
                None,
                None,
                debrid_service,
                debrid_api_key,
                ip if not should_proxy else "",
            )
            download_url = await debrid.generate_download_link(
                hash, index, name_query, torrent_name, season, episode, sources, aliases
            )
            if not download_url:
                return FileResponse(
                    "comet/assets/uncached.mp4", headers=NO_CACHE_HEADERS
                )

            await database.execute(
                f"""
                    INSERT {"OR IGNORE " if settings.DATABASE_TYPE == "sqlite" else ""}
                    INTO download_links_cache
                    VALUES (:debrid_key, :info_hash, :season, :episode, :download_url, :timestamp)
                    {" ON CONFLICT DO NOTHING" if settings.DATABASE_TYPE == "postgresql" else ""}
                """,
                {
                    "debrid_key": debrid_api_key,
                    "info_hash": hash,
                    "season": season,
                    "episode": episode,
                    "download_url": download_url,
                    "timestamp": time.time(),
                },
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


# Legacy route
@router.get(
    "/{b64config}/playback/{hash}/{index}/{season}/{episode}/{torrent_name:path}",
    tags=["Stremio"],
    summary="Playback Proxy (Legacy)",
    description="Legacy playback route for backward compatibility.",
)
async def playback_legacy(
    request: Request,
    b64config: str,
    hash: str,
    index: str,
    season: str,
    episode: str,
    torrent_name: str,
    name_query: str = Query(None, alias="name"),
):
    # Call the new playback with service_index="n" (will use first service)
    return await playback(
        request=request,
        b64config=b64config,
        hash=hash,
        service_index="n",
        index=index,
        season=season,
        episode=episode,
        torrent_name=torrent_name,
        name_query=name_query,
    )
