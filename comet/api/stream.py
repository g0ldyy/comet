import aiohttp
import asyncio
import time
import orjson
import mediaflow_proxy.utils.http_utils

from urllib.parse import quote
from fastapi import APIRouter, Request, BackgroundTasks
from fastapi.responses import (
    FileResponse,
    RedirectResponse,
)

from comet.utils.models import settings, database, redis_client, trackers
from comet.utils.general import parse_media_id
from comet.metadata.manager import MetadataScraper
from comet.scrapers.manager import TorrentManager
from comet.utils.general import config_check, format_title, get_client_ip
from comet.debrid.manager import get_debrid_extension, get_debrid
from comet.utils.streaming import custom_handle_stream_request
from comet.utils.logger import logger
from comet.utils.distributed_lock import DistributedLock, is_scrape_in_progress

streams = APIRouter()


async def is_first_search(media_id: str):
    try:
        await database.execute(
            "INSERT INTO first_searches VALUES (:media_id, :timestamp)",
            {"media_id": media_id, "timestamp": time.time()},
        )

        return True
    except Exception:
        return False


async def background_scrape(
    torrent_manager: TorrentManager, media_id: str, debrid_service: str
):
    scrape_lock = DistributedLock(media_id)
    lock_acquired = await scrape_lock.acquire()

    if not lock_acquired:
        logger.log(
            "SCRAPER",
            f"🔒 Background scrape skipped for {media_id} - already in progress by another instance",
        )
        return

    try:
        async with aiohttp.ClientSession() as new_session:
            await torrent_manager.scrape_torrents(new_session)

            if debrid_service != "torrent" and len(torrent_manager.torrents) > 0:
                await torrent_manager.get_and_cache_debrid_availability(new_session)

            logger.log(
                "SCRAPER",
                "📥 Background scrape + availability check complete!",
            )
    except Exception as e:
        logger.log("SCRAPER", f"❌ Background scrape + availability check failed: {e}")
    finally:
        await scrape_lock.release()


async def wait_for_scrape_completion(media_id: str, context: str = ""):
    """
    Wait for another scrape to complete for the given media_id.

    Args:
        media_id: The media identifier
        context: Additional context for logging (e.g., "log_title")

    Returns:
        True if scrape completed, False if timeout
    """
    check_interval = 1
    waited_time = 0

    while waited_time < settings.SCRAPE_WAIT_TIMEOUT:
        await asyncio.sleep(check_interval)
        waited_time += check_interval

        if not await is_scrape_in_progress(media_id):
            logger.log(
                "SCRAPER",
                f"✅ Other instance completed scraping for {context or media_id} after {waited_time}s",
            )
            return True

    logger.log(
        "SCRAPER",
        f"⏰ Timeout waiting for other instance to complete scraping {context or media_id} after {settings.SCRAPE_WAIT_TIMEOUT}s",
    )
    return False


