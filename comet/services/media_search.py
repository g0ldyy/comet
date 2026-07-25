import asyncio
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from comet.core.logger import logger
from comet.core.models import settings
from comet.core.scrape import ScrapeContext
from comet.debrid.exceptions import DebridAuthError
from comet.metadata.episode_index import EpisodeIndexService
from comet.metadata.filter import release_filter
from comet.metadata.manager import MetadataScraper
from comet.observability import metrics
from comet.services.anime import anime_mapper
from comet.services.cache_state import CacheStateManager, mark_scope_scraped
from comet.services.debrid import DebridService
from comet.services.debrid_account_scraper import (
    ensure_account_snapshot_ready,
    get_account_torrents_for_media,
    ingest_account_torrents_to_public_cache,
    schedule_account_snapshot_refresh,
)
from comet.services.lock import DistributedLock
from comet.services.orchestration import TorrentManager
from comet.utils.http_client import http_client_manager
from comet.utils.parsing import MediaScope, parse_media_id, resolve_media_scope

BackgroundTaskAdder = Callable[..., Any]


class MediaSearchStatus(StrEnum):
    OK = "ok"
    INVALID = "invalid"
    DISABLED = "disabled"
    UNRELEASED = "unreleased"
    METADATA_UNAVAILABLE = "metadata_unavailable"
    BUSY = "busy"


@dataclass(slots=True)
class MediaSearchResult:
    status: MediaSearchStatus
    metadata: dict = field(default_factory=dict)
    aliases: dict = field(default_factory=dict)
    media_scope: MediaScope | None = None
    torrents: dict = field(default_factory=dict)
    ranked_info_hashes: list[str] = field(default_factory=list)
    service_cache_status: dict = field(default_factory=dict)
    debrid_errors: dict = field(default_factory=dict)
    cache_state: str = "unknown"
    media_only_id: str = ""
    search_season: int | None = None
    search_episode: int | None = None
    is_torrent_only: bool = False
    sort_mixed: bool = False
    show_account_sync_trigger: bool = False
    use_account_scrape: bool = False


def episode_matching_policy(
    media_type: str,
    media_only_id: str,
    search_season: int | None,
    search_episode: int | None,
    *,
    cached_only: bool,
    has_debrid: bool,
    enable_torrent: bool,
) -> tuple[bool, bool]:
    is_imdb_episode_request = (
        media_type == "series"
        and search_season is not None
        and search_episode is not None
        and media_only_id.startswith("tt")
    )
    allow_debrid_verified_season_packs = (
        is_imdb_episode_request and cached_only and has_debrid and not enable_torrent
    )
    reject_unknown_episode_files = (
        is_imdb_episode_request and not allow_debrid_verified_season_packs
    )
    return is_imdb_episode_request, reject_unknown_episode_files


def merge_service_cache_status(target: dict, incoming: dict):
    for info_hash, service_map in incoming.items():
        cache_map = target.setdefault(info_hash, {})
        for service, is_cached in service_map.items():
            if is_cached:
                cache_map[service] = True
            elif service not in cache_map:
                cache_map[service] = False


async def _mark_scope_scraped_if_populated(media_id: str, torrents: dict) -> None:
    if torrents:
        await mark_scope_scraped(media_id)


def group_debrid_entries_by_service(debrid_entries: list) -> list[tuple[str, list]]:
    service_entries = {}
    seen_credentials = set()
    for entry in debrid_entries:
        service = entry["service"]
        credential = (service, entry["apiKey"])
        if credential in seen_credentials:
            continue
        seen_credentials.add(credential)
        service_entries.setdefault(service, []).append(entry)
    return list(service_entries.items())


