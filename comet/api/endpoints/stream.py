import asyncio
from collections import defaultdict
from urllib.parse import quote

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
from comet.utils.http_client import http_client_manager
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
    debrid_entries: list,
    ip: str,
    session,
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
        await torrent_manager.scrape_torrents()

        if debrid_entries and len(torrent_manager.torrents) > 0:
            await get_and_cache_multi_service_availability(
                session,
                debrid_entries,
                torrent_manager.torrents,
                torrent_manager.media_id,
                torrent_manager.media_only_id,
                torrent_manager.season,
                torrent_manager.episode,
                ip,
            )

        logger.log(
            "SCRAPER",
            f"📥 Background scrape complete for {media_id}!",
        )
    except Exception as e:
        logger.log("SCRAPER", f"❌ Background scrape failed for {media_id}: {e}")
    finally:
        await scrape_lock.release()


async def check_multi_service_availability(
    debrid_entries: list,
    torrents: dict,
    season: int,
    episode: int,
):
    service_cache_status = defaultdict(dict)
    info_hashes = list(torrents.keys())
    if not info_hashes or not debrid_entries:
        return service_cache_status

    async def check_service(entry):
        service = entry["service"]
        api_key = entry["apiKey"]

        debrid_instance = DebridService(service, api_key, "")
        cached_hashes = await debrid_instance.check_existing_availability(
            info_hashes, season, episode, torrents
        )

        return service, cached_hashes

    if debrid_entries:
        results = await asyncio.gather(
            *[check_service(e) for e in debrid_entries], return_exceptions=True
        )

        for result in results:
            if isinstance(result, Exception):
                logger.log("DEBRID", f"❌ Error checking availability: {result}")
                continue
            service, cached_hashes = result
            for info_hash in cached_hashes:
                service_cache_status[info_hash][service] = True

    return service_cache_status


