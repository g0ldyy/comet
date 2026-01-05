import asyncio
import base64
import hashlib
import re
import time
from collections import defaultdict
from urllib.parse import unquote

import anyio
import bencodepy
import orjson
from demagnetize.core import Demagnetizer
from RTN import ParsedData, parse
from torf import Magnet

from comet.core.constants import TORRENT_TIMEOUT
from comet.core.logger import logger
from comet.core.models import database, settings
from comet.utils.parsing import default_dump, is_video

TRACKER_PATTERN = re.compile(r"[&?]tr=([^&]+)")
INFO_HASH_PATTERN = re.compile(r"btih:([a-fA-F0-9]{40}|[a-zA-Z0-9]{32})")


def extract_trackers_from_magnet(magnet_uri: str):
    try:
        trackers = TRACKER_PATTERN.findall(magnet_uri)
        return [unquote(tracker) for tracker in trackers]
    except Exception as e:
        logger.warning(f"Failed to extract trackers from magnet URI: {e}")
        return []


async def download_torrent(session, url: str):
    try:
        async with session.get(
            url, allow_redirects=False, timeout=TORRENT_TIMEOUT
        ) as response:
            if response.status == 200:
                return (await response.read(), None, None)

            location = response.headers.get("Location", "")
            if location:
                match = INFO_HASH_PATTERN.search(location)
                if match:
                    info_hash = match.group(1)
                    if len(info_hash) == 32:
                        info_hash = base64.b16encode(
                            base64.b32decode(info_hash)
                        ).decode("utf-8")
                    return (None, info_hash, location)
            return (None, None, None)
    except Exception as e:
        logger.warning(
            f"Failed to download torrent from {url}: {e} (in most cases, you can ignore this error)"
        )
        return (None, None, None)


demagnetizer = Demagnetizer()


async def get_torrent_from_magnet(magnet_uri: str):
    try:
        magnet = Magnet.from_string(magnet_uri)
        with anyio.fail_after(settings.MAGNET_RESOLVE_TIMEOUT):
            torrent_data = await demagnetizer.demagnetize(magnet)
            if torrent_data:
                return torrent_data.dump()
    except Exception as e:
        logger.warning(f"Failed to get torrent from magnet: {e}")
        return None


def extract_torrent_metadata(content: bytes):
    try:
        torrent_data = bencodepy.decode(content)
        info = torrent_data[b"info"]
        info_encoded = bencodepy.encode(info)
        m = hashlib.sha1()
        m.update(info_encoded)
        info_hash = m.hexdigest()

        announce_list = [
            tracker[0].decode() for tracker in torrent_data.get(b"announce-list", [])
        ]
        announce = torrent_data.get(b"announce", b"").decode()
        if announce:
            announce_list.append(announce)

        metadata = {"info_hash": info_hash, "announce_list": announce_list, "files": []}

        files = info[b"files"] if b"files" in info else [info]
        for idx, file in enumerate(files):
            name = (
                file[b"path"][-1].decode()
                if b"path" in file
                else file[b"name"].decode()
            )

            if not is_video(name) or "sample" in name.lower():
                continue

            size = file[b"length"]

            metadata["files"].append({"index": idx, "name": name, "size": size})

        return metadata

    except Exception as e:
        logger.warning(f"Failed to extract torrent metadata: {e}")
        return {}


