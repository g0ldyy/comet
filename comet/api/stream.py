import aiohttp
import time
import asyncio
import mediaflow_proxy.utils.http_utils

from fastapi import APIRouter, Request, BackgroundTasks
from fastapi.responses import (
    FileResponse,
    RedirectResponse,
)

from comet.utils.models import settings, database, trackers
from comet.metadata.manager import MetadataScraper
from comet.scrapers.manager import TorrentManager
from comet.utils.general import config_check, format_title, get_client_ip
from comet.debrid.manager import get_debrid_extension, get_debrid
from comet.utils.streaming import custom_handle_stream_request
from comet.utils.logger import logger

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
            logger.log("SCRAPER", f"❌ Failed to fetch metadata for {media_id}")
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

        logger.log("SCRAPER", f"🔍 Starting search for {log_title}")

        debrid_service = config["debridService"]
        torrent_manager = TorrentManager(
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
        logger.log(
            "SCRAPER", f"📦 Found cached torrents: {len(torrent_manager.torrents)}"
        )

        if len(torrent_manager.torrents) == 0:
            cached = False
            ongoing_search = await database.fetch_one(
                "SELECT * FROM ongoing_searches WHERE media_id = :media_id AND timestamp + 120 >= :current_time",
                {"media_id": media_id, "current_time": time.time()},
            )
            if ongoing_search:
                logger.log(
                    "SCRAPER", f"⏳ Ongoing search detected for {log_title}, waiting..."
                )
                while ongoing_search:
                    await asyncio.sleep(10)
                    ongoing_search = await database.fetch_one(
                        "SELECT * FROM ongoing_searches WHERE media_id = :media_id AND timestamp + 120 >= :current_time",
                        {"media_id": media_id, "current_time": time.time()},
                    )

                await torrent_manager.get_cached_torrents()
                if len(torrent_manager.torrents) != 0:
                    cached = True
                    logger.log(
                        "SCRAPER",
                        f"✅ New cached torrents found: {len(torrent_manager.torrents)}",
                    )

            if not cached:
                logger.log("SCRAPER", f"🔎 Starting new search for {log_title}")
                await database.execute(
                    f"INSERT {'OR IGNORE ' if settings.DATABASE_TYPE == 'sqlite' else ''}INTO ongoing_searches VALUES (:media_id, :timestamp){' ON CONFLICT DO NOTHING' if settings.DATABASE_TYPE == 'postgresql' else ''}",
                    {"media_id": media_id, "timestamp": time.time()},
                )
                background_tasks.add_task(remove_ongoing_search_from_database, media_id)
                initial_count = len(torrent_manager.torrents)
                await torrent_manager.scrape_torrents(session)
                logger.log(
                    "SCRAPER",
                    f"📥 Scraped torrents: {len(torrent_manager.torrents) - initial_count}",
                )

        await torrent_manager.get_cached_availability()
        cached_count = sum(
            1 for torrent in torrent_manager.torrents.values() if torrent["cached"]
        )
        logger.log(
            "SCRAPER",
            f"💾 Available cached torrents: {cached_count}/{len(torrent_manager.torrents)}",
        )

        if debrid_service != "torrent" and not any(
            torrent["cached"] for torrent in torrent_manager.torrents.values()
        ):
            logger.log("SCRAPER", "🔄 Checking availability on debrid service...")
            await torrent_manager.get_and_cache_debrid_availability(session)

        initial_torrent_count = len(torrent_manager.torrents)
        torrent_manager.rank_torrents(
            config["rtnSettings"],
            config["rtnRanking"],
            config["maxResultsPerResolution"],
            config["maxSize"],
            config["cachedOnly"],
            config["removeTrash"],
        )
        logger.log(
            "SCRAPER",
            f"⚖️ Torrents after RTN filtering: {len(torrent_manager.ranked_torrents)}/{initial_torrent_count}",
        )

        debrid_extension = get_debrid_extension(debrid_service)
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

            if debrid_service == "torrent":
                cached_index = await database.fetch_one(
                    """
                    SELECT file_index, file_size 
                    FROM torrent_file_indexes 
                    WHERE info_hash = :info_hash
                    AND ((cast(:season as INTEGER) IS NULL AND season IS NULL) OR season = cast(:season as INTEGER))
                    AND ((cast(:episode as INTEGER) IS NULL AND episode IS NULL) OR episode = cast(:episode as INTEGER))
                    AND timestamp + :cache_ttl >= :current_time
                    """,
                    {
                        "info_hash": info_hash,
                        "season": metadata["season"],
                        "episode": metadata["episode"],
                        "cache_ttl": settings.CACHE_TTL,
                        "current_time": time.time(),
                    },
                )

                if cached_index:
                    torrent_data["fileIndex"] = cached_index["file_index"]
                    torrent_data["size"] = cached_index["file_size"]

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

                if torrent_data["tracker"] == "DMM":  # Generic trackers for DMM
                    the_stream["sources"] = trackers
                else:
                    the_stream["sources"] = torrent_data["sources"]
            else:
                the_stream["url"] = (
                    f"{request.url.scheme}://{request.url.netloc}/{b64config}/playback/{info_hash}/{torrent_data['fileIndex'] if torrent_data['cached'] else 'n'}/{title}/{season if season is not None else 'n'}/{episode if episode is not None else 'n'}"
                )

            if torrent_data["cached"]:
                cached_results.append(the_stream)
            else:
                non_cached_results.append(the_stream)

        return {"streams": cached_results + non_cached_results}


@streams.get("/{b64config}/playback/{hash}/{index}/{name}/{season}/{episode}")
async def playback(
    request: Request,
    b64config: str,
    hash: str,
    index: str,
    name: str,
    season: str,
    episode: str,
):
    config = config_check(b64config)
    if not config:
        return FileResponse("comet/assets/invalidconfig.mp4")

    async with aiohttp.ClientSession() as session:
        cached_link = await database.fetch_one(
            f"SELECT download_url FROM download_links_cache WHERE debrid_key = '{config['debridApiKey']}' AND info_hash = '{hash}' AND ((cast(:season as INTEGER) IS NULL AND season IS NULL) OR season = cast(:season as INTEGER)) AND ((cast(:episode as INTEGER) IS NULL AND episode IS NULL) OR episode = cast(:episode as INTEGER)) AND timestamp + 3600 >= :current_time",
            {
                "current_time": time.time(),
                "season": season if season != "n" else None,
                "episode": episode if episode != "n" else None,
            },
        )

        download_url = None
        if cached_link:
            download_url = cached_link["download_url"]

        ip = get_client_ip(request)
        if download_url is None:
            debrid = get_debrid(
                session,
                None,
                config["debridService"],
                config["debridApiKey"],
                ip,
            )
            download_url = await debrid.generate_download_link(
                hash, index, name, season, episode
            )
            if not download_url:
                return FileResponse("comet/assets/uncached.mp4")

            query = f"""
            INSERT {'OR IGNORE ' if settings.DATABASE_TYPE == 'sqlite' else ''}
            INTO download_links_cache (debrid_key, info_hash, name, season, episode, download_url, timestamp)
            VALUES (:debrid_key, :info_hash, :name, :season, :episode, :download_url, :timestamp)
            {' ON CONFLICT DO NOTHING' if settings.DATABASE_TYPE == 'postgresql' else ''}
            """

            await database.execute(
                query,
                {
                    "debrid_key": config["debridApiKey"],
                    "info_hash": hash,
                    "name": name,
                    "season": season if season != "n" else None,
                    "episode": episode if episode != "n" else None,
                    "download_url": download_url,
                    "timestamp": time.time(),
                },
            )

        if (
            settings.PROXY_DEBRID_STREAM
            and settings.PROXY_DEBRID_STREAM_PASSWORD
            == config["debridStreamProxyPassword"]
        ):
            return await custom_handle_stream_request(
                request.method,
                download_url,
                mediaflow_proxy.utils.http_utils.get_proxy_headers(request),
                media_id=hash,
                ip=ip,
            )

        return RedirectResponse(download_url, status_code=302)
