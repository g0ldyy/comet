from urllib.parse import quote

import aiohttp
from fastapi import APIRouter, BackgroundTasks, Request

from comet.core.config_validation import config_check
from comet.core.logger import logger
from comet.core.models import settings
from comet.debrid.exceptions import DebridAuthError
from comet.debrid.manager import get_debrid_extension
from comet.metadata.filter import release_filter
from comet.metadata.manager import MetadataScraper
from comet.services.anime import anime_mapper
from comet.services.cache_state import CacheStateManager
from comet.services.debrid import DebridService
from comet.services.lock import DistributedLock
from comet.services.orchestration import TorrentManager
from comet.services.trackers import trackers
from comet.utils.cache import (CachedJSONResponse, CachePolicies,
                               check_etag_match, generate_etag,
                               not_modified_response)
from comet.utils.formatting import (format_chilllink, format_title,
                                    get_formatted_components)
from comet.utils.network import get_client_ip
from comet.utils.parsing import parse_media_id

streams = APIRouter()


def _build_stream_response(
    request: Request,
    content: dict,
    is_empty: bool = False,
    vary_headers: list = None,
):
    if not settings.HTTP_CACHE_ENABLED:
        return content

    etag = generate_etag(content)

    if check_etag_match(request, etag):
        return not_modified_response(etag)

    if is_empty:
        cache_policy = CachePolicies.empty_results()
        vary = ["Accept", "Accept-Encoding"]
    else:
        cache_policy = CachePolicies.streams()
        vary = ["Accept", "Accept-Encoding"]

    if vary_headers:
        vary.extend(vary_headers)

    return CachedJSONResponse(
        content=content,
        cache_control=cache_policy,
        etag=etag,
        vary=list(set(vary)),
    )