@streams.get("/stream/{media_type}/{media_id}.json")
@streams.get("/{b64config}/stream/{media_type}/{media_id}.json")
async def stream(
    request: Request,
    media_type: str,
    media_id: str,
    background_tasks: BackgroundTasks,
    b64config: str = None,
):
    if "tmdb:" in media_id:
        return {"streams": []}

    config = config_check(b64config)
    if not config:
        return {
            "streams": [
                {
                    "name": "[❌] Comet",
                    "description": f"⚠️ OBSOLETE CONFIGURATION, PLEASE RE-CONFIGURE ON {request.url.scheme}://{request.url.netloc} ⚠️",
                    "url": "https://comet.fast",
                }
            ]
        }

    # Check for cached stream results
    stream_cache_key = f"comet:v1:streams:{media_type}:{media_id}:{b64config}"
    if redis_client and redis_client.is_connected():
        cached_streams = await redis_client.get(stream_cache_key)
        if cached_streams:
            try:
                streams_data = orjson.loads(cached_streams) if isinstance(cached_streams, str) else cached_streams
                logger.log("SCRAPER", f"🚀 Serving cached stream results for {media_id}")
                return streams_data
            except (KeyError, orjson.JSONDecodeError):
                pass

    connector = aiohttp.TCPConnector(limit=0)
    async with aiohttp.ClientSession(connector=connector) as session:
        metadata_scraper = MetadataScraper(session)

        # First, check if metadata is already cached
        id, season, episode = parse_media_id(media_type, media_id)
        cached_metadata = await metadata_scraper.get_cached(
            id, season if "kitsu" not in media_id else 1, episode
        )

        # Quick check for cached torrents (without creating TorrentManager yet)
        cached_torrents_count = await database.fetch_val(
            """
                SELECT COUNT(*)
                FROM torrents
                WHERE media_id = :media_id
                AND ((season IS NOT NULL AND season = CAST(:season as INTEGER)) OR (season IS NULL AND CAST(:season as INTEGER) IS NULL))
                AND (episode IS NULL OR episode = CAST(:episode as INTEGER))
                AND timestamp + :cache_ttl >= :current_time
            """,
            {
                "media_id": id,
                "season": season,
                "episode": episode,
                "cache_ttl": settings.TORRENT_CACHE_TTL,
                "current_time": time.time(),
            },
        )

        # If both metadata and torrents are cached, skip lock entirely
        if cached_metadata is not None and cached_torrents_count > 0:
            logger.log("SCRAPER", f"🚀 Fast path: using cached data for {media_id}")
            metadata, aliases = cached_metadata[0], cached_metadata[1]
            # Variables for fast path
            lock_acquired = False
            waited_for_other_scrape = False
            scrape_lock = None
            needs_scraping = False
        else:
            # Something is missing, acquire lock for scraping
            scrape_lock = DistributedLock(media_id)
            lock_acquired = await scrape_lock.acquire()
            waited_for_other_scrape = False
            needs_scraping = False

            if not lock_acquired:
                # Another instance has the lock, wait for completion
                logger.log(
                    "SCRAPER",
                    f"🔄 Another instance is scraping {media_id}, waiting for results...",
                )
                await wait_for_scrape_completion(media_id)
                waited_for_other_scrape = True

                # After waiting, re-check cached metadata
                cached_metadata = await metadata_scraper.get_cached(
                    id, season if "kitsu" not in media_id else 1, episode
                )

            if lock_acquired:
                # We have the lock, scrape metadata normally
                metadata, aliases = await metadata_scraper.fetch_metadata_and_aliases(
                    media_type, media_id
                )
            elif cached_metadata is not None:
                # Use cached metadata after waiting
                metadata, aliases = cached_metadata[0], cached_metadata[1]
            else:
                # No cached metadata available, fallback to scraping
                metadata, aliases = await metadata_scraper.fetch_metadata_and_aliases(
                    media_type, media_id
                )
        if metadata is None:
            if lock_acquired and scrape_lock:
                await scrape_lock.release()
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
        if media_type == "series" and episode is not None:
            log_title += f" S{season:02d}E{episode:02d}"

        logger.log("SCRAPER", f"🔍 Starting search for {log_title}")

        id, season, episode = parse_media_id(media_type, media_id)
        media_only_id = id

        debrid_service = config["debridService"]
        torrent_manager = TorrentManager(
            debrid_service,
            config["debridApiKey"],
            get_client_ip(request),
            media_type,
            media_id,
            media_only_id,
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

        is_first = await is_first_search(media_id)
        has_cached_results = len(torrent_manager.torrents) > 0

        cached_results = []
        non_cached_results = []

        # If we waited for another scrape to complete, check cache again
        if not has_cached_results and waited_for_other_scrape:
            await torrent_manager.get_cached_torrents()
            has_cached_results = len(torrent_manager.torrents) > 0
            logger.log(
                "SCRAPER",
                f"📦 Re-checked cache after waiting: {len(torrent_manager.torrents)} torrents",
            )

        if not has_cached_results:
            if lock_acquired and scrape_lock:
                logger.log("SCRAPER", f"🔎 Starting new search for {log_title}")
                needs_scraping = True
            else:
                # Another process is scraping, wait and check cache periodically
                logger.log(
                    "SCRAPER",
                    f"🔄 Another instance is scraping {log_title}, waiting for results...",
                )

                await wait_for_scrape_completion(media_id, log_title)

                await torrent_manager.get_cached_torrents()
                logger.log(
                    "SCRAPER",
                    f"📦 Found cached torrents after waiting: {len(torrent_manager.torrents)}",
                )

                if len(torrent_manager.torrents) == 0:
                    return {
                        "streams": [
                            {
                                "name": "[🔄] Comet",
                                "description": "Scraping in progress by another instance, please try again in a few seconds...",
                                "url": "https://comet.fast",
                            }
                        ]
                    }

        elif is_first:
            logger.log(
                "SCRAPER",
                f"🔄 Starting background scrape + availability check for {log_title}",
            )

            background_tasks.add_task(
                background_scrape, torrent_manager, media_id, debrid_service
            )

            cached_results.append(
                {
                    "name": "[🔄] Comet",
                    "description": "First search for this media - More results will be available in a few seconds...",
                    "url": "https://comet.fast",
                }
            )

        # Perform scraping if lock acquired and needed
        if needs_scraping and scrape_lock:
            try:
                await torrent_manager.scrape_torrents(session)
                logger.log(
                    "SCRAPER",
                    f"📥 Scraped torrents: {len(torrent_manager.torrents)}",
                )
            finally:
                await scrape_lock.release()
                lock_acquired = False  # Mark as released
        elif lock_acquired and scrape_lock:
            # Release lock if we had it but didn't need to scrape
            await scrape_lock.release()
            lock_acquired = False

        await torrent_manager.get_cached_availability()
        if (
            (
                not has_cached_results
                or sum(
                    1
                    for torrent in torrent_manager.torrents.values()
                    if torrent["cached"]
                )
                == 0
            )
            and len(torrent_manager.torrents) > 0
            and debrid_service != "torrent"
        ):
            logger.log("SCRAPER", "🔄 Checking availability on debrid service...")
            await torrent_manager.get_and_cache_debrid_availability(session)

        if debrid_service != "torrent":
            cached_count = sum(
                1 for torrent in torrent_manager.torrents.values() if torrent["cached"]
            )

            logger.log(
                "SCRAPER",
                f"💾 Available cached torrents on {debrid_service}: {cached_count}/{len(torrent_manager.torrents)}",
            )

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
            f"⚖️  Torrents after RTN filtering: {len(torrent_manager.ranked_torrents)}/{initial_torrent_count}",
        )

        debrid_extension = get_debrid_extension(debrid_service)

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

        result_season = season if season is not None else "n"
        result_episode = episode if episode is not None else "n"

        torrents = torrent_manager.torrents
        for info_hash in torrent_manager.ranked_torrents:
            torrent = torrents[info_hash]
            rtn_data = torrent["parsed"]

            debrid_emoji = (
                "🧲"
                if debrid_service == "torrent"
                else ("⚡" if torrent["cached"] else "⬇️")
            )

            torrent_title = torrent["title"]
            the_stream = {
                "name": f"[{debrid_extension}{debrid_emoji}] Comet {rtn_data.resolution}",
                "description": format_title(
                    rtn_data,
                    torrent_title,
                    torrent["seeders"],
                    torrent["size"],
                    torrent["tracker"],
                    config["resultFormat"],
                ),
                "behaviorHints": {
                    "bingeGroup": "comet|" + info_hash,
                    "videoSize": torrent["size"],
                    "filename": rtn_data.raw_title,
                },
            }

            if debrid_service == "torrent":
                the_stream["infoHash"] = info_hash

                if torrent["fileIndex"] is not None:
                    the_stream["fileIdx"] = torrent["fileIndex"]

                if len(torrent["sources"]) == 0:
                    the_stream["sources"] = trackers
                else:
                    the_stream["sources"] = torrent["sources"]
            else:
                the_stream["url"] = (
                    f"{request.url.scheme}://{request.url.netloc}/{b64config}/playback/{info_hash}/{torrent['fileIndex'] if torrent['cached'] and torrent['fileIndex'] is not None else 'n'}/{quote(title)}/{result_season}/{result_episode}/{quote(torrent_title)}"
                )

            if torrent["cached"]:
                cached_results.append(the_stream)
            else:
                non_cached_results.append(the_stream)

        final_results = {"streams": cached_results + non_cached_results}

        # Cache the final stream results
        if redis_client and redis_client.is_connected() and settings.STREAM_CACHE_TTL > 0:
            await redis_client.set(stream_cache_key, final_results, settings.STREAM_CACHE_TTL)

        return final_results