async def add_torrent(
    info_hash: str,
    seeders: int,
    tracker: str,
    media_id: str,
    search_season: int,
    sources: list,
    file_index: int,
    title: str,
    size: int,
    parsed: ParsedData,
):
    try:
        parsed_season = parsed.seasons[0] if parsed.seasons else search_season
        parsed_episode = parsed.episodes[0] if parsed.episodes else None

        if parsed_episode is not None:
            await database.execute(
                """
                DELETE FROM torrents
                WHERE info_hash = :info_hash
                AND season = :season 
                AND episode IS NULL
                """,
                {
                    "info_hash": info_hash,
                    "season": parsed_season,
                },
            )
            logger.log(
                "SCRAPER",
                f"Deleted season-only entry for S{parsed_season:02d} of {info_hash}",
            )

        await _upsert_torrent_record(
            {
                "media_id": media_id,
                "info_hash": info_hash,
                "file_index": file_index,
                "season": parsed_season,
                "episode": parsed_episode,
                "title": title,
                "seeders": seeders,
                "size": size,
                "tracker": tracker,
                "sources": orjson.dumps(sources).decode("utf-8"),
                "parsed": orjson.dumps(parsed, default_dump).decode("utf-8"),
                "timestamp": time.time(),
            }
        )

        additional = ""
        if parsed_season:
            additional += f" - S{parsed_season:02d}"
            additional += f"E{parsed_episode:02d}" if parsed_episode else ""

        logger.log("SCRAPER", f"Added torrent for {media_id} - {title}{additional}")
    except Exception as e:
        logger.warning(f"Failed to add torrent for {info_hash}: {e}")


class AddTorrentQueue:
    def __init__(self, max_concurrent: int = 10):
        self.queue = asyncio.Queue()
        self.max_concurrent = max_concurrent
        self.is_running = False
        self.semaphore = asyncio.Semaphore(max_concurrent)

    async def add_torrent(
        self,
        magnet_url: str,
        seeders: int,
        tracker: str,
        media_id: str,
        search_season: int,
    ):
        if not settings.DOWNLOAD_TORRENT_FILES:
            return

        await self.queue.put((magnet_url, seeders, tracker, media_id, search_season))
        if not self.is_running:
            self.is_running = True
            asyncio.create_task(self._process_queue())

    async def _process_queue(self):
        while self.is_running:
            try:
                (
                    magnet_url,
                    seeders,
                    tracker,
                    media_id,
                    search_season,
                ) = await self.queue.get()

                async with self.semaphore:
                    try:
                        content = await get_torrent_from_magnet(magnet_url)
                        if content:
                            metadata = extract_torrent_metadata(content)
                            for file in metadata["files"]:
                                parsed = parse(file["name"])

                                await add_torrent(
                                    metadata["info_hash"],
                                    seeders,
                                    tracker,
                                    media_id,
                                    search_season,
                                    metadata["announce_list"],
                                    file["index"],
                                    file["name"],
                                    file["size"],
                                    parsed,
                                )
                    finally:
                        self.queue.task_done()

            except Exception:
                await asyncio.sleep(1)

        self.is_running = False

    async def stop(self):
        await self.queue.join()
        self.is_running = False


add_torrent_queue = AddTorrentQueue()


UPDATE_INTERVAL = (
    settings.TORRENT_CACHE_TTL // 2 if settings.TORRENT_CACHE_TTL >= 0 else 31536000
)


