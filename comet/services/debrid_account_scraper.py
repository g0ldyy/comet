import asyncio
import time
from datetime import datetime

import orjson

from comet.core.database import IS_SQLITE, JSON_FUNC, database
from comet.core.execution import get_executor
from comet.core.logger import logger
from comet.core.models import settings
from comet.debrid.manager import build_account_key_hash
from comet.debrid.stremthru import StremThru
from comet.services.filtering import filter_worker
from comet.services.lock import DistributedLock
from comet.services.torrent_manager import torrent_update_queue

_SYNC_LOCK_PREFIX = "debrid-account-sync"
_CACHED_STATUSES = frozenset({"cached", "downloaded"})


def _dedupe_accounts(debrid_entries: list[dict]) -> list[tuple[str, str, str]]:
    seen = set()
    accounts = []
    for entry in debrid_entries:
        service = entry["service"]
        api_key = entry["apiKey"]
        if not api_key:
            continue
        key = (service, api_key)
        if key in seen:
            continue
        seen.add(key)
        accounts.append((service, api_key, build_account_key_hash(api_key)))
    return accounts


def _sync_lock_key(service: str, account_key_hash: str) -> str:
    return f"{_SYNC_LOCK_PREFIX}:{service}:{account_key_hash}"


def _snapshot_ttl() -> int:
    return max(
        settings.DEBRID_ACCOUNT_SCRAPE_CACHE_TTL,
        settings.DEBRID_ACCOUNT_SCRAPE_REFRESH_INTERVAL,
    )


