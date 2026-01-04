import asyncio
import time
from urllib.parse import quote

import aiohttp
from fastapi import APIRouter, BackgroundTasks, Request

from comet.core.config_validation import config_check
from comet.core.logger import logger
from comet.core.models import database, settings, trackers
from comet.debrid.exceptions import DebridAuthError
from comet.debrid.manager import get_debrid_extension
from comet.metadata.filter import release_filter
from comet.metadata.manager import MetadataScraper
from comet.services.debrid import DebridService
from comet.services.lock import DistributedLock, is_scrape_in_progress
from comet.services.orchestration import TorrentManager
from comet.utils.formatting import (format_chilllink, format_title,
                                    get_formatted_components)
from comet.utils.network import get_client_ip
from comet.utils.parsing import parse_media_id

streams = APIRouter()


async def is_first_search(media_id: str):
    params = {"media_id": media_id, "timestamp": time.time()}

    try:
        if settings.DATABASE_TYPE == "sqlite":
            try:
                await database.execute(
                    "INSERT INTO first_searches VALUES (:media_id, :timestamp)",
                    params,
                )
                return True
            except Exception:
                return False

        inserted = await database.fetch_val(
            """
            INSERT INTO first_searches (media_id, timestamp)
            VALUES (:media_id, :timestamp)
            ON CONFLICT (media_id) DO NOTHING
            RETURNING 1
            """,
            params,
            force_primary=True,
        )
        return inserted == 1
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
        async with aiohttp.ClientSession() as session:
            await torrent_manager.scrape_torrents()

            if debrid_service != "torrent" and len(torrent_manager.torrents) > 0:
                debrid_service_instance = DebridService(
                    debrid_service,
                    torrent_manager.debrid_api_key,
                    torrent_manager.ip,
                )

                await debrid_service_instance.get_and_cache_availability(
                    session,
                    torrent_manager.torrents,
                    torrent_manager.media_id,
                    torrent_manager.media_only_id,
                    torrent_manager.season,
                    torrent_manager.episode,
                )

            logger.log(
                "SCRAPER",
                "📥 Background scrape + availability check complete!",
            )
    except Exception as e:
        logger.log("SCRAPER", f"❌ Background scrape + availability check failed: {e}")
    finally:
        await scrape_lock.release()


