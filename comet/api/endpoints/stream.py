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
from comet.services.debrid_account_scraper import (
    ensure_account_snapshot_ready, get_account_torrents_for_media,
    ingest_account_torrents_to_public_cache, schedule_account_snapshot_refresh)
from comet.services.lock import DistributedLock
from comet.services.orchestration import TorrentManager
from comet.services.trackers import trackers
from comet.utils.cache import (CachedJSONResponse, CachePolicies,
                               check_etag_match, generate_etag,
                               not_modified_response)
from comet.utils.formatting import (format_chilllink, format_title,
                                    get_formatted_components,
                                    get_formatted_components_plain)
from comet.utils.http_client import http_client_manager
from comet.utils.network import get_client_ip
from comet.utils.parsing import parse_media_id

streams = APIRouter()

RESOLUTION_TO_DIMENSIONS = {
    "4K": (2160, 3840),
    "2160P": (2160, 3840),
    "1440P": (1440, 2560),
    "1080P": (1080, 1920),
    "720P": (720, 1280),
    "576P": (576, 720),
    "480P": (480, 640),
    "360P": (360, 480),
    "240P": (240, 320),
}


def _first_meta_value(value):
    if isinstance(value, list):
        return value[0] if value else ""
    return value or ""


def _build_kodi_meta(parsed, formatted_components: dict):
    resolution_value = getattr(parsed, "resolution", "")
    resolution = str(resolution_value).upper() if resolution_value else ""
    height, width = RESOLUTION_TO_DIMENSIONS.get(resolution, (0, 0))
    languages = getattr(parsed, "languages", None) or []

    return {
        "width": width,
        "height": height,
        "resolution": resolution,
        "codec": _first_meta_value(getattr(parsed, "codec", "")),
        "hdr": _first_meta_value(getattr(parsed, "hdr", "")),
        "audio": _first_meta_value(getattr(parsed, "audio", "")),
        "channels": _first_meta_value(getattr(parsed, "channels", "")),
        "language": languages[0] if languages else "",
        "languages": languages,
        "title": formatted_components.get("title", ""),
        "videoInfo": formatted_components.get("video", ""),
        "audioInfo": formatted_components.get("audio", ""),
        "qualityInfo": formatted_components.get("quality", ""),
        "groupInfo": formatted_components.get("group", ""),
        "seedersInfo": formatted_components.get("seeders", ""),
        "sizeInfo": formatted_components.get("size", ""),
        "trackerInfo": formatted_components.get("tracker", ""),
        "languagesInfo": formatted_components.get("languages", ""),
    }


def _stream_notice_name(kodi: bool, emoji_name: str, plain_name: str):
    return plain_name if kodi else emoji_name


def _build_stream_response(
    request: Request,
    content: dict,
    is_empty: bool = False,
    vary_headers: list = None,
    cache_policy=None,
):
    if not settings.HTTP_CACHE_ENABLED:
        return content

    vary = ["Accept", "Accept-Encoding"]
    if cache_policy is None:
        if is_empty:
            cache_policy = CachePolicies.empty_results()
        else:
            cache_policy = CachePolicies.streams()
    cache_control = cache_policy.build()

    etag = generate_etag(content)
    if check_etag_match(request, etag):
        return not_modified_response(etag, cache_control=cache_control)

    if vary_headers:
        vary.extend(vary_headers)

    return CachedJSONResponse(
        content=content,
        cache_control=cache_policy,
        etag=etag,
        vary=list(dict.fromkeys(vary)),
    )


