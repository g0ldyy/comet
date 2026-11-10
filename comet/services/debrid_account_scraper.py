import asyncio
import time
from datetime import datetime

from comet.core.database import (
    _debrid_account_snapshot_ttl,
    build_changed_rows_upsert,
    build_json_list_membership_predicate,
    database,
    encode_json_param,
    fetch_flag,
)
from comet.core.execution import get_executor
from comet.core.models import settings
from comet.debrid.manager import build_account_key_hash
from comet.debrid.stremthru import StremThru
from comet.observability.context import create_detached_task
from comet.services.filtering import filter_release_records
from comet.services.lock import DistributedLock
from comet.utils.http_client import http_client_manager
from comet.utils.parsing import MediaScope

_SYNC_LOCK_PREFIX = "debrid-account-sync"
_background_tasks: dict[asyncio.Task, DistributedLock] = {}
_UPSERT_ACCOUNT_MAGNETS_QUERY = build_changed_rows_upsert(
    "debrid_account_magnets",
    columns={
        "debrid_service": "TEXT",
        "account_key_hash": "TEXT",
        "magnet_id": "TEXT",
        "info_hash": "TEXT",
        "name": "TEXT",
        "size": "BIGINT",
        "status": "TEXT",
        "added_at": "DOUBLE PRECISION",
    },
    conflict_columns=("debrid_service", "account_key_hash", "magnet_id"),
    compared_columns=("info_hash", "name", "size", "status", "added_at"),
)
_FRESH_ACCOUNT_SNAPSHOT_SQL = """
    debrid_account_sync_state.debrid_service = :debrid_service
    AND debrid_account_sync_state.account_key_hash = :account_key_hash
    AND debrid_account_sync_state.last_sync_at >= :min_timestamp
"""
_DELETE_REMOVED_ACCOUNT_MAGNETS_QUERY = f"""
    DELETE FROM debrid_account_magnets
    WHERE debrid_service = :debrid_service
      AND account_key_hash = :account_key_hash
      AND NOT {build_json_list_membership_predicate("magnet_id", "magnet_ids")}
"""
_UPSERT_ACCOUNT_SYNC_STATE_QUERY = """
    INSERT INTO debrid_account_sync_state (
        debrid_service,
        account_key_hash,
        last_sync_at
    ) VALUES (
        :debrid_service,
        :account_key_hash,
        :last_sync_at
    )
    ON CONFLICT (debrid_service, account_key_hash)
    DO UPDATE SET last_sync_at = EXCLUDED.last_sync_at
"""


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