class TorrentUpdateQueue:
    def __init__(self, batch_size: int = 1000, flush_interval: float = 5.0):
        self.queue = asyncio.Queue()
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.is_running = False
        self.batches = {"to_delete": set(), "upserts": {}}

    async def add_torrent_info(self, file_info: dict, media_id: str = None):
        await self.queue.put((file_info, media_id))
        if not self.is_running:
            self.is_running = True
            asyncio.create_task(self._process_queue())

    async def _process_queue(self):
        last_flush_time = time.time()

        while self.is_running:
            try:
                while not self.queue.empty():
                    try:
                        file_info, media_id = self.queue.get_nowait()
                        await self._process_file_info(file_info, media_id)
                    except asyncio.QueueEmpty:
                        break

                current_time = time.time()

                if (
                    current_time - last_flush_time >= self.flush_interval
                    or self.queue.empty()
                ) and any(len(batch) > 0 for batch in self.batches.values()):
                    await self._flush_batch()
                    last_flush_time = current_time

                if self.queue.empty() and not any(
                    len(batch) > 0 for batch in self.batches.values()
                ):
                    self.is_running = False
                    break

                await asyncio.sleep(0.1)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Error in _process_queue: {e}")
                self._reset_batches()

        if any(len(batch) > 0 for batch in self.batches.values()):
            await self._flush_batch()

        self.is_running = False

    async def stop(self):
        self.is_running = False

        # Process remaining items in queue
        while not self.queue.empty():
            try:
                file_info, media_id = self.queue.get_nowait()
                await self._process_file_info(file_info, media_id)
            except Exception as e:
                logger.warning(
                    f"Error processing remaining queue items during shutdown: {e}"
                )
                break

        # Flush any remaining batches
        if any(len(batch) > 0 for batch in self.batches.values()):
            await self._flush_batch()

    def _reset_batches(self):
        for key, batch in self.batches.items():
            if len(batch) > 0:
                logger.warning(
                    f"Ignoring {len(batch)} items in problematic '{key}' batch"
                )
                batch.clear()

    async def _flush_batch(self):
        try:
            if self.batches["to_delete"]:
                delete_items = list(self.batches["to_delete"])
                sub_batch_size = 100
                for i in range(0, len(delete_items), sub_batch_size):
                    try:
                        sub_batch = delete_items[i : i + sub_batch_size]

                        placeholders = []
                        params = {}
                        for idx, item in enumerate(sub_batch):
                            info_hash, season = item
                            key_suffix = f"_{idx}"
                            placeholders.append(
                                f"(CAST(:info_hash{key_suffix} AS TEXT), CAST(:season{key_suffix} AS INTEGER))"
                            )
                            params[f"info_hash{key_suffix}"] = info_hash
                            params[f"season{key_suffix}"] = season

                        async with database.transaction():
                            delete_query = f"""
                                DELETE FROM torrents
                                WHERE (info_hash, season) IN (
                                    {",".join(placeholders)}
                                )
                                AND episode IS NULL
                            """
                            await database.execute(delete_query, params)
                    except Exception as e:
                        logger.warning(f"Error processing delete batch: {e}")

                self.batches["to_delete"].clear()

            if self.batches["upserts"]:
                grouped: dict[str, list[dict]] = defaultdict(list)
                for params in self.batches["upserts"].values():
                    key = _determine_conflict_key(params["season"], params["episode"])
                    grouped[key].append(params)

                for key, rows in grouped.items():
                    query = _get_torrent_upsert_query(key)
                    try:
                        await _execute_batched_upsert(query, rows)
                    except Exception as e:
                        logger.warning(f"Error processing upsert batch: {e}")

                total_upserts = len(self.batches["upserts"])
                if total_upserts > 0:
                    logger.log(
                        "SCRAPER",
                        f"Upserted {total_upserts} torrents in batch",
                    )
                self.batches["upserts"].clear()

        except Exception as e:
            logger.warning(f"Error in flush_batch: {e}")
            self._reset_batches()

    async def _process_file_info(self, file_info: dict, media_id: str = None):
        try:
            params = {
                "info_hash": file_info["info_hash"],
                "file_index": file_info["index"],
                "season": file_info["season"],
                "episode": file_info["episode"],
                "title": file_info["title"],
                "seeders": file_info["seeders"],
                "size": file_info["size"],
                "tracker": file_info["tracker"],
                "sources": orjson.dumps(file_info["sources"]).decode("utf-8"),
                "parsed": orjson.dumps(
                    file_info["parsed"], default=default_dump
                ).decode("utf-8"),
                "timestamp": time.time(),
                "media_id": media_id,
            }

            if settings.DATABASE_TYPE == "postgresql":
                params["update_interval"] = UPDATE_INTERVAL

            params["lock_key"] = _compute_advisory_lock_key(
                media_id,
                file_info["info_hash"],
                file_info["season"],
                file_info["episode"],
            )

            upsert_key = _build_upsert_key(
                file_info["info_hash"],
                file_info["season"],
                file_info["episode"],
                media_id,
            )

            # In-memory deduplication: keep the freshest timestamp
            existing = self.batches["upserts"].get(upsert_key)
            if not existing or params["timestamp"] > existing["timestamp"]:
                self.batches["upserts"][upsert_key] = params

            if file_info["episode"] is not None:
                self.batches["to_delete"].add(
                    (file_info["info_hash"], file_info["season"])
                )

            await self._check_batch_size()
        except Exception as e:
            logger.warning(f"Error processing file info: {e}")
        finally:
            self.queue.task_done()

    async def _check_batch_size(self):
        if any(len(batch) >= self.batch_size for batch in self.batches.values()):
            await self._flush_batch()