def _to_epoch(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return time.time()
    return time.time()


async def _fetch_all_magnets(client: StremThru, max_items: int):
    limit = 500
    items_by_id = {}
    offset = 0

    while len(items_by_id) < max_items:
        page_limit = min(limit, max_items - len(items_by_id))
        items, total_items = await client.list_magnets(limit=page_limit, offset=offset)
        if items is None:
            return None

        if not items:
            break

        for item in items:
            magnet_id = str(item["id"])
            items_by_id[magnet_id] = item

        if len(items) < page_limit:
            break

        offset += page_limit
        if total_items and offset >= total_items:
            break

    return list(items_by_id.values())


async def _upsert_snapshot_rows(rows: list[dict]):
    if not rows:
        return

    if IS_SQLITE:
        query = """
            INSERT OR REPLACE INTO debrid_account_magnets (
                debrid_service,
                account_key_hash,
                magnet_id,
                info_hash,
                name,
                size,
                status,
                added_at,
                timestamp
            ) VALUES (
                :debrid_service,
                :account_key_hash,
                :magnet_id,
                :info_hash,
                :name,
                :size,
                :status,
                :added_at,
                :timestamp
            )
        """
    else:
        query = """
            INSERT INTO debrid_account_magnets (
                debrid_service,
                account_key_hash,
                magnet_id,
                info_hash,
                name,
                size,
                status,
                added_at,
                timestamp
            ) VALUES (
                :debrid_service,
                :account_key_hash,
                :magnet_id,
                :info_hash,
                :name,
                :size,
                :status,
                :added_at,
                :timestamp
            )
            ON CONFLICT (debrid_service, account_key_hash, magnet_id)
            DO UPDATE SET
                info_hash = EXCLUDED.info_hash,
                name = EXCLUDED.name,
                size = EXCLUDED.size,
                status = EXCLUDED.status,
                added_at = EXCLUDED.added_at,
                timestamp = EXCLUDED.timestamp
        """

    await database.execute_many(query, rows)


async def _set_last_sync(service: str, account_key_hash: str, last_sync: float):
    if IS_SQLITE:
        query = """
            INSERT OR REPLACE INTO debrid_account_sync_state
            VALUES (:debrid_service, :account_key_hash, :last_sync)
        """
    else:
        query = """
            INSERT INTO debrid_account_sync_state
            VALUES (:debrid_service, :account_key_hash, :last_sync)
            ON CONFLICT (debrid_service, account_key_hash)
            DO UPDATE SET last_sync = EXCLUDED.last_sync
        """

    await database.execute(
        query,
        {
            "debrid_service": service,
            "account_key_hash": account_key_hash,
            "last_sync": last_sync,
        },
    )


async def _sync_single_account(
    session,
    service: str,
    api_key: str,
    ip: str,
    account_key_hash: str,
):
    client = StremThru(session, "", "", f"{service}:{api_key}", ip)
    synced_at = time.time()

    magnets = await _fetch_all_magnets(
        client, settings.DEBRID_ACCOUNT_SCRAPE_MAX_SNAPSHOT_ITEMS
    )
    if magnets is None:
        return

    rows = []
    for item in magnets:
        info_hash = item["hash"].lower()
        if not info_hash:
            continue

        rows.append(
            {
                "debrid_service": service,
                "account_key_hash": account_key_hash,
                "magnet_id": str(item["id"]),
                "info_hash": info_hash,
                "name": item["name"],
                "size": item["size"],
                "status": item["status"],
                "added_at": _to_epoch(item.get("added_at")),
                "timestamp": synced_at,
            }
        )

    await _upsert_snapshot_rows(rows)

    await database.execute(
        """
        DELETE FROM debrid_account_magnets
        WHERE debrid_service = :debrid_service
          AND account_key_hash = :account_key_hash
          AND timestamp < :timestamp
        """,
        {
            "debrid_service": service,
            "account_key_hash": account_key_hash,
            "timestamp": synced_at,
        },
    )

    await _set_last_sync(service, account_key_hash, synced_at)

    logger.log(
        "SCRAPER",
        f"{service}: Synced {len(rows)} account torrents",
    )


async def _sync_task(
    lock: DistributedLock,
    session,
    service: str,
    api_key: str,
    ip: str,
    account_key_hash: str,
):
    try:
        await _sync_single_account(session, service, api_key, ip, account_key_hash)
    except Exception as e:
        logger.warning(f"Failed to sync debrid account torrents for {service}: {e}")
    finally:
        await lock.release()


async def _has_fresh_snapshot(
    service: str, account_key_hash: str, min_timestamp: float
):
    row = await database.fetch_one(
        """
        SELECT 1
        WHERE EXISTS (
            SELECT 1
            FROM debrid_account_sync_state
            WHERE debrid_service = :debrid_service
              AND account_key_hash = :account_key_hash
              AND last_sync >= :min_timestamp
        )
        OR EXISTS (
            SELECT 1
            FROM debrid_account_magnets
            WHERE debrid_service = :debrid_service
              AND account_key_hash = :account_key_hash
              AND timestamp >= :min_timestamp
        )
        """,
        {
            "debrid_service": service,
            "account_key_hash": account_key_hash,
            "min_timestamp": min_timestamp,
        },
        force_primary=True,
    )
    return bool(row)


async def _wait_for_snapshot_targets(
    targets: list[tuple[str, str]],
    min_timestamp: float,
    deadline: float,
):
    if not targets:
        return

    pending = targets
    while pending and time.monotonic() < deadline:
        unresolved = []
        for service, account_key_hash in pending:
            has_snapshot = await _has_fresh_snapshot(
                service, account_key_hash, min_timestamp
            )
            if not has_snapshot:
                unresolved.append((service, account_key_hash))
        if not unresolved:
            return
        pending = unresolved
        await asyncio.sleep(0.15)


async def ensure_account_snapshot_ready(session, debrid_entries: list[dict], ip: str):
    accounts = _dedupe_accounts(debrid_entries)
    if not accounts:
        return

    min_timestamp = time.time() - _snapshot_ttl()
    missing = []
    for service, api_key, account_key_hash in accounts:
        has_snapshot = await _has_fresh_snapshot(
            service, account_key_hash, min_timestamp
        )
        if not has_snapshot:
            missing.append((service, api_key, account_key_hash))

    if not missing:
        return

    deadline = time.monotonic() + settings.DEBRID_ACCOUNT_SCRAPE_INITIAL_WARM_TIMEOUT
    sync_tasks = []
    waiting_targets = []

    for service, api_key, account_key_hash in missing:
        lock = DistributedLock(_sync_lock_key(service, account_key_hash), timeout=300)
        if await lock.acquire():
            sync_tasks.append(
                _sync_task(lock, session, service, api_key, ip, account_key_hash)
            )
        else:
            waiting_targets.append((service, account_key_hash))

    if sync_tasks:
        remaining = deadline - time.monotonic()
        if remaining > 0:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*sync_tasks, return_exceptions=True),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                logger.log(
                    "SCRAPER",
                    "Debrid account warm sync timed out, continuing with partial data",
                )

    if waiting_targets:
        await _wait_for_snapshot_targets(waiting_targets, min_timestamp, deadline)


