import time

import aiohttp
import mediaflow_proxy.utils.http_utils
import orjson
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse

from comet.core.config_validation import config_check
from comet.core.models import database, settings
from comet.debrid.manager import get_debrid
from comet.metadata.manager import MetadataScraper
from comet.services.streaming.manager import custom_handle_stream_request
from comet.utils.network import NO_CACHE_HEADERS, get_client_ip

router = APIRouter()


def _parse_optional_int(value: str, field_name: str) -> int | None:
    if value == "n":
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {field_name} parameter") from exc


@router.get(
    "/{b64config}/playback/{hash}/{index}/{season}/{episode}/{torrent_name:path}"
)
async def playback(
    request: Request,
    b64config: str,
    hash: str,
    index: str,
    season: str,
    episode: str,
    torrent_name: str,
    name_query: str = Query(None, alias="name"),
):
    config = config_check(b64config)

    season = _parse_optional_int(season, "season")
    episode = _parse_optional_int(episode, "episode")

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
                "debrid_key": config["debridApiKey"],
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
                config["debridService"],
                config["debridApiKey"],
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
                    "debrid_key": config["debridApiKey"],
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