@streams.get(
    "/{b64config}/playback/{hash}/{index}/{name}/{season}/{episode}/{torrent_name}"
)
async def playback(
    request: Request,
    b64config: str,
    hash: str,
    index: str,
    name: str,
    season: str,
    episode: str,
    torrent_name: str,
):
    config = config_check(b64config)

    season = int(season) if season != "n" else None
    episode = int(episode) if episode != "n" else None

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
                SELECT sources
                FROM torrents
                WHERE info_hash = :info_hash
                LIMIT 1
                """,
                {"info_hash": hash},
            )

            sources = []
            if torrent_data and torrent_data["sources"]:
                sources = orjson.loads(torrent_data["sources"])

            debrid = get_debrid(
                session,
                None,
                None,
                config["debridService"],
                config["debridApiKey"],
                ip if not should_proxy else "",
            )
            download_url = await debrid.generate_download_link(
                hash, index, name, torrent_name, season, episode, sources
            )
            if not download_url:
                return FileResponse("comet/assets/uncached.mp4")

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

        if (
            settings.PROXY_DEBRID_STREAM
            and settings.PROXY_DEBRID_STREAM_PASSWORD
            == config["debridStreamProxyPassword"]
        ):
            return await custom_handle_stream_request(
                request.method,
                download_url,
                mediaflow_proxy.utils.http_utils.get_proxy_headers(request),
                media_id=torrent_name,
                ip=ip,
            )

        return RedirectResponse(download_url, status_code=302)