TORRENT_INSERT_TEMPLATE = """
INSERT INTO torrents (
    media_id,
    info_hash,
    file_index,
    season,
    episode,
    title,
    seeders,
    size,
    tracker,
    sources,
    parsed,
    timestamp
) VALUES (
    :media_id,
    :info_hash,
    :file_index,
    :season,
    :episode,
    :title,
    :seeders,
    :size,
    :tracker,
    :sources,
    :parsed,
    :timestamp
)
"""

SQLITE_UPSERT_QUERY = TORRENT_INSERT_TEMPLATE.replace("INSERT", "INSERT OR REPLACE", 1)
POSTGRES_UPDATE_SET = """
    DO UPDATE SET
        media_id = EXCLUDED.media_id,
        file_index = EXCLUDED.file_index,
        title = EXCLUDED.title,
        seeders = EXCLUDED.seeders,
        size = EXCLUDED.size,
        tracker = EXCLUDED.tracker,
        sources = EXCLUDED.sources,
        parsed = EXCLUDED.parsed,
        timestamp = EXCLUDED.timestamp
    WHERE
        (
            torrents.media_id IS DISTINCT FROM EXCLUDED.media_id OR
            torrents.file_index IS DISTINCT FROM EXCLUDED.file_index OR
            torrents.title IS DISTINCT FROM EXCLUDED.title OR
            torrents.seeders IS DISTINCT FROM EXCLUDED.seeders OR
            torrents.size IS DISTINCT FROM EXCLUDED.size OR
            torrents.tracker IS DISTINCT FROM EXCLUDED.tracker OR
            torrents.sources IS DISTINCT FROM EXCLUDED.sources OR
            torrents.parsed IS DISTINCT FROM EXCLUDED.parsed
        )
        OR
        (
            COALESCE(torrents.timestamp, 0) < (EXCLUDED.timestamp - :update_interval)
        )
"""

POSTGRES_CONFLICT_TARGETS = {
    "series": "(media_id, info_hash, season, episode) WHERE season IS NOT NULL AND episode IS NOT NULL",
    "season_only": "(media_id, info_hash, season) WHERE season IS NOT NULL AND episode IS NULL",
    "episode_only": "(media_id, info_hash, episode) WHERE season IS NULL AND episode IS NOT NULL",
    "none": "(media_id, info_hash) WHERE season IS NULL AND episode IS NULL",
}

_POSTGRES_UPSERT_CACHE: dict[str, str] = {}


def _determine_conflict_key(season, episode) -> str:
    if season is not None and episode is not None:
        return "series"
    if season is not None:
        return "season_only"
    if episode is not None:
        return "episode_only"
    return "none"


def _build_upsert_key(info_hash, season, episode, media_id):
    return (media_id, info_hash, season, episode)


def _compute_advisory_lock_key(media_id, info_hash, season, episode) -> int:
    payload = f"{media_id}|{info_hash}|{season}|{episode}".encode("utf-8")
    digest = hashlib.sha1(payload).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


async def _execute_sqlite_batched_upsert(rows: list[dict]):
    if not rows:
        return

    # Fetch relevant existing records to compare against
    media_ids = list({row["media_id"] for row in rows if row.get("media_id")})
    if not media_ids:
        async with database.transaction():
            await _execute_standard_sqlite_insert(rows)
        return

    placeholders = ",".join(f":mid{i}" for i in range(len(media_ids)))
    params = {f"mid{i}": mid for i, mid in enumerate(media_ids)}

    existing_rows = await database.fetch_all(
        f"SELECT * FROM torrents WHERE media_id IN ({placeholders})", params
    )

    # Build lookup map: (media_id, info_hash, season, episode) -> row
    existing_map = {}
    for row in existing_rows:
        key = (
            row["media_id"],
            row["info_hash"],
            row["season"],
            row["episode"],
        )
        existing_map[key] = row

    # Filter in Python
    to_insert = []
    # Columns to check for changes (everything except timestamp and lock_key/update_interval)
    check_cols = [
        "file_index",
        "title",
        "seeders",
        "size",
        "tracker",
        "sources",
        "parsed",
    ]

    for row in rows:
        key = (
            row["media_id"],
            row["info_hash"],
            row["season"],
            row["episode"],
        )
        existing = existing_map.get(key)

        if not existing:
            # New record
            to_insert.append(row)
            continue

        # Check for data changes
        changed = False
        for col in check_cols:
            # Simple equality check.
            if row.get(col) != existing[col]:
                changed = True
                break

        if changed:
            to_insert.append(row)
            continue

        # Check TTL
        existing_ts = existing["timestamp"]
        if existing_ts < (row["timestamp"] - UPDATE_INTERVAL):
            to_insert.append(row)

    if to_insert:
        await _execute_standard_sqlite_insert(to_insert)