def select_debrid_refresh_hashes(
    current_hashes: set[str],
    initial_hashes: set[str],
    verified_cache_status: dict,
    *,
    had_cached_torrents: bool,
    use_account_scrape: bool,
) -> set[str]:
    if not current_hashes:
        return set()

    verified_count = sum(
        any(service_map.values())
        for info_hash, service_map in verified_cache_status.items()
        if info_hash in current_hashes
    )
    requires_full_refresh = (
        (not had_cached_torrents and not use_account_scrape)
        or verified_count == 0
        or (verified_count / len(current_hashes)) < settings.DEBRID_CACHE_CHECK_RATIO
    )
    return current_hashes if requires_full_refresh else current_hashes - initial_hashes


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

    async def run_scrape():
        await torrent_manager.scrape_torrents(ScrapeContext.BACKGROUND)

        if debrid_entries and torrent_manager.torrents:
            await get_and_cache_multi_service_availability(
                session,
                debrid_entries,
                torrent_manager.torrents,
                torrent_manager.media_id,
                torrent_manager.media_only_id,
                torrent_manager.search_season,
                torrent_manager.search_episode,
                torrent_manager.media_scope,
                ip,
                target_air_date=torrent_manager.target_air_date,
            )

    try:
        await scrape_lock.run(run_scrape())
        await _mark_scope_scraped_if_populated(media_id, torrent_manager.torrents)
        logger.log("SCRAPER", f"📥 Background scrape complete for {media_id}!")
    except Exception as exc:
        logger.log("SCRAPER", f"❌ Background scrape failed for {media_id}: {exc}")
    finally:
        await scrape_lock.release()


async def check_multi_service_availability(
    debrid_entries: list,
    torrents: dict,
    season: int | None,
    episode: int | None,
    media_scope: MediaScope,
):
    service_cache_status = defaultdict(dict)
    info_hashes = list(torrents)
    if not info_hashes or not debrid_entries:
        return service_cache_status

    async def check_service(entry):
        service = entry["service"]
        debrid_instance = DebridService(service, entry["apiKey"], "")
        cached_hashes, torrent_updates = (
            await debrid_instance.check_existing_availability(
                info_hashes, season, episode, media_scope, torrents
            )
        )
        return service, cached_hashes, torrent_updates

    service_groups = group_debrid_entries_by_service(debrid_entries)
    results = await asyncio.gather(
        *(check_service(entries[0]) for _, entries in service_groups),
        return_exceptions=True,
    )

    enriched_hashes = set()
    for result in results:
        if isinstance(result, Exception):
            logger.log("DEBRID", f"❌ Error checking availability: {result}")
            continue
        service, cached_hashes, torrent_updates = result
        for info_hash, update in torrent_updates.items():
            if info_hash in enriched_hashes:
                continue
            torrent = torrents.get(info_hash)
            if torrent is not None:
                torrent.update(update)
                enriched_hashes.add(info_hash)
        for info_hash in cached_hashes:
            service_cache_status[info_hash][service] = True

    return service_cache_status


async def get_and_cache_multi_service_availability(
    session,
    debrid_entries: list,
    torrents: dict,
    media_id: str,
    media_only_id: str,
    season: int | None,
    episode: int | None,
    media_scope: MediaScope,
    ip: str,
    target_air_date: str | None = None,
    known_cache_status: dict | None = None,
):
    service_cache_status = defaultdict(dict)
    errors = {}
    info_hashes = list(torrents)

    if not info_hashes or not debrid_entries:
        return service_cache_status, errors

    seeders_map = {info_hash: torrents[info_hash]["seeders"] for info_hash in info_hashes}
    tracker_map = {
        info_hash: torrents[info_hash]["tracker"] for info_hash in info_hashes
    }
    sources_map = {
        info_hash: torrents[info_hash]["sources"] for info_hash in info_hashes
    }
    service_groups = group_debrid_entries_by_service(debrid_entries)
    known_cache_status = known_cache_status or {}

    async def check_service(service, entries):
        service_info_hashes = [
            info_hash
            for info_hash in info_hashes
            if not known_cache_status.get(info_hash, {}).get(service, False)
        ]
        if not service_info_hashes:
            return service, set(), {}, None

        auth_error = None
        for entry in entries:
            try:
                debrid_instance = DebridService(service, entry["apiKey"], ip)
                cached_hashes, torrent_updates = (
                    await debrid_instance.get_and_cache_availability(
                        session,
                        service_info_hashes,
                        seeders_map,
                        tracker_map,
                        sources_map,
                        torrents,
                        media_id,
                        media_only_id,
                        season,
                        episode,
                        media_scope,
                        target_air_date=target_air_date,
                    )
                )
                return service, cached_hashes, torrent_updates, None
            except DebridAuthError as error:
                if auth_error is None:
                    auth_error = error
            except Exception as error:
                return service, None, None, error

        return service, None, None, auth_error

    results = await asyncio.gather(
        *(check_service(service, entries) for service, entries in service_groups),
        return_exceptions=True,
    )

    enriched_hashes = set()
    for result in results:
        if isinstance(result, Exception):
            logger.log("DEBRID", f"❌ Error checking availability: {result}")
            continue

        service, cache_map, torrent_updates, error = result
        if error:
            if isinstance(error, DebridAuthError):
                errors[service] = error
            else:
                logger.log(
                    "DEBRID",
                    f"❌ Error checking availability on {service}: {error}",
                )
            continue

        for info_hash, update in torrent_updates.items():
            if info_hash in enriched_hashes:
                continue
            torrent = torrents.get(info_hash)
            if torrent is not None:
                torrent.update(update)
                enriched_hashes.add(info_hash)

        if cache_map:
            for info_hash in cache_map:
                service_cache_status[info_hash][service] = True

    return service_cache_status, errors