def _select_info_hashes_by_resolution(
    ranked_info_hashes,
    torrents: dict,
    service_cache_status: dict,
    max_results: int,
    cached_only: bool,
    prioritize_cached: bool,
):
    if max_results <= 0:
        return None

    per_resolution_count = defaultdict(int)
    selected_info_hashes = []

    def try_select(info_hash: str):
        resolution = str(torrents[info_hash]["parsed"].resolution)
        if per_resolution_count[resolution] >= max_results:
            return
        selected_info_hashes.append(info_hash)
        per_resolution_count[resolution] += 1

    is_cached_by_hash = {}
    if prioritize_cached or cached_only:
        is_cached_by_hash = {
            info_hash: any(service_cache_status.get(info_hash, {}).values())
            for info_hash in ranked_info_hashes
        }

    if prioritize_cached:
        for info_hash in ranked_info_hashes:
            if not is_cached_by_hash[info_hash]:
                continue
            try_select(info_hash)

        if cached_only:
            return selected_info_hashes

        for info_hash in ranked_info_hashes:
            if is_cached_by_hash[info_hash]:
                continue
            try_select(info_hash)

        return selected_info_hashes

    for info_hash in ranked_info_hashes:
        if cached_only and not is_cached_by_hash[info_hash]:
            continue
        try_select(info_hash)

    return selected_info_hashes


def _merge_service_cache_status(target: dict, incoming: dict):
    for info_hash, service_map in incoming.items():
        cache_map = target.setdefault(info_hash, {})
        for service, is_cached in service_map.items():
            if is_cached:
                cache_map[service] = True
            elif service not in cache_map:
                cache_map[service] = False