async def get_and_cache_multi_service_availability(
    session,
    debrid_entries: list,
    torrents: dict,
    media_id: str,
    media_only_id: str,
    season: int,
    episode: int,
    ip: str,
):
    service_cache_status = defaultdict(dict)
    errors = {}
    info_hashes = list(torrents.keys())

    if not info_hashes or not debrid_entries:
        return service_cache_status, errors

    seeders_map = {h: torrents[h]["seeders"] for h in info_hashes}
    tracker_map = {h: torrents[h]["tracker"] for h in info_hashes}
    sources_map = {h: torrents[h]["sources"] for h in info_hashes}

    unique_services = {}
    for entry in debrid_entries:
        if entry["service"] not in unique_services:
            unique_services[entry["service"]] = entry

    async def check_service(entry):
        service = entry["service"]
        api_key = entry["apiKey"]

        try:
            debrid_instance = DebridService(service, api_key, ip)
            cached_hashes = await debrid_instance.get_and_cache_availability(
                session,
                info_hashes,
                seeders_map,
                tracker_map,
                sources_map,
                torrents,
                media_id,
                media_only_id,
                season,
                episode,
            )

            return service, cached_hashes, None
        except Exception as e:
            return service, None, e

    if unique_services:
        results = await asyncio.gather(
            *[check_service(e) for e in unique_services.values()],
            return_exceptions=True,
        )

        for result in results:
            service, cache_map, error = result
            if error:
                if isinstance(error, DebridAuthError):
                    errors[service] = error
                else:
                    logger.log(
                        "DEBRID",
                        f"❌ Error checking availability on {service}: {error}",
                    )
                continue

            if cache_map:
                for info_hash in cache_map:
                    service_cache_status[info_hash][service] = True

    return service_cache_status, errors


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

    debrid_entries = config.get("_debridEntries", [])
    enable_torrent = config.get("_enableTorrent", False)
    deduplicate_streams = config.get("deduplicateStreams", False)

    is_torrent_only = enable_torrent and not debrid_entries

    if settings.DISABLE_TORRENT_STREAMS and is_torrent_only:
        placeholder_stream = {
            "name": settings.TORRENT_DISABLED_STREAM_NAME,
            "description": settings.TORRENT_DISABLED_STREAM_DESCRIPTION,
        }
        if settings.TORRENT_DISABLED_STREAM_URL:
            placeholder_stream["url"] = settings.TORRENT_DISABLED_STREAM_URL

        return _build_stream_response(request, {"streams": [placeholder_stream]})

    session = await http_client_manager.get_session()
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
    ip = get_client_ip(request)

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

    cache_media_ids = [(media_only_id, is_kitsu)]
    if anime_mapper.is_loaded():
        if is_kitsu:
            imdb_id = await anime_mapper.get_imdb_from_kitsu(id)
            if imdb_id:
                cache_media_ids.append((imdb_id, False))
        elif anime_mapper.is_anime_content(media_id, media_only_id):
            kitsu_id = await anime_mapper.get_kitsu_from_imdb(id)
            if kitsu_id:
                cache_media_ids.append((kitsu_id, True))

    torrent_manager = TorrentManager(
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
        cache_media_ids=cache_media_ids,
    )

    await torrent_manager.get_cached_torrents()
    torrent_count = len(torrent_manager.torrents)
    logger.log("SCRAPER", f"📦 Found cached torrents: {torrent_count}")
    primary_cached = torrent_manager.primary_cached

    cache_manager = CacheStateManager(
        media_id=media_id,
        media_only_id=media_only_id,
        season=season,
        episode=episode,
        is_kitsu=is_kitsu,
        search_episode=search_episode,
        search_season=search_season,
        cache_media_ids=cache_media_ids,
    )
    cache_result = await cache_manager.check_and_decide(torrent_count)
    force_scrape_now = not primary_cached
    lock_acquired = cache_result.lock_acquired
    debrid_season = search_season if is_kitsu else season
    debrid_episode = search_episode if is_kitsu else episode

    sort_mixed = is_torrent_only or config["sortCachedUncachedTogether"]
    cached_results = []
    non_cached_results = []

    def _wait_response():
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

    if force_scrape_now and not lock_acquired:
        lock_acquired = await cache_manager.try_acquire_lock()

    if force_scrape_now and not lock_acquired:
        return _wait_response()

    if cache_result.should_return_wait_message and not force_scrape_now:
        return _wait_response()

    if cache_result.should_show_first_search_message:
        cached_results.append(
            {
                "name": "[🔄] Comet",
                "description": "First search for this media - More results will be available in a few seconds...",
                "url": "https://comet.feels.legal",
            }
        )

    if cache_result.should_scrape_background and not force_scrape_now:
        logger.log(
            "SCRAPER",
            f"🔄 Starting background scrape for {log_title} (state={cache_result.state.value})",
        )
        background_tasks.add_task(
            background_scrape,
            torrent_manager,
            media_id,
            debrid_entries,
            ip,
            session,
        )

    if cache_result.should_scrape_now or force_scrape_now:
        logger.log("SCRAPER", f"🔎 Starting new search for {log_title}")
        try:
            await torrent_manager.scrape_torrents()
            logger.log(
                "SCRAPER",
                f"📥 Torrents after global RTN filtering: {len(torrent_manager.torrents)}",
            )
        finally:
            await cache_manager.release_lock()

    service_cache_status = {}
    if debrid_entries:
        service_cache_status = await check_multi_service_availability(
            debrid_entries, torrent_manager.torrents, debrid_season, debrid_episode
        )
    elif enable_torrent:
        await DebridService.apply_cached_availability_any_service(
            list(torrent_manager.torrents.keys()),
            debrid_season,
            debrid_episode,
            torrent_manager.torrents,
        )

    total_cached_count = 0
    for info_hash in torrent_manager.torrents:
        for service in service_cache_status.get(info_hash, {}).values():
            if service:
                total_cached_count += 1
                break

    total_count = len(torrent_manager.torrents)

    needs_debrid_check = (
        total_count > 0
        and debrid_entries
        and (
            not cache_result.has_cached_torrents
            or total_cached_count == 0
            or (total_cached_count / total_count) < settings.DEBRID_CACHE_CHECK_RATIO
        )
    )

    debrid_errors = {}
    if needs_debrid_check:
        services_str = "+".join([e["service"] for e in debrid_entries])
        logger.log(
            "SCRAPER",
            f"🔄 Checking availability on debrid services: {services_str}",
        )
        (
            service_cache_status,
            debrid_errors,
        ) = await get_and_cache_multi_service_availability(
            session,
            debrid_entries,
            torrent_manager.torrents,
            media_id,
            media_only_id,
            debrid_season,
            debrid_episode,
            ip,
        )

        for service, error in debrid_errors.items():
            cached_results.append(
                {
                    "name": f"[❌] {service}",
                    "description": error.display_message,
                    "url": "https://comet.feels.legal",
                }
            )

    if debrid_entries:
        for entry in debrid_entries:
            service = entry["service"]
            cached_count = sum(
                1
                for h in torrent_manager.torrents
                if service_cache_status.get(h, {}).get(service, False)
            )
            logger.log(
                "SCRAPER",
                f"💾 Available cached torrents on {service}: {cached_count}/{len(torrent_manager.torrents)}",
            )

    initial_torrent_count = len(torrent_manager.torrents)

    await torrent_manager.rank_torrents(
        config["rtnSettings"],
        config["rtnRanking"],
        config["maxResultsPerResolution"],
        config["maxSize"],
        config["removeTrash"],
    )
    logger.log(
        "SCRAPER",
        f"⚖️  Torrents after user RTN filtering: {len(torrent_manager.ranked_torrents)}/{initial_torrent_count}",
    )

    if (
        config["debridStreamProxyPassword"] != ""
        and settings.PROXY_DEBRID_STREAM
        and settings.PROXY_DEBRID_STREAM_PASSWORD != config["debridStreamProxyPassword"]
    ):
        cached_results.append(
            {
                "name": "[⚠️] Comet",
                "description": "Debrid Stream Proxy Password incorrect.\nStreams will not be proxied.",
                "url": "https://comet.feels.legal",
            }
        )

    result_season = search_season if is_kitsu else season
    result_episode = search_episode if is_kitsu else episode
    result_season = result_season if result_season is not None else "n"
    result_episode = result_episode if result_episode is not None else "n"

    torrents = torrent_manager.torrents
    base_playback_host = (
        settings.PUBLIC_BASE_URL
        if settings.PUBLIC_BASE_URL
        else f"{request.url.scheme}://{request.url.netloc}"
    )

    added_hashes = set()

    for info_hash in torrent_manager.ranked_torrents:
        torrent = torrents[info_hash]
        rtn_data = torrent["parsed"]
        torrent_title = torrent["title"]

        formatted_components = get_formatted_components(
            rtn_data,
            torrent_title,
            torrent["seeders"],
            torrent["size"],
            torrent["tracker"],
            config["resultFormat"],
        )

        for entry_index, entry in enumerate(debrid_entries):
            service = entry["service"]

            if service in debrid_errors:
                continue

            is_cached = service_cache_status.get(info_hash, {}).get(service, False)

            if config["cachedOnly"] and not is_cached:
                continue

            if deduplicate_streams and info_hash in added_hashes and is_cached:
                continue

            debrid_extension = get_debrid_extension(service)
            debrid_emoji = "⚡" if is_cached else "⬇️"

            the_stream = {
                "name": f"[{debrid_extension}{debrid_emoji}] Comet {rtn_data.resolution}",
                "description": format_title(formatted_components),
                "behaviorHints": {
                    "bingeGroup": f"comet|{service}|{info_hash}",
                    "videoSize": torrent["size"],
                    "filename": rtn_data.raw_title,
                },
            }

            if chilllink:
                the_stream["_chilllink"] = format_chilllink(
                    formatted_components, is_cached
                )

            file_index = torrent.get("fileIndex")
            file_index_str = (
                str(file_index) if is_cached and file_index is not None else "n"
            )
            the_stream["url"] = (
                f"{base_playback_host}/{b64config}/playback/{info_hash}/{entry_index}/{file_index_str}/{result_season}/{result_episode}/{quote(torrent_title)}?name={quote(title)}"
            )

            if is_cached:
                added_hashes.add(info_hash)

            if sort_mixed:
                cached_results.append(the_stream)
            elif is_cached:
                cached_results.append(the_stream)
            else:
                non_cached_results.append(the_stream)

        if enable_torrent:
            if deduplicate_streams and info_hash in added_hashes:
                continue

            torrent_extension = get_debrid_extension("torrent")
            debrid_emoji = "🧲"

            the_stream = {
                "name": f"[{torrent_extension}{debrid_emoji}] Comet {rtn_data.resolution}",
                "description": format_title(formatted_components),
                "behaviorHints": {
                    "bingeGroup": f"comet|torrent|{info_hash}",
                    "videoSize": torrent["size"],
                    "filename": rtn_data.raw_title,
                },
                "infoHash": info_hash,
            }

            if chilllink:
                the_stream["_chilllink"] = format_chilllink(formatted_components, False)

            if torrent.get("fileIndex") is not None:
                the_stream["fileIdx"] = torrent["fileIndex"]

            sources = torrent.get("sources") or trackers
            if sources:
                the_stream["sources"] = sources

            cached_results.append(the_stream)

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