async def search_media(
    media_type: str,
    media_id: str,
    config: Mapping[str, Any],
    ip: str,
    add_background_task: BackgroundTaskAdder,
) -> MediaSearchResult:
    if media_type not in {"movie", "series"} or "tmdb:" in media_id:
        return MediaSearchResult(MediaSearchStatus.INVALID)

    debrid_entries = config["_debridEntries"]
    enable_torrent = config["_enableTorrent"]
    scrape_debrid_account_torrents = config["scrapeDebridAccountTorrents"]
    use_account_scrape = bool(debrid_entries and scrape_debrid_account_torrents)
    is_torrent_only = enable_torrent and not debrid_entries

    if settings.DISABLE_TORRENT_STREAMS and is_torrent_only:
        return MediaSearchResult(
            MediaSearchStatus.DISABLED,
            is_torrent_only=is_torrent_only,
            use_account_scrape=use_account_scrape,
        )

    session = await http_client_manager.get_session()
    metadata_scraper = MetadataScraper(session)

    try:
        media_only_id, season, episode = parse_media_id(media_type, media_id)
    except ValueError:
        return MediaSearchResult(MediaSearchStatus.INVALID)
    media_scope = resolve_media_scope(media_type, season, episode)

    if settings.DIGITAL_RELEASE_FILTER:
        is_released = await release_filter.check_is_released(
            session, media_type, media_id, season, episode
        )
        if not is_released:
            logger.log("FILTER", f"🚫 {media_id} is not released yet. Skipping.")
            return MediaSearchResult(
                MediaSearchStatus.UNRELEASED,
                media_scope=media_scope,
                media_only_id=media_only_id,
                search_season=season,
                search_episode=episode,
                is_torrent_only=is_torrent_only,
                use_account_scrape=use_account_scrape,
            )

    metadata, aliases = await metadata_scraper.fetch_metadata_and_aliases(
        media_type, media_id, media_only_id, season, episode
    )
    if metadata is None:
        logger.log("SCRAPER", f"❌ Failed to fetch metadata for {media_id}")
        return MediaSearchResult(
            MediaSearchStatus.METADATA_UNAVAILABLE,
            media_scope=media_scope,
            media_only_id=media_only_id,
            search_season=season,
            search_episode=episode,
            is_torrent_only=is_torrent_only,
            use_account_scrape=use_account_scrape,
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

    is_kitsu = media_id.startswith("kitsu:")
    search_episode = episode
    search_season = season

    if is_kitsu:
        kitsu_mapping = anime_mapper.get_kitsu_episode_mapping(media_only_id)
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
                        "📺 Multi-part anime detected "
                        f"(kitsu:{media_only_id}): searching for "
                        f"S{search_season:02d}E{search_episode:02d} instead of "
                        f"S{season:02d}E{episode:02d}",
                    )
                elif search_season is not None and season is not None:
                    logger.log(
                        "SCRAPER",
                        "📺 Multi-part anime detected "
                        f"(kitsu:{media_only_id}): searching for "
                        f"S{search_season:02d} instead of S{season:02d}",
                    )

    cache_media_ids = [media_only_id]
    if anime_mapper.is_loaded():
        if is_kitsu:
            imdb_id = await anime_mapper.get_imdb_from_kitsu(media_only_id)
            if imdb_id:
                cache_media_ids.append(imdb_id)
        elif anime_mapper.is_anime_content(media_id, media_only_id):
            kitsu_ids = anime_mapper.get_kitsu_ids_from_imdb(media_only_id)
            if kitsu_ids:
                cache_media_ids.extend(kitsu_ids)
            kitsu_id = await anime_mapper.get_kitsu_from_imdb(media_only_id)
            if kitsu_id and kitsu_id not in cache_media_ids:
                cache_media_ids.append(kitsu_id)

    is_imdb_episode_request, reject_unknown_episode_files = episode_matching_policy(
        media_type,
        media_only_id,
        search_season,
        search_episode,
        cached_only=bool(config["cachedOnly"]),
        has_debrid=bool(debrid_entries),
        enable_torrent=enable_torrent,
    )
    target_air_date = None
    if is_imdb_episode_request:
        target_air_date = await EpisodeIndexService(session).get_target_air_date(
            media_only_id,
            search_season,
            search_episode,
        )
        if target_air_date:
            logger.log(
                "SCRAPER",
                f"📅 Episode target air date: {target_air_date} for "
                f"{media_only_id} S{search_season:02d}E{search_episode:02d}",
            )
        else:
            logger.log(
                "SCRAPER",
                f"📅 Episode target air date unavailable for {media_only_id} "
                f"S{search_season:02d}E{search_episode:02d}",
            )

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
        target_air_date=target_air_date,
        reject_unknown_episode_files=reject_unknown_episode_files,
        media_scope=media_scope,
    )

    await torrent_manager.get_cached_torrents()
    torrent_count = len(torrent_manager.torrents)
    cache_state = "hit" if torrent_count else "miss"
    metrics.observe_torrent_cache(media_type, cache_state, torrent_count)
    initial_info_hashes = set(torrent_manager.torrents)
    logger.log("SCRAPER", f"📦 Found cached torrents: {torrent_count}")

    cache_manager = CacheStateManager(media_id)
    cache_result = await cache_manager.check_and_decide(torrent_count)
    force_scrape_now = not torrent_manager.primary_cached
    lock_acquired = cache_result.lock_acquired
    sort_mixed = is_torrent_only or config["sortCachedUncachedTogether"]
    account_snapshot_ready = False

    if force_scrape_now and not lock_acquired:
        lock_acquired = await cache_manager.try_acquire_lock()

    if (force_scrape_now and not lock_acquired) or (
        cache_result.should_return_wait_message and not force_scrape_now
    ):
        logger.log(
            "SCRAPER",
            f"🔄 Another instance is scraping {log_title}, returning early",
        )
        return MediaSearchResult(
            MediaSearchStatus.BUSY,
            metadata=metadata,
            aliases=aliases,
            media_scope=media_scope,
            cache_state=cache_state,
            media_only_id=media_only_id,
            search_season=search_season,
            search_episode=search_episode,
            is_torrent_only=is_torrent_only,
            sort_mixed=sort_mixed,
            use_account_scrape=use_account_scrape,
        )

    if cache_result.should_scrape_background and not force_scrape_now:
        logger.log(
            "SCRAPER",
            f"🔄 Starting background scrape for {log_title} "
            f"(state={cache_result.state.value})",
        )
        add_background_task(
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
                    torrent_manager.scrape_torrents(ScrapeContext.LIVE),
                    ensure_account_snapshot_ready(session, debrid_entries, ip),
                    return_exceptions=True,
                )
                if isinstance(scrape_result, Exception):
                    raise scrape_result
                if isinstance(warmup_result, Exception):
                    raise warmup_result
                account_snapshot_ready = True
            else:
                await torrent_manager.scrape_torrents(ScrapeContext.LIVE)
            await _mark_scope_scraped_if_populated(media_id, torrent_manager.torrents)
            logger.log(
                "SCRAPER",
                "📥 Torrents after global RTN filtering: "
                f"{len(torrent_manager.torrents)}",
            )
        finally:
            await cache_manager.release_lock()

    service_cache_status = defaultdict(dict)
    verified_service_cache_status = defaultdict(dict)
    if use_account_scrape:
        if not account_snapshot_ready:
            await ensure_account_snapshot_ready(session, debrid_entries, ip)
        await schedule_account_snapshot_refresh(
            add_background_task, session, debrid_entries, ip
        )
        account_torrents, account_cache_status = await get_account_torrents_for_media(
            debrid_entries,
            media_type,
            media_scope,
            title,
            year,
            year_end,
            search_season,
            search_episode,
            aliases,
            remove_adult_content,
            target_air_date=target_air_date,
            reject_unknown_episode_files=reject_unknown_episode_files,
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
                f"📚 Account scrape added {len(account_torrents)} torrents "
                "from debrid snapshots",
            )
            public_cache_ingested = await ingest_account_torrents_to_public_cache(
                account_torrents, media_only_id, search_season
            )
            if public_cache_ingested:
                logger.log(
                    "SCRAPER",
                    f"🌐 Debrid account contributed {public_cache_ingested} rows "
                    "to public torrent cache",
                )

        merge_service_cache_status(service_cache_status, account_cache_status)

    if debrid_entries:
        existing_service_cache_status = await check_multi_service_availability(
            debrid_entries,
            torrent_manager.torrents,
            search_season,
            search_episode,
            media_scope,
        )
        merge_service_cache_status(
            service_cache_status, existing_service_cache_status
        )
        merge_service_cache_status(
            verified_service_cache_status, existing_service_cache_status
        )
    elif enable_torrent:
        await DebridService.apply_cached_availability_any_service(
            list(torrent_manager.torrents),
            search_season,
            search_episode,
            media_scope,
            torrent_manager.torrents,
        )

    current_info_hashes = set(torrent_manager.torrents)
    debrid_refresh_hashes = select_debrid_refresh_hashes(
        current_info_hashes,
        initial_info_hashes,
        verified_service_cache_status,
        had_cached_torrents=cache_result.has_cached_torrents,
        use_account_scrape=use_account_scrape,
    )

    debrid_errors = {}
    if debrid_entries and debrid_refresh_hashes:
        services_str = "+".join(entry["service"] for entry in debrid_entries)
        logger.log(
            "SCRAPER",
            f"🔄 Checking availability on debrid services: {services_str} "
            f"({len(debrid_refresh_hashes)}/{len(current_info_hashes)} torrents)",
        )
        torrents_to_check = {
            info_hash: torrent
            for info_hash, torrent in torrent_manager.torrents.items()
            if info_hash in debrid_refresh_hashes
        }
        fresh_service_cache_status, debrid_errors = (
            await get_and_cache_multi_service_availability(
                session,
                debrid_entries,
                torrents_to_check,
                media_id,
                media_only_id,
                search_season,
                search_episode,
                media_scope,
                ip,
                target_air_date=target_air_date,
                known_cache_status=service_cache_status,
            )
        )
        merge_service_cache_status(
            service_cache_status, fresh_service_cache_status
        )

    for service in dict.fromkeys(entry["service"] for entry in debrid_entries):
        cached_count = sum(
            1
            for cache_map in service_cache_status.values()
            if cache_map.get(service, False)
        )
        logger.log(
            "SCRAPER",
            f"💾 Available cached torrents on {service}: "
            f"{cached_count}/{len(torrent_manager.torrents)}",
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
        "⚖️  Torrents after user RTN filtering: "
        f"{len(torrent_manager.ranked_torrents)}/{initial_torrent_count}",
    )

    return MediaSearchResult(
        MediaSearchStatus.OK,
        metadata=metadata,
        aliases=aliases,
        media_scope=media_scope,
        torrents=torrent_manager.torrents,
        ranked_info_hashes=list(torrent_manager.ranked_torrents),
        service_cache_status=service_cache_status,
        debrid_errors=debrid_errors,
        cache_state=cache_state,
        media_only_id=media_only_id,
        search_season=search_season,
        search_episode=search_episode,
        is_torrent_only=is_torrent_only,
        sort_mixed=sort_mixed,
        show_account_sync_trigger=use_account_scrape,
        use_account_scrape=use_account_scrape,
    )