def _dedupe_debrid_entries_by_service(debrid_entries: list) -> list:
    unique_services = {}
    for entry in debrid_entries:
        service = entry["service"]
        if service not in unique_services:
            unique_services[service] = entry
    return list(unique_services.values())


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
                torrent_manager.search_season,
                torrent_manager.search_episode,
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

    unique_services = _dedupe_debrid_entries_by_service(debrid_entries)

    if unique_services:
        results = await asyncio.gather(
            *[check_service(e) for e in unique_services],
            return_exceptions=True,
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

    unique_services = _dedupe_debrid_entries_by_service(debrid_entries)

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
            *[check_service(e) for e in unique_services],
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
    kodi: bool = False,
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
                    "name": _stream_notice_name(kodi, "[❌] Comet", "[ERROR] Comet"),
                    "description": (
                        f"OBSOLETE CONFIGURATION, PLEASE RE-CONFIGURE ON {request.url.scheme}://{request.url.netloc}"
                        if kodi
                        else f"⚠️ OBSOLETE CONFIGURATION, PLEASE RE-CONFIGURE ON {request.url.scheme}://{request.url.netloc} ⚠️"
                    ),
                    "url": "https://comet.feels.legal",
                }
            ]
        }
        return _build_stream_response(request, error_response, is_empty=True)

    debrid_entries = config["_debridEntries"]
    enable_torrent = config["_enableTorrent"]
    deduplicate_streams = config["deduplicateStreams"]
    scrape_debrid_account_torrents = config["scrapeDebridAccountTorrents"]
    use_account_scrape = bool(debrid_entries and scrape_debrid_account_torrents)
    response_cache_policy = CachePolicies.no_cache() if use_account_scrape else None

    def _stream_response(content: dict, is_empty: bool = False):
        return _build_stream_response(
            request,
            content,
            is_empty=is_empty,
            cache_policy=response_cache_policy,
        )

    is_torrent_only = enable_torrent and not debrid_entries

    if settings.DISABLE_TORRENT_STREAMS and is_torrent_only:
        placeholder_stream = {
            "name": settings.TORRENT_DISABLED_STREAM_NAME,
            "description": settings.TORRENT_DISABLED_STREAM_DESCRIPTION,
        }
        if settings.TORRENT_DISABLED_STREAM_URL:
            placeholder_stream["url"] = settings.TORRENT_DISABLED_STREAM_URL

        return _stream_response({"streams": [placeholder_stream]})

    session = await http_client_manager.get_session()
    metadata_scraper = MetadataScraper(session)

    id, season, episode = parse_media_id(media_type, media_id)

    if settings.DIGITAL_RELEASE_FILTER:
        is_released = await release_filter.check_is_released(
            session, media_type, media_id, season, episode
        )

        if not is_released:
            logger.log("FILTER", f"🚫 {media_id} is not released yet. Skipping.")
            return _stream_response(
                {
                    "streams": [
                        {
                            "name": _stream_notice_name(
                                kodi, "[🚫] Comet", "[BLOCKED] Comet"
                            ),
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
        return _stream_response(
            {
                "streams": [
                    {
                        "name": _stream_notice_name(kodi, "[⚠️] Comet", "[WARN] Comet"),
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

    if is_kitsu:
        kitsu_mapping = anime_mapper.get_kitsu_episode_mapping(id)
        if kitsu_mapping:
            from_episode = kitsu_mapping.get("from_episode")
            from_season = kitsu_mapping.get("from_season")
            if from_season is not None and from_season != season:
                search_season = from_season
            if episode is not None and from_episode is not None:
                new_episode = from_episode + episode - 1
                if new_episode != episode:
                    search_episode = new_episode

            if search_season != season or search_episode != episode:
                if episode is not None and search_season is not None:
                    logger.log(
                        "SCRAPER",
                        f"📺 Multi-part anime detected (kitsu:{id}): searching for S{search_season:02d}E{search_episode:02d} instead of S{season:02d}E{episode:02d}",
                    )
                elif search_season is not None and season is not None:
                    logger.log(
                        "SCRAPER",
                        f"📺 Multi-part anime detected (kitsu:{id}): searching for S{search_season:02d} instead of S{season:02d}",
                    )

    cache_media_ids = [media_only_id]
    if anime_mapper.is_loaded():
        if is_kitsu:
            imdb_id = await anime_mapper.get_imdb_from_kitsu(id)
            if imdb_id:
                cache_media_ids.append(imdb_id)
        elif anime_mapper.is_anime_content(media_id, media_only_id):
            kitsu_ids = anime_mapper.get_kitsu_ids_from_imdb(id)
            if kitsu_ids:
                cache_media_ids.extend(kitsu_ids)

            # always include the base IMDb-Kitsu link if present
            kitsu_id = await anime_mapper.get_kitsu_from_imdb(id)
            if kitsu_id and kitsu_id not in cache_media_ids:
                cache_media_ids.append(kitsu_id)

    remove_adult_content = settings.REMOVE_ADULT_CONTENT and config["removeTrash"]
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
        remove_adult_content,
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

    sort_mixed = is_torrent_only or config["sortCachedUncachedTogether"]
    account_snapshot_ready = False
    cached_results = []
    non_cached_results = []

    def _wait_response():
        logger.log(
            "SCRAPER",
            f"🔄 Another instance is scraping {log_title}, returning early",
        )
        return _stream_response(
            {
                "streams": [
                    {
                        "name": _stream_notice_name(kodi, "[🔄] Comet", "[INFO] Comet"),
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
                "name": _stream_notice_name(kodi, "[🔄] Comet", "[INFO] Comet"),
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
            if use_account_scrape:
                scrape_result, warmup_result = await asyncio.gather(
                    torrent_manager.scrape_torrents(),
                    ensure_account_snapshot_ready(session, debrid_entries, ip),
                    return_exceptions=True,
                )
                if isinstance(scrape_result, Exception):
                    raise scrape_result
                if isinstance(warmup_result, Exception):
                    raise warmup_result
                account_snapshot_ready = True
            else:
                await torrent_manager.scrape_torrents()
            logger.log(
                "SCRAPER",
                f"📥 Torrents after global RTN filtering: {len(torrent_manager.torrents)}",
            )
        finally:
            await cache_manager.release_lock()

    service_cache_status = defaultdict(dict)
    verified_service_cache_status = defaultdict(dict)
    show_account_sync_trigger = use_account_scrape
    if use_account_scrape:
        if not account_snapshot_ready:
            await ensure_account_snapshot_ready(session, debrid_entries, ip)
        await schedule_account_snapshot_refresh(
            background_tasks, session, debrid_entries, ip
        )
        account_torrents, account_cache_status = await get_account_torrents_for_media(
            debrid_entries,
            media_type,
            title,
            year,
            year_end,
            search_season,
            search_episode,
            aliases,
            remove_adult_content,
        )

        for info_hash, account_torrent in account_torrents.items():
            existing_torrent = torrent_manager.torrents.get(info_hash)
            if existing_torrent is None:
                torrent_manager.torrents[info_hash] = account_torrent
                continue

            if (
                existing_torrent.get("fileIndex") is None
                and account_torrent["fileIndex"] is not None
            ):
                existing_torrent["fileIndex"] = account_torrent["fileIndex"]

            if (
                existing_torrent.get("size") is None
                and account_torrent["size"] is not None
            ):
                existing_torrent["size"] = account_torrent["size"]

            existing_parsed = existing_torrent.get("parsed")
            if existing_parsed is None or str(existing_parsed.resolution) == "unknown":
                existing_torrent["parsed"] = account_torrent["parsed"]

        if account_torrents:
            logger.log(
                "SCRAPER",
                f"📚 Account scrape added {len(account_torrents)} torrents from debrid snapshots",
            )

            public_cache_ingested = await ingest_account_torrents_to_public_cache(
                account_torrents, media_only_id, search_season
            )
            if public_cache_ingested:
                logger.log(
                    "SCRAPER",
                    f"🌐 Debrid account contributed {public_cache_ingested} rows to public torrent cache",
                )

        _merge_service_cache_status(service_cache_status, account_cache_status)

    if debrid_entries:
        existing_service_cache_status = await check_multi_service_availability(
            debrid_entries, torrent_manager.torrents, search_season, search_episode
        )
        _merge_service_cache_status(service_cache_status, existing_service_cache_status)
        _merge_service_cache_status(
            verified_service_cache_status, existing_service_cache_status
        )
    elif enable_torrent:
        await DebridService.apply_cached_availability_any_service(
            list(torrent_manager.torrents.keys()),
            search_season,
            search_episode,
            torrent_manager.torrents,
        )

    total_count = len(torrent_manager.torrents)
    total_verified_cached_count = 0
    for info_hash in torrent_manager.torrents:
        for service in verified_service_cache_status.get(info_hash, {}).values():
            if service:
                total_verified_cached_count += 1
                break

    needs_debrid_check = (
        total_count > 0
        and debrid_entries
        and (
            (not cache_result.has_cached_torrents and not use_account_scrape)
            or total_verified_cached_count == 0
            or (total_verified_cached_count / total_count)
            < settings.DEBRID_CACHE_CHECK_RATIO
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
            fresh_service_cache_status,
            debrid_errors,
        ) = await get_and_cache_multi_service_availability(
            session,
            debrid_entries,
            torrent_manager.torrents,
            media_id,
            media_only_id,
            search_season,
            search_episode,
            ip,
        )
        _merge_service_cache_status(service_cache_status, fresh_service_cache_status)

        for service, error in debrid_errors.items():
            cached_results.append(
                {
                    "name": (f"[ERROR] {service}" if kodi else f"[❌] {service}"),
                    "description": error.display_message,
                    "url": "https://comet.feels.legal",
                }
            )

    debrid_stream_specs = [
        (entry_index, entry["service"], get_debrid_extension(entry["service"]))
        for entry_index, entry in enumerate(debrid_entries)
    ]
    if debrid_stream_specs:
        seen_services = set()
        for _, service, _ in debrid_stream_specs:
            if service in seen_services:
                continue
            seen_services.add(service)
            cached_count = sum(
                1
                for cache_map in service_cache_status.values()
                if cache_map.get(service, False)
            )
            logger.log(
                "SCRAPER",
                f"💾 Available cached torrents on {service}: {cached_count}/{len(torrent_manager.torrents)}",
            )

    initial_torrent_count = len(torrent_manager.torrents)

    await torrent_manager.rank_torrents(
        config["rtnSettings"],
        config["rtnRanking"],
        0,
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
                "name": _stream_notice_name(kodi, "[⚠️] Comet", "[WARN] Comet"),
                "description": "Debrid Stream Proxy Password incorrect.\nStreams will not be proxied.",
                "url": "https://comet.feels.legal",
            }
        )

    result_season = search_season if search_season is not None else "n"
    result_episode = search_episode if search_episode is not None else "n"

    torrents = torrent_manager.torrents
    base_playback_host = (
        settings.PUBLIC_BASE_URL
        if settings.PUBLIC_BASE_URL
        else f"{request.url.scheme}://{request.url.netloc}"
    )
    quoted_title = quote(title)
    format_components = (
        get_formatted_components_plain if kodi else get_formatted_components
    )
    format_title_fn = format_title
    torrent_extension = get_debrid_extension("torrent")

    if show_account_sync_trigger:
        for entry_index, _, debrid_extension in debrid_stream_specs:
            cached_results.append(
                {
                    "name": (
                        f"[{debrid_extension}] Comet Sync"
                        if kodi
                        else f"[{debrid_extension}🔄] Comet Sync"
                    ),
                    "description": (
                        "Sync debrid account library now.\n"
                        "Select this stream, then retry this title in a few seconds."
                    ),
                    "url": (
                        f"{base_playback_host}/{b64config}/debrid-sync/{entry_index}"
                    ),
                }
            )

    selected_info_hashes = _select_info_hashes_by_resolution(
        ranked_info_hashes=torrent_manager.ranked_torrents,
        torrents=torrents,
        service_cache_status=service_cache_status,
        max_results=config["maxResultsPerResolution"],
        cached_only=bool(
            config["cachedOnly"] and debrid_entries and not enable_torrent
        ),
        prioritize_cached=bool(debrid_entries and not sort_mixed),
    )
    ranked_info_hashes = (
        selected_info_hashes
        if selected_info_hashes is not None
        else torrent_manager.ranked_torrents
    )

    added_hashes = set()

    for info_hash in ranked_info_hashes:
        torrent = torrents[info_hash]
        rtn_data = torrent["parsed"]
        torrent_title = torrent["title"]
        torrent_size = torrent["size"]
        formatted_components = format_components(
            rtn_data,
            torrent_title,
            torrent["seeders"],
            torrent_size,
            torrent["tracker"],
            config["resultFormat"],
        )
        formatted_title = format_title_fn(formatted_components)
        kodi_meta = _build_kodi_meta(rtn_data, formatted_components) if kodi else None
        info_hash_cache_status = service_cache_status.get(info_hash)
        quoted_torrent_title = quote(torrent_title)

        for entry_index, service, debrid_extension in debrid_stream_specs:
            if service in debrid_errors:
                continue

            is_cached = (
                info_hash_cache_status.get(service, False)
                if info_hash_cache_status
                else False
            )

            if config["cachedOnly"] and not is_cached:
                continue

            if deduplicate_streams and info_hash in added_hashes and is_cached:
                continue

            debrid_emoji = "⚡" if is_cached else "⬇️"
            behavior_hints = {
                "bingeGroup": f"comet|{service}|{info_hash}",
                "filename": rtn_data.raw_title,
            }
            if torrent_size is not None:
                behavior_hints["videoSize"] = torrent_size
            if kodi_meta is not None:
                behavior_hints["cometKodiMetaV1"] = kodi_meta

            the_stream = {
                "name": f"[{debrid_extension}{debrid_emoji}] Comet {rtn_data.resolution}",
                "description": formatted_title,
                "behaviorHints": behavior_hints,
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
                f"{base_playback_host}/{b64config}/playback/{info_hash}/{entry_index}/{file_index_str}/{result_season}/{result_episode}/{quoted_torrent_title}?name={quoted_title}"
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

            debrid_emoji = "" if kodi else "🧲"
            behavior_hints = {
                "bingeGroup": f"comet|torrent|{info_hash}",
                "filename": rtn_data.raw_title,
            }
            if torrent_size is not None:
                behavior_hints["videoSize"] = torrent_size
            if kodi_meta is not None:
                behavior_hints["cometKodiMetaV1"] = kodi_meta

            the_stream = {
                "name": f"[{torrent_extension}{debrid_emoji}] Comet {rtn_data.resolution}",
                "description": formatted_title,
                "behaviorHints": behavior_hints,
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

    return _stream_response(
        {"streams": final_streams},
        is_empty=not has_results,
    )