async def background_scrape(
    torrent_manager: TorrentManager,
    media_id: str,
    debrid_service: str,
):
    scrape_lock = DistributedLock(media_id)
    lock_acquired = await scrape_lock.acquire()

    if not lock_acquired:
        logger.log(
            "SCRAPER",
            f"🔒 Background scrape skipped for {media_id} - already in progress",
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
                f"📥 Background scrape complete for {media_id}!",
            )
    except Exception as e:
        logger.log("SCRAPER", f"❌ Background scrape failed for {media_id}: {e}")
    finally:
        await scrape_lock.release()


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
        return _build_stream_response(request, {"streams": []}, is_empty=True)

    if "tmdb:" in media_id:
        return _build_stream_response(request, {"streams": []}, is_empty=True)

    media_id = media_id.replace("imdb_id:", "")

    config = config_check(b64config)
    if not config:
        error_response = {
            "streams": [
                {
                    "name": "[❌] Comet",
                    "description": f"⚠️ OBSOLETE CONFIGURATION, PLEASE RE-CONFIGURE ON {request.url.scheme}://{request.url.netloc} ⚠️",
                    "url": "https://comet.feels.legal",
                }
            ]
        }
        return _build_stream_response(request, error_response, is_empty=True)

    is_torrent = config["debridService"] == "torrent"
    if settings.DISABLE_TORRENT_STREAMS and is_torrent:
        placeholder_stream = {
            "name": settings.TORRENT_DISABLED_STREAM_NAME,
            "description": settings.TORRENT_DISABLED_STREAM_DESCRIPTION,
        }
        if settings.TORRENT_DISABLED_STREAM_URL:
            placeholder_stream["url"] = settings.TORRENT_DISABLED_STREAM_URL

        return _build_stream_response(request, {"streams": [placeholder_stream]})

    connector = aiohttp.TCPConnector(limit=0)
    async with aiohttp.ClientSession(connector=connector) as session:
        metadata_scraper = MetadataScraper(session)

        id, season, episode = parse_media_id(media_type, media_id)

        if settings.DIGITAL_RELEASE_FILTER:
            is_released = await release_filter.check_is_released(
                session, media_type, media_id, season, episode
            )

            if not is_released:
                logger.log("FILTER", f"🚫 {media_id} is not released yet. Skipping.")
                return _build_stream_response(
                    request,
                    {
                        "streams": [
                            {
                                "name": "[🚫] Comet",
                                "description": "Content not digitally released yet.",
                                "url": "https://comet.feels.legal",
                            }
                        ]
                    },
                    is_empty=True,
                )

        metadata, aliases = await metadata_scraper.fetch_metadata_and_aliases(
            media_type, media_id, id, season, episode
        )

        if metadata is None:
            logger.log("SCRAPER", f"❌ Failed to fetch metadata for {media_id}")
            return _build_stream_response(
                request,
                {
                    "streams": [
                        {
                            "name": "[⚠️] Comet",
                            "description": "Unable to get metadata.",
                            "url": "https://comet.feels.legal",
                        }
                    ]
                },
                is_empty=True,
            )

        title = metadata["title"]
        year = metadata["year"]
        year_end = metadata["year_end"]
        season = metadata["season"]
        episode = metadata["episode"]

        log_title = f"({media_id}) {title}"
        if media_type == "series" and episode is not None:
            log_title += f" S{season:02d}E{episode:02d}"

        logger.log("SCRAPER", f"🔍 Starting search for {log_title}")

        media_only_id = id

        debrid_service = config["debridService"]

        debrid_service_instance = DebridService(
            config["debridService"],
            config["debridApiKey"],
            get_client_ip(request),
        )

        is_kitsu = media_id.startswith("kitsu:")
        search_episode = episode
        search_season = season

        if is_kitsu and episode is not None:
            kitsu_mapping = anime_mapper.get_kitsu_episode_mapping(id)
            if kitsu_mapping:
                from_episode = kitsu_mapping.get("from_episode")
                from_season = kitsu_mapping.get("from_season")
                if from_episode:
                    new_episode = from_episode + episode - 1
                    if new_episode != episode:
                        search_episode = new_episode

                if from_season and from_season != season:
                    search_season = from_season
                if search_season != season or search_episode != episode:
                    logger.log(
                        "SCRAPER",
                        f"📺 Multi-part anime detected (kitsu:{id}): searching for S{search_season:02d}E{search_episode:02d} instead of S{season:02d}E{episode:02d}",
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
            is_kitsu=is_kitsu,
            search_episode=search_episode,
            search_season=search_season,
        )

        await torrent_manager.get_cached_torrents()
        torrent_count = len(torrent_manager.torrents)
        logger.log("SCRAPER", f"📦 Found cached torrents: {torrent_count}")

        cache_manager = CacheStateManager(
            media_id=media_id,
            media_only_id=media_only_id,
            season=season,
            episode=episode,
            is_kitsu=is_kitsu,
            search_episode=search_episode,
        )
        cache_result = await cache_manager.check_and_decide(torrent_count)

        sort_mixed = is_torrent or config["sortCachedUncachedTogether"]
        cached_results = []
        non_cached_results = []

        if cache_result.should_return_wait_message:
            logger.log(
                "SCRAPER",
                f"🔄 Another instance is scraping {log_title}, returning early",
            )
            return _build_stream_response(
                request,
                {
                    "streams": [
                        {
                            "name": "[🔄] Comet",
                            "description": "Scraping in progress, please try again in a few seconds...",
                            "url": "https://comet.feels.legal",
                        }
                    ]
                },
                is_empty=True,
            )

        if cache_result.should_show_first_search_message:
            cached_results.append(
                {
                    "name": "[🔄] Comet",
                    "description": "First search for this media - More results will be available in a few seconds...",
                    "url": "https://comet.feels.legal",
                }
            )

        if cache_result.should_scrape_background:
            logger.log(
                "SCRAPER",
                f"🔄 Starting background scrape for {log_title} (state={cache_result.state.value})",
            )
            background_tasks.add_task(
                background_scrape, torrent_manager, media_id, debrid_service
            )

        if cache_result.should_scrape_now:
            logger.log("SCRAPER", f"🔎 Starting new search for {log_title}")
            try:
                await torrent_manager.scrape_torrents()
                logger.log(
                    "SCRAPER",
                    f"📥 Torrents after global RTN filtering: {len(torrent_manager.torrents)}",
                )
            finally:
                await cache_manager.release_lock()

        await debrid_service_instance.check_existing_availability(
            torrent_manager.torrents, season, episode
        )
        cached_count = sum(
            1 for torrent in torrent_manager.torrents.values() if torrent["cached"]
        )
        total_count = len(torrent_manager.torrents)

        needs_debrid_check = (
            total_count > 0
            and debrid_service != "torrent"
            and (
                not cache_result.has_cached_torrents
                or cached_count == 0
                or (cached_count / total_count) < settings.DEBRID_CACHE_CHECK_RATIO
            )
        )

        if needs_debrid_check:
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
                return _build_stream_response(
                    request,
                    {
                        "streams": [
                            {
                                "name": "[❌] Comet",
                                "description": e.display_message,
                                "url": "https://comet.feels.legal",
                            }
                        ]
                    },
                    is_empty=True,
                )

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
                    "url": "https://comet.feels.legal",
                }
            )

        result_season = season if season is not None else "n"
        result_episode = episode if episode is not None else "n"

        torrents = torrent_manager.torrents
        base_playback_host = (
            settings.PUBLIC_BASE_URL
            if settings.PUBLIC_BASE_URL
            else f"{request.url.scheme}://{request.url.netloc}"
        )
        for info_hash in torrent_manager.ranked_torrents:
            torrent = torrents[info_hash]
            rtn_data = torrent["parsed"]

            debrid_emoji = "🧲" if is_torrent else ("⚡" if torrent["cached"] else "⬇️")

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

            if is_torrent:
                the_stream["infoHash"] = info_hash

                if torrent["fileIndex"] is not None:
                    the_stream["fileIdx"] = torrent["fileIndex"]

                sources = torrent["sources"] or trackers
                if sources:
                    the_stream["sources"] = sources
            else:
                the_stream["url"] = (
                    f"{base_playback_host}/{b64config}/playback/{info_hash}/{torrent['fileIndex'] if torrent['cached'] and torrent['fileIndex'] is not None else 'n'}/{result_season}/{result_episode}/{quote(torrent_title)}?name={quote(title)}"
                )

            if sort_mixed:
                cached_results.append(the_stream)
            elif torrent["cached"]:
                cached_results.append(the_stream)
            else:
                non_cached_results.append(the_stream)

        if sort_mixed:
            final_streams = cached_results
        else:
            final_streams = cached_results + non_cached_results

        has_results = len(final_streams) > 0

        return _build_stream_response(
            request,
            {"streams": final_streams},
            is_empty=not has_results,
        )