async def _execute_standard_sqlite_insert(rows: list[dict]):
    keys_to_ignore = {"lock_key", "update_interval"}
    columns = [k for k in rows[0].keys() if k not in keys_to_ignore]
    sanitized_rows = [{k: row[k] for k in columns} for row in rows]

    query = f"""
        INSERT OR REPLACE INTO torrents ({", ".join(columns)})
        VALUES ({", ".join(f":{col}" for col in columns)})
    """

    # Retry logic for busy database
    for attempt in range(5):
        try:
            async with database.transaction():
                await database.execute_many(query, sanitized_rows)
            return
        except Exception:
            if attempt < 4:
                await asyncio.sleep(0.2 * (attempt + 1))
                continue
            raise


async def _execute_batched_upsert(query: str, rows):
    if not rows:
        return

    if settings.DATABASE_TYPE == "sqlite":
        await _execute_sqlite_batched_upsert(rows)
        return

    ordered_rows = sorted(rows, key=lambda row: row.get("lock_key") or 0)

    acquired_locks = []
    rows_to_insert = []

    try:
        # Non-blocking lock acquisition - skip rows we can't lock
        for row in ordered_rows:
            lock_key = row.get("lock_key")
            if lock_key is None:
                rows_to_insert.append(row)
                continue

            # Use session-level non-blocking lock (not transaction-level)
            acquired = await database.fetch_val(
                "SELECT pg_try_advisory_lock(CAST(:lock_key AS BIGINT))",
                {"lock_key": lock_key},
            )
            if acquired:
                acquired_locks.append(lock_key)
                rows_to_insert.append(row)
            # If not acquired, skip this row - another replica is handling it

        if rows_to_insert:
            sanitized_rows = [
                {key: value for key, value in row.items() if key != "lock_key"}
                for row in rows_to_insert
            ]

            await database.execute_many(query, sanitized_rows)

    finally:
        # Always release all acquired session-level locks
        for lock_key in acquired_locks:
            try:
                await database.execute(
                    "SELECT pg_advisory_unlock(CAST(:lock_key AS BIGINT))",
                    {"lock_key": lock_key},
                )
            except Exception:
                pass  # Best effort unlock


def _get_torrent_upsert_query(conflict_key: str) -> str:
    if settings.DATABASE_TYPE == "sqlite":
        return SQLITE_UPSERT_QUERY

    target = POSTGRES_CONFLICT_TARGETS[conflict_key]
    if conflict_key not in _POSTGRES_UPSERT_CACHE:
        _POSTGRES_UPSERT_CACHE[conflict_key] = (
            TORRENT_INSERT_TEMPLATE + f" ON CONFLICT {target} " + POSTGRES_UPDATE_SET
        )
    return _POSTGRES_UPSERT_CACHE[conflict_key]


async def _upsert_torrent_record(params: dict):
    if settings.DATABASE_TYPE == "sqlite":
        await _execute_sqlite_batched_upsert([params])
        return

    query = _get_torrent_upsert_query(
        _determine_conflict_key(params.get("season"), params.get("episode"))
    )

    params["update_interval"] = UPDATE_INTERVAL

    await database.execute(query, params)


torrent_update_queue = TorrentUpdateQueue()