async def wait_for_scrape_completion(media_id: str, context: str = ""):
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
    b64config: str = None,
    chilllink: bool = False,
):
    if media_type not in ["movie", "series"]:
        return {"streams": []}

    if "tmdb:" in media_id:
        return {"streams": []}

    media_id = media_id.replace("imdb_id:", "")

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

    if settings.DISABLE_TORRENT_STREAMS and config["debridService"] == "torrent":
        placeholder_stream = {
            "name": settings.TORRENT_DISABLED_STREAM_NAME or "[INFO] Comet",
            "description": settings.TORRENT_DISABLED_STREAM_DESCRIPTION
            or "Direct torrent playback is disabled on this server.",
        }
        if settings.TORRENT_DISABLED_STREAM_URL:
            placeholder_stream["url"] = settings.TORRENT_DISABLED_STREAM_URL

        return {"streams": [placeholder_stream]}

    connector = aiohttp.TCPConnector(limit=0)
    async with aiohttp.ClientSession(connector=connector) as session:
        metadata_scraper = MetadataScraper(session)

        id, season, episode = parse_media_id(media_type, media_id)

        # Digital Release Filter
        if settings.DIGITAL_RELEASE_FILTER:
            is_released = await release_filter.check_is_released(
                session, media_type, media_id, season, episode
            )

            if not is_released:
                logger.log("FILTER", f"🚫 {media_id} is not released yet. Skipping.")
                return {
                    "streams": [
                        {
                            "name": "[🚫] Comet",
                            "description": "Content not digitally released yet.",
                            "url": "https://comet.fast",
                        }
                    ]
                }

        # Check if metadata is already cached
        cached_metadata = await metadata_scraper.get_cached(
            id, season if "kitsu" not in media_id else 1, episode
        )

        # Quick check for "fresh" cached torrents to decide if we need to re-scrape.
        # This does NOT filter what torrents are shown, it only determines if a new search is triggered.
        # LIVE_TORRENT_CACHE_TTL controls when cache is considered "stale" and needs refreshing.
        # If -1, cache is never considered stale (all cached torrents are "fresh").
        if settings.LIVE_TORRENT_CACHE_TTL >= 0:
            fresh_cached_count = await database.fetch_val(
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
                    "cache_ttl": settings.LIVE_TORRENT_CACHE_TTL,
                    "current_time": time.time(),
                },
            )
        else:
            # TTL=-1 means cache never expires, count all cached torrents as "fresh"
            fresh_cached_count = await database.fetch_val(
                """
                    SELECT COUNT(*)
                    FROM torrents
                    WHERE media_id = :media_id
                    AND ((season IS NOT NULL AND season = CAST(:season as INTEGER)) OR (season IS NULL AND CAST(:season as INTEGER) IS NULL))
                    AND (episode IS NULL OR episode = CAST(:episode as INTEGER))
                """,
                {
                    "media_id": id,
                    "season": season,
                    "episode": episode,
                },
            )

        # Track if cache is stale (no fresh torrents) for background refresh decision
        cache_is_stale = fresh_cached_count == 0

        # If both metadata and fresh torrents are cached, skip lock entirely
        if cached_metadata is not None and fresh_cached_count > 0:
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

        debrid_service_instance = DebridService(
            config["debridService"],
            config["debridApiKey"],
            get_client_ip(request),
        )

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

        sort_mixed = config["sortCachedUncachedTogether"]
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

        elif is_first or cache_is_stale:
            # Background scrape if first search OR if cache is stale (needs refresh)
            if is_first:
                logger.log(
                    "SCRAPER",
                    f"🔄 First search - starting background scrape for {log_title}",
                )
                cached_results.append(
                    {
                        "name": "[🔄] Comet",
                        "description": "First search for this media - More results will be available in a few seconds...",
                        "url": "https://comet.fast",
                    }
                )
            else:
                logger.log(
                    "SCRAPER",
                    f"🔄 Cache stale - starting background refresh for {log_title}",
                )

            background_tasks.add_task(
                background_scrape, torrent_manager, media_id, debrid_service
            )

        # Perform scraping if lock acquired and needed
        if needs_scraping and scrape_lock:
            try:
                await torrent_manager.scrape_torrents()
                logger.log(
                    "SCRAPER",
                    f"📥 Torrents after global RTN filtering: {len(torrent_manager.torrents)}",
                )
            finally:
                await scrape_lock.release()
                lock_acquired = False  # Mark as released
        elif lock_acquired and scrape_lock:
            # Release lock if we had it but didn't need to scrape
            await scrape_lock.release()
            lock_acquired = False

        await debrid_service_instance.check_existing_availability(
            torrent_manager.torrents, season, episode
        )
        cached_count = sum(
            1 for torrent in torrent_manager.torrents.values() if torrent["cached"]
        )
        total_count = len(torrent_manager.torrents)

        if (
            (
                not has_cached_results
                or cached_count == 0
                or (cached_count / total_count) < settings.DEBRID_CACHE_CHECK_RATIO
            )
            and total_count > 0
            and debrid_service != "torrent"
        ):
            logger.log("SCRAPER", "🔄 Checking availability on debrid service...")
            try:
                await debrid_service_instance.get_and_cache_availability(
                    session,
                    torrent_manager.torrents,
                    media_id,
                    media_only_id,
                    season,
                    episode,
                )
            except DebridAuthError as e:
                return {
                    "streams": [
                        {
                            "name": "[❌] Comet",
                            "description": e.display_message,
                            "url": "https://comet.fast",
                        }
                    ]
                }

        if debrid_service != "torrent":
            cached_count = sum(
                1 for torrent in torrent_manager.torrents.values() if torrent["cached"]
            )

            logger.log(
                "SCRAPER",
                f"💾 Available cached torrents on {debrid_service}: {cached_count}/{len(torrent_manager.torrents)}",
            )

        initial_torrent_count = len(torrent_manager.torrents)

        await torrent_manager.rank_torrents(
            config["rtnSettings"],
            config["rtnRanking"],
            config["maxResultsPerResolution"],
            config["maxSize"],
            config["cachedOnly"],
            config["removeTrash"],
        )
        logger.log(
            "SCRAPER",
            f"⚖️  Torrents after user RTN filtering: {len(torrent_manager.ranked_torrents)}/{initial_torrent_count}",
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
            formatted_components = get_formatted_components(
                rtn_data,
                torrent_title,
                torrent["seeders"],
                torrent["size"],
                torrent["tracker"],
                config["resultFormat"],
            )

            the_stream = {
                "name": f"[{debrid_extension}{debrid_emoji}] Comet {rtn_data.resolution}",
                "description": format_title(formatted_components),
                "behaviorHints": {
                    "bingeGroup": "comet|" + info_hash,
                    "videoSize": torrent["size"],
                    "filename": rtn_data.raw_title,
                },
            }

            if chilllink:
                the_stream["_chilllink"] = format_chilllink(
                    formatted_components, torrent["cached"]
                )

            if debrid_service == "torrent":
                the_stream["infoHash"] = info_hash

                if torrent["fileIndex"] is not None:
                    the_stream["fileIdx"] = torrent["fileIndex"]

                if not torrent["sources"]:
                    the_stream["sources"] = trackers
                else:
                    the_stream["sources"] = torrent["sources"]
            else:
                the_stream["url"] = (
                    f"{request.url.scheme}://{request.url.netloc}/{b64config}/playback/{info_hash}/{torrent['fileIndex'] if torrent['cached'] and torrent['fileIndex'] is not None else 'n'}/{result_season}/{result_episode}/{quote(torrent_title)}?name={quote(title)}"
                )

            if sort_mixed:
                cached_results.append(the_stream)
            elif torrent["cached"]:
                cached_results.append(the_stream)
            else:
                non_cached_results.append(the_stream)

        if sort_mixed:
            return {"streams": cached_results}

        return {"streams": cached_results + non_cached_results}