def _to_epoch(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def _should_force_requested_episode_scope(
    parsed,
    season: int | None,
    episode: int | None,
    reject_unknown_episode_files: bool,
) -> bool:
    return (
        reject_unknown_episode_files
        and season is not None
        and episode is not None
        and not (parsed.seasons and parsed.episodes)
    )


async def _fetch_all_magnets(client: StremThru):
    limit = 500
    items_by_id = {}
    offset = 0

    while True:
        items, total_items = await client.list_magnets(limit=limit, offset=offset)

        if not items:
            break

        previous_count = len(items_by_id)
        for item in items:
            items_by_id[item["id"]] = item

        offset += len(items)
        if (
            offset >= total_items
            or len(items) < limit
            or len(items_by_id) == previous_count
        ):
            break

    return list(items_by_id.values())


async def _replace_account_snapshot(
    service: str,
    account_key_hash: str,
    synced_at: float,
    rows: list[dict],
) -> None:
    account = {"debrid_service": service, "account_key_hash": account_key_hash}
    async with database.transaction():
        await database.execute(
            _DELETE_REMOVED_ACCOUNT_MAGNETS_QUERY,
            {
                **account,
                "magnet_ids": encode_json_param([row["magnet_id"] for row in rows]),
            },
        )
        await database.execute_many(
            _UPSERT_ACCOUNT_MAGNETS_QUERY, [{**account, **row} for row in rows]
        )
        await database.execute(
            _UPSERT_ACCOUNT_SYNC_STATE_QUERY,
            {**account, "last_sync_at": synced_at},
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

    magnets = await _fetch_all_magnets(client)

    rows = []
    for item in magnets:
        rows.append(
            {
                "magnet_id": item["id"],
                "info_hash": item["hash"],
                "name": item["name"],
                "size": item["size"],
                "status": item["status"],
                "added_at": _to_epoch(item.get("added_at")),
            }
        )

    await _replace_account_snapshot(service, account_key_hash, synced_at, rows)


async def _sync_task(
    lock: DistributedLock,
    service: str,
    api_key: str,
    ip: str,
    account_key_hash: str,
):
    try:
        async with http_client_manager.bind() as session:
            await lock.run(
                _sync_single_account(session, service, api_key, ip, account_key_hash)
            )
    finally:
        await lock.release()


def _handle_sync_task_done(task: asyncio.Task):
    _background_tasks.pop(task, None)


def _schedule_sync_task(
    lock: DistributedLock,
    service: str,
    api_key: str,
    ip: str,
    account_key_hash: str,
) -> asyncio.Task:
    task = create_detached_task(
        _sync_task(lock, service, api_key, ip, account_key_hash),
        name="debrid-account-sync",
    )
    _background_tasks[task] = lock
    task.add_done_callback(_handle_sync_task_done)
    return task


async def shutdown_account_sync_tasks() -> None:
    pending = tuple(_background_tasks.items())
    if not pending:
        return

    for task, _ in pending:
        task.cancel()
    await asyncio.gather(*(task for task, _ in pending), return_exceptions=True)
    await asyncio.gather(
        *(lock.release() for _, lock in pending),
        return_exceptions=True,
    )


async def _has_fresh_snapshot(
    service: str, account_key_hash: str, min_timestamp: float
):
    return await fetch_flag(
        f"""
        SELECT 1
        FROM debrid_account_sync_state
        WHERE {_FRESH_ACCOUNT_SNAPSHOT_SQL}
        """,
        {
            "debrid_service": service,
            "account_key_hash": account_key_hash,
            "min_timestamp": min_timestamp,
        },
        force_primary=True,
    )


async def _get_fresh_snapshot_states(
    targets: list[tuple[str, str]],
    min_timestamp: float,
) -> list[bool]:
    return await asyncio.gather(
        *(
            _has_fresh_snapshot(service, account_key_hash, min_timestamp)
            for service, account_key_hash in targets
        )
    )


async def _wait_for_snapshot_targets(
    targets: list[tuple[str, str]],
    min_timestamp: float,
    deadline: float,
):
    if not targets:
        return

    pending = targets
    while pending and time.monotonic() < deadline:
        states = await _get_fresh_snapshot_states(pending, min_timestamp)
        unresolved = [
            target for target, has_snapshot in zip(pending, states) if not has_snapshot
        ]
        if not unresolved:
            return
        pending = unresolved
        await asyncio.sleep(0.15)


async def ensure_account_snapshot_ready(debrid_entries: list[dict], ip: str):
    accounts = _dedupe_accounts(debrid_entries)
    if not accounts:
        return

    min_timestamp = time.time() - _debrid_account_snapshot_ttl()
    states = await _get_fresh_snapshot_states(
        [(service, account_key_hash) for service, _, account_key_hash in accounts],
        min_timestamp,
    )
    missing = [
        account for account, has_snapshot in zip(accounts, states) if not has_snapshot
    ]

    if not missing:
        return

    deadline = time.monotonic() + settings.DEBRID_ACCOUNT_SCRAPE_INITIAL_WARM_TIMEOUT
    sync_tasks = []
    waiting_targets = []

    for service, api_key, account_key_hash in missing:
        lock = DistributedLock(_sync_lock_key(service, account_key_hash))
        if await lock.acquire():
            sync_tasks.append(
                _schedule_sync_task(
                    lock,
                    service,
                    api_key,
                    ip,
                    account_key_hash,
                )
            )
        else:
            waiting_targets.append((service, account_key_hash))

    if sync_tasks:
        remaining = deadline - time.monotonic()
        if remaining > 0:
            await asyncio.wait(sync_tasks, timeout=remaining)
    if waiting_targets:
        await _wait_for_snapshot_targets(waiting_targets, min_timestamp, deadline)


async def trigger_account_snapshot_sync(service: str, api_key: str, ip: str):
    if not api_key:
        return False

    account_key_hash = build_account_key_hash(api_key)
    lock = DistributedLock(_sync_lock_key(service, account_key_hash))
    if not await lock.acquire():
        return False

    _schedule_sync_task(
        lock,
        service,
        api_key,
        ip,
        account_key_hash,
    )
    return True


async def _refresh_account_snapshots(
    debrid_entries: list[dict],
    ip: str,
) -> None:
    now = time.time()
    accounts = _dedupe_accounts(debrid_entries)

    async def fetch_last_sync(service: str, account_key_hash: str):
        return await database.fetch_one(
            """
            SELECT last_sync_at
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

    last_sync_rows = await asyncio.gather(
        *(
            fetch_last_sync(service, account_key_hash)
            for service, _, account_key_hash in accounts
        )
    )

    for (service, api_key, account_key_hash), row in zip(accounts, last_sync_rows):
        if (
            row
            and row["last_sync_at"]
            and (
                now - row["last_sync_at"]
                < settings.DEBRID_ACCOUNT_SCRAPE_REFRESH_INTERVAL
            )
        ):
            continue

        lock = DistributedLock(_sync_lock_key(service, account_key_hash))
        lock_acquired = await lock.acquire()
        if not lock_acquired:
            continue

        _schedule_sync_task(
            lock,
            service,
            api_key,
            ip,
            account_key_hash,
        )


def schedule_account_snapshot_refresh(
    add_background_task,
    debrid_entries: list[dict],
    ip: str,
) -> None:
    add_background_task(
        _refresh_account_snapshots,
        debrid_entries,
        ip,
    )


async def get_account_torrents_for_media(
    debrid_entries: list[dict],
    media_type: str,
    media_scope: MediaScope,
    title: str,
    year: int | None,
    year_end: int | None,
    season: int | None,
    episode: int | None,
    aliases: dict | None,
    remove_adult_content: bool,
    target_air_date: str | None = None,
    reject_unknown_episode_files: bool = False,
):
    account_torrents = {}

    accounts = _dedupe_accounts(debrid_entries)
    if not accounts:
        return account_torrents

    min_timestamp = time.time() - _debrid_account_snapshot_ttl()
    aliases = aliases or {}

    async def fetch_rows(service: str, account_key_hash: str):
        rows = await database.fetch_all(
            f"""
            SELECT info_hash, name, size
            FROM debrid_account_magnets
            WHERE debrid_service = :debrid_service
              AND account_key_hash = :account_key_hash
              AND EXISTS (
                  SELECT 1
                  FROM debrid_account_sync_state
                  WHERE {_FRESH_ACCOUNT_SNAPSHOT_SQL}
              )
            ORDER BY added_at DESC
            """,
            {
                "debrid_service": service,
                "account_key_hash": account_key_hash,
                "min_timestamp": min_timestamp,
            },
            force_primary=True,
        )
        return service, rows

    results = await asyncio.gather(
        *[
            fetch_rows(service, account_key_hash)
            for service, _, account_key_hash in accounts
        ]
    )

    for service, rows in results:
        candidate_torrents = []
        for row in rows:
            info_hash = row["info_hash"]
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
            filter_release_records,
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
            if not media_scope.matches_parsed(
                parsed,
                season,
                episode,
                target_air_date=target_air_date,
                reject_unknown_episode_files=reject_unknown_episode_files,
            ):
                continue

            info_hash = torrent["infoHash"]
            if info_hash in account_torrents:
                continue

            force_requested_scope = _should_force_requested_episode_scope(
                parsed,
                season,
                episode,
                reject_unknown_episode_files,
            )
            account_torrents[info_hash] = {
                "fileIndex": torrent["fileIndex"],
                "title": torrent["title"],
                "seeders": torrent["seeders"],
                "size": torrent["size"],
                "tracker": torrent["tracker"],
                "sources": torrent["sources"],
                "parsed": parsed,
                "season": season if force_requested_scope else None,
                "episode": episode if force_requested_scope else None,
            }

    return account_torrents