async def trigger_account_snapshot_sync(session, service: str, api_key: str, ip: str):
    if not api_key:
        return False

    account_key_hash = build_account_key_hash(api_key)
    lock = DistributedLock(_sync_lock_key(service, account_key_hash), timeout=300)
    if not await lock.acquire():
        return False

    asyncio.create_task(
        _sync_task(lock, session, service, api_key, ip, account_key_hash)
    )
    return True


async def _fetch_existing_media_torrent_keys(
    media_id: str, info_hashes: list[str]
) -> set[tuple[str, int | None, int | None]]:
    if not info_hashes:
        return set()

    rows = await database.fetch_all(
        f"""
        SELECT info_hash, season, episode
        FROM torrents
        WHERE media_id = :media_id
          AND info_hash IN (
              SELECT CAST(value as TEXT)
              FROM {JSON_FUNC}(:info_hashes)
          )
        """,
        {
            "media_id": media_id,
            "info_hashes": orjson.dumps(info_hashes).decode("utf-8"),
        },
        force_primary=True,
    )
    return {(row["info_hash"], row["season"], row["episode"]) for row in rows}


async def ingest_account_torrents_to_public_cache(
    account_torrents: dict,
    media_id: str,
    search_season: int | None,
):
    if not account_torrents:
        return 0

    existing_torrent_keys = await _fetch_existing_media_torrent_keys(
        media_id, list(account_torrents.keys())
    )

    enqueued_rows = 0
    for info_hash, torrent in account_torrents.items():
        parsed = torrent["parsed"]
        parsed_seasons = parsed.seasons if parsed.seasons else [search_season]
        parsed_episodes = parsed.episodes if parsed.episodes else [None]
        episode = None if len(parsed_episodes) > 1 else parsed_episodes[0]

        for season in parsed_seasons:
            if (info_hash, season, episode) in existing_torrent_keys:
                continue

            file_info = {
                "info_hash": info_hash,
                "index": torrent["fileIndex"],
                "title": torrent["title"],
                "size": torrent["size"],
                "season": season,
                "episode": episode,
                "parsed": parsed,
                "seeders": torrent["seeders"],
                "tracker": torrent["tracker"],
                "sources": torrent["sources"],
            }
            await torrent_update_queue.add_torrent_info(file_info, media_id)
            enqueued_rows += 1

    return enqueued_rows


async def schedule_account_snapshot_refresh(
    background_tasks,
    session,
    debrid_entries: list[dict],
    ip: str,
):
    now = time.time()

    for service, api_key, account_key_hash in _dedupe_accounts(debrid_entries):
        row = await database.fetch_one(
            """
            SELECT last_sync
            FROM debrid_account_sync_state
            WHERE debrid_service = :debrid_service
              AND account_key_hash = :account_key_hash
            """,
            {
                "debrid_service": service,
                "account_key_hash": account_key_hash,
            },
            force_primary=True,
        )

        if (
            row
            and row["last_sync"]
            and (
                now - row["last_sync"] < settings.DEBRID_ACCOUNT_SCRAPE_REFRESH_INTERVAL
            )
        ):
            continue

        lock = DistributedLock(_sync_lock_key(service, account_key_hash), timeout=300)
        lock_acquired = await lock.acquire()
        if not lock_acquired:
            continue

        background_tasks.add_task(
            _sync_task,
            lock,
            session,
            service,
            api_key,
            ip,
            account_key_hash,
        )


