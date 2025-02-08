import aiohttp
import time
import asyncio
import mediaflow_proxy.handlers
import mediaflow_proxy.utils.http_utils

from fastapi import APIRouter, Request, BackgroundTasks
from fastapi.responses import (
    FileResponse,
    RedirectResponse,
)

from comet.utils.models import settings, database
from comet.metadata.manager import MetadataScraper
from comet.scrapers.manager import TorrentManager
from comet.utils.general import config_check, format_title, get_client_ip
from comet.debrid.manager import get_debrid_extension, get_debrid

streams = APIRouter()


async def remove_ongoing_search_from_database(media_id: str):
    await database.execute(
        "DELETE FROM ongoing_searches WHERE media_id = :media_id",
        {"media_id": media_id},
    )


@streams.get("/stream/{media_type}/{media_id}.json")
@streams.get("/{b64config}/stream/{media_type}/{media_id}.json")
async def stream(
    request: Request,
    media_type: str,
    media_id: str,
    background_tasks: BackgroundTasks,
    b64config: str = None,
):
    config = config_check(b64config)

    connector = aiohttp.TCPConnector(limit=0)
    async with aiohttp.ClientSession(connector=connector) as session:
        metadata, aliases = await MetadataScraper(session).fetch_metadata_and_aliases(
            media_type, media_id
        )
        if metadata is None:
            return {
                "streams": [
                    {
                        "name": "[⚠️] Comet",
                        "description": "Unable to get metadata.",
                        "url": "https://comet.fast",
                    }
                ]
            }

        title = metadata["title"]
        year = metadata["year"]
        year_end = metadata["year_end"]
        season = metadata["season"]
        episode = metadata["episode"]

        log_title = f"({media_id}) {title}"
        if media_type == "series":
            log_title += f" S{season:02d}E{episode:02d}"

        debrid_service = config["debridService"]
        torrent_manager = TorrentManager(
            config["stremthruUrl"],
            debrid_service,
            config["debridApiKey"],
            get_client_ip(request),
            media_type,
            media_id,
            title,
            year,
            year_end,
            season,
            episode,
            aliases,
            settings.REMOVE_ADULT_CONTENT and config["removeTrash"],
        )

        await torrent_manager.get_cached_torrents()
        if (
            len(torrent_manager.torrents) == 0
        ):  # no torrent, we search for an ongoing search before starting a new one
            cached = False
            ongoing_search = await database.fetch_one(
                "SELECT * FROM ongoing_searches WHERE media_id = :media_id AND timestamp + 120 >= :current_time",
                {"media_id": media_id, "current_time": time.time()},
            )
            if ongoing_search:
                while ongoing_search:
                    await asyncio.sleep(10)
                    ongoing_search = await database.fetch_one(
                        "SELECT * FROM ongoing_searches WHERE media_id = :media_id AND timestamp + 120 >= :current_time",
                        {"media_id": media_id, "current_time": time.time()},
                    )

                await (
                    torrent_manager.get_cached_torrents()
                )  # we verify that no cache is available
                if len(torrent_manager.torrents) != 0:
                    cached = True

            if not cached:
                await database.execute(
                    f"INSERT {'OR IGNORE ' if settings.DATABASE_TYPE == 'sqlite' else ''}INTO ongoing_searches VALUES (:media_id, :timestamp){' ON CONFLICT DO NOTHING' if settings.DATABASE_TYPE == 'postgresql' else ''}",
                    {"media_id": media_id, "timestamp": time.time()},
                )
                background_tasks.add_task(remove_ongoing_search_from_database, media_id)
                await torrent_manager.scrape_torrents(session)

        await torrent_manager.get_cached_availability()
        if debrid_service != "torrent" and not any(
            torrent["cached"] for torrent in torrent_manager.torrents.values()
        ):
            await torrent_manager.get_and_cache_debrid_availability(session)

        torrent_manager.rank_torrents(
            config["rtnSettings"],
            config["rtnRanking"],
            config["maxResultsPerResolution"],
            config["maxSize"],
            config["cachedOnly"],
            config["removeTrash"],
        )

        debrid_extension = get_debrid_extension(debrid_service, config["debridApiKey"])
        torrents = torrent_manager.torrents

        cached_results = []
        non_cached_results = []
        if (
            config["debridStreamProxyPassword"] != ""
            and settings.PROXY_DEBRID_STREAM
            and settings.PROXY_DEBRID_STREAM_PASSWORD
            != config["debridStreamProxyPassword"]
        ):
            cached_results.append(
                {
                    "name": "[⚠️] Comet",
                    "description": "Debrid Stream Proxy Password incorrect.\nStreams will not be proxied.",
                    "url": "https://comet.fast",
                }
            )

        for info_hash, torrent in torrent_manager.ranked_torrents.items():
            torrent_data = torrents[info_hash]
            rtn_data = (
                torrent_data["parsed"] if torrent_data["cached"] else torrent.data
            )

            debrid_emoji = (
                "🧲"
                if debrid_service == "torrent"
                else ("⚡" if torrent_data["cached"] else "⬇️")
            )

            the_stream = {
                "name": f"[{debrid_extension}{debrid_emoji}] Comet {rtn_data.resolution}",
                "description": format_title(
                    rtn_data,
                    torrent_data["title"],
                    torrent_data["seeders"],
                    torrent_data["size"],
                    torrent_data["tracker"],
                    config["resultFormat"],
                ),
                "behaviorHints": {
                    "bingeGroup": "comet|" + info_hash,
                    "videoSize": torrent_data["size"],
                    "filename": rtn_data.raw_title,
                },
            }

            if debrid_service == "torrent":
                the_stream["infoHash"] = info_hash
                the_stream["fileIdx"] = torrent_data["fileIndex"]
                the_stream["sources"] = torrent_data["sources"]
            else:
                the_stream["url"] = (
                    f"{request.url.scheme}://{request.url.netloc}/{b64config}/playback/{info_hash}/{torrent_data['fileIndex']}"
                )

            if torrent_data["cached"]:
                cached_results.append(the_stream)
            else:
                non_cached_results.append(the_stream)

        return {"streams": cached_results + non_cached_results}


@streams.get("/{b64config}/playback/{hash}/{index}")
async def playback(request: Request, b64config: str, hash: str, index: str):
    config = config_check(b64config)
    if not config:
        return FileResponse("comet/assets/invalidconfig.mp4")

    async with aiohttp.ClientSession() as session:
        current_time = time.time()
        cached_link = await database.fetch_one(
            f"SELECT download_url FROM download_links_cache WHERE debrid_key = '{config['debridApiKey']}' AND info_hash = '{hash}' AND file_index = '{index}' AND timestamp + 3600 >= :current_time",
            {"current_time": current_time},
        )

        download_url = None
        if cached_link:
            download_url = cached_link["download_url"]

        ip = get_client_ip(request)
        if download_url is None:
            debrid = get_debrid(
                session,
                None,
                config["stremthruUrl"],
                config["debridService"],
                config["debridApiKey"],
                ip,
            )
            download_url = await debrid.generate_download_link(hash, index)
            if not download_url:
                return FileResponse("comet/assets/uncached.mp4")

            await database.execute(
                f"INSERT {'OR IGNORE ' if settings.DATABASE_TYPE == 'sqlite' else ''}INTO download_links_cache VALUES (:debrid_key, :info_hash, :file_index, :download_url, :timestamp){' ON CONFLICT DO NOTHING' if settings.DATABASE_TYPE == 'postgresql' else ''}",
                {
                    "debrid_key": config["debridApiKey"],
                    "info_hash": hash,
                    "file_index": index,
                    "download_url": download_url,
                    "timestamp": current_time,
                },
            )

        if (
            settings.PROXY_DEBRID_STREAM
            and settings.PROXY_DEBRID_STREAM_PASSWORD
            == config["debridStreamProxyPassword"]
        ):
            return await mediaflow_proxy.handlers.handle_stream_request(
                request.method,
                download_url,
                mediaflow_proxy.utils.http_utils.get_proxy_headers(request),
            )

        return RedirectResponse(download_url, status_code=302)