async def get_account_torrents_for_media(
    debrid_entries: list[dict],
    media_type: str,
    title: str,
    year: int | None,
    year_end: int | None,
    season: int | None,
    episode: int | None,
    aliases: dict | None,
    remove_adult_content: bool,
):
    account_torrents = {}
    service_cache_status = {}

    accounts = _dedupe_accounts(debrid_entries)
    if not accounts:
        return account_torrents, service_cache_status

    min_timestamp = time.time() - _snapshot_ttl()
    aliases = aliases or {}

    async def fetch_rows(service: str, account_key_hash: str):
        rows = await database.fetch_all(
            """
            SELECT info_hash, name, size, status
            FROM debrid_account_magnets
            WHERE debrid_service = :debrid_service
              AND account_key_hash = :account_key_hash
              AND timestamp >= :min_timestamp
            ORDER BY added_at DESC
            LIMIT :limit
            """,
            {
                "debrid_service": service,
                "account_key_hash": account_key_hash,
                "min_timestamp": min_timestamp,
                "limit": settings.DEBRID_ACCOUNT_SCRAPE_MAX_MATCH_ITEMS,
            },
            force_primary=True,
        )
        return service, rows

    results = await asyncio.gather(
        *[
            fetch_rows(service, account_key_hash)
            for service, _, account_key_hash in accounts
        ],
        return_exceptions=True,
    )

    for result in results:
        if isinstance(result, Exception):
            logger.warning(f"Failed to read debrid account snapshot: {result}")
            continue

        service, rows = result
        candidate_torrents = []
        service_cached_status = {}
        for row in rows:
            info_hash = row["info_hash"]
            if not info_hash:
                continue

            info_hash = info_hash.lower()
            is_cached = row["status"] in _CACHED_STATUSES
            if is_cached:
                service_cached_status[info_hash] = True
            elif info_hash not in service_cached_status:
                service_cached_status[info_hash] = False

            candidate_torrents.append(
                {
                    "infoHash": info_hash,
                    "fileIndex": None,
                    "title": row["name"],
                    "seeders": 0,
                    "size": row["size"],
                    "tracker": f"DebridAccount|{service}",
                    "sources": [],
                }
            )

        if not candidate_torrents:
            continue

        loop = asyncio.get_running_loop()
        filtered_torrents = await loop.run_in_executor(
            get_executor(),
            filter_worker,
            candidate_torrents,
            title,
            year,
            year_end,
            media_type,
            aliases,
            remove_adult_content,
        )

        for torrent in filtered_torrents:
            parsed = torrent["parsed"]
            parsed_season = parsed.seasons[0] if parsed.seasons else None
            parsed_episode = parsed.episodes[0] if parsed.episodes else None

            if (parsed_season is not None and parsed_season != season) or (
                parsed_episode is not None and parsed_episode != episode
            ):
                continue

            info_hash = torrent["infoHash"]
            cached_state = service_cached_status.get(info_hash, False)
            status_map = service_cache_status.setdefault(info_hash, {})
            if cached_state:
                status_map[service] = True
            elif service not in status_map:
                status_map[service] = False

            if info_hash in account_torrents:
                continue

            account_torrents[info_hash] = {
                "fileIndex": torrent["fileIndex"],
                "title": torrent["title"],
                "seeders": torrent["seeders"],
                "size": torrent["size"],
                "tracker": torrent["tracker"],
                "sources": torrent["sources"],
                "parsed": parsed,
            }

    return account_torrents, service_cache_status
