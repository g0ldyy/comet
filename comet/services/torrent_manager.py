import asyncio
import hashlib
import re
import time
from asyncio import QueueEmpty
from collections import defaultdict
from urllib.parse import unquote

import anyio
import bencodepy
import orjson
import xxhash
from demagnetize.core import Demagnetizer
from RTN import ParsedData, parse
from torf import Magnet

from comet.cometnet import get_active_backend
from comet.cometnet.protocol import TorrentMetadata
from comet.core.constants import TORRENT_TIMEOUT
from comet.core.logger import logger
from comet.core.models import database, settings
from comet.utils.formatting import normalize_info_hash
from comet.utils.parsing import default_dump, ensure_multi_language, is_video

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
                        info_hash = normalize_info_hash(info_hash)
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


async def broadcast_torrents(torrents_params: list[dict]):
    if not torrents_params:
        return

    backend = get_active_backend()
    if not backend:
        return

    try:
        clean_torrents = []
        for params in torrents_params:
            # Prepare clean data
            sources = params.get("sources", [])
            if isinstance(sources, str):
                try:
                    sources = orjson.loads(sources)
                except Exception:
                    sources = []

            parsed = params.get("parsed", {})
            if isinstance(parsed, str):
                try:
                    parsed = orjson.loads(parsed)
                except Exception:
                    parsed = {}

            imdb_id = params.get("media_id")
            if not (isinstance(imdb_id, str) and imdb_id.startswith("tt")):
                imdb_id = None

            clean_torrents.append(
                {
                    "info_hash": params["info_hash"],
                    "title": params["title"],
                    "size": params["size"],
                    "tracker": params["tracker"],
                    "imdb_id": imdb_id,
                    "file_index": params.get("file_index", 0),
                    "seeders": params.get("seeders", 0),
                    "season": params.get("season"),
                    "episode": params.get("episode"),
                    "sources": sources,
                    "parsed": parsed,
                }
            )

        await torrent_broadcast_queue.add(clean_torrents)

    except Exception as e:
        logger.debug(f"CometNet broadcast error: {e}")


class TorrentBroadcastQueue:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.is_running = False
        self._lock = asyncio.Lock()

    async def add(self, torrents: list[dict]):
        if not torrents:
            return

        await self.queue.put(torrents)

        if not self.is_running:
            async with self._lock:
                if not self.is_running:
                    self.is_running = True
                    asyncio.create_task(self._process_queue())

    async def _process_queue(self):
        backend = get_active_backend()
        if not backend:
            self.is_running = False
            return

        while self.is_running:
            try:
                torrents = await self.queue.get()

                if torrents:
                    tasks = [backend.broadcast_torrent(t) for t in torrents]
                    if tasks:
                        await asyncio.gather(*tasks)

                    logger.log(
                        "COMETNET",
                        f"Queued {len(torrents)} torrents for broadcast via {backend.__class__.__name__}",
                    )

                self.queue.task_done()

            except Exception as e:
                logger.warning(f"Error in broadcast queue: {e}")
                await asyncio.sleep(1)

            if self.queue.empty():
                await asyncio.sleep(1)
                if self.queue.empty():
                    async with self._lock:
                        if self.queue.empty():
                            self.is_running = False
                            return

    async def stop(self):
        self.is_running = False


torrent_broadcast_queue = TorrentBroadcastQueue()


async def check_torrent_exists(info_hash: str) -> bool:
    try:
        query = "SELECT 1 FROM torrents WHERE info_hash = :info_hash LIMIT 1"
        result = await database.fetch_val(query, {"info_hash": info_hash})
        return bool(result)
    except Exception as e:
        logger.warning(f"Error checking torrent existence for {info_hash}: {e}")
        return False


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
        seasons_to_process = parsed.seasons if parsed.seasons else [search_season]
        parsed_episodes = parsed.episodes if parsed.episodes else [None]

        episode_to_insert = parsed_episodes[0] if len(parsed_episodes) == 1 else None

        for season in seasons_to_process:
            await _upsert_torrent_record(
                {
                    "media_id": media_id,
                    "info_hash": info_hash,
                    "file_index": file_index,
                    "season": season,
                    "episode": episode_to_insert,
                    "title": title,
                    "seeders": seeders,
                    "size": size,
                    "tracker": tracker,
                    "sources": orjson.dumps(sources).decode("utf-8"),
                    "parsed": orjson.dumps(parsed, default_dump).decode("utf-8"),
                    "timestamp": time.time(),
                }
            )

        # Broadcast to CometNet
        try:
            episode_to_broadcast = episode_to_insert

            broadcast_list = []
            for season in seasons_to_process:
                broadcast_list.append(
                    {
                        "info_hash": info_hash,
                        "title": title,
                        "size": size,
                        "tracker": tracker,
                        "media_id": media_id,
                        "file_index": file_index,
                        "seeders": seeders,
                        "season": season,
                        "episode": episode_to_broadcast,
                        "sources": sources,
                        "parsed": parsed.model_dump()
                        if hasattr(parsed, "model_dump")
                        else parsed,
                    }
                )

            if broadcast_list:
                await broadcast_torrents(broadcast_list)
        except Exception:
            pass  # Don't fail torrent insertion if CometNet fails

        additional = ""
        if seasons_to_process:
            additional += f" - S{seasons_to_process[0]:02d}"
            if parsed_episodes and parsed_episodes[0] is not None:
                additional += f"E{parsed_episodes[0]:02d}"

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
                                ensure_multi_language(parsed)

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
    __slots__ = (
        "queue",
        "batch_size",
        "flush_interval",
        "is_running",
        "_lock",
        "_event",
        "upserts",
        "_is_postgresql",
        "_grouped_upserts",
    )

    def __init__(self, batch_size: int = 1000, flush_interval: float = 5.0):
        self.queue = asyncio.Queue()
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.is_running = False
        self._lock = asyncio.Lock()
        self._event = asyncio.Event()
        self.upserts = {}
        self._is_postgresql = settings.DATABASE_TYPE == "postgresql"
        self._grouped_upserts = defaultdict(list)

    async def add_torrent_info(
        self, file_info: dict, media_id: str = None, from_cometnet: bool = False
    ):
        # Mark if this torrent came from CometNet (to avoid re-broadcasting)
        file_info["_from_cometnet"] = from_cometnet
        await self.queue.put((file_info, media_id))
        self._event.set()

        if not self.is_running:
            async with self._lock:
                if not self.is_running:
                    self.is_running = True
                    asyncio.create_task(self._process_queue())

    async def _process_queue(self):
        last_flush_time = time.time()

        try:
            while True:
                try:
                    await asyncio.wait_for(
                        self._event.wait(), timeout=self.flush_interval
                    )
                except asyncio.TimeoutError:
                    pass

                self._event.clear()

                batch_time = time.time()
                items_processed = 0
                while True:
                    try:
                        file_info, media_id = self.queue.get_nowait()
                        self._process_file_info(file_info, media_id, batch_time)
                        self.queue.task_done()
                        items_processed += 1

                        if len(self.upserts) >= self.batch_size:
                            await self._flush_batch()
                            last_flush_time = time.time()
                            batch_time = last_flush_time
                    except QueueEmpty:
                        break

                current_time = time.time()

                if self.upserts and (
                    current_time - last_flush_time >= self.flush_interval
                ):
                    await self._flush_batch()
                    last_flush_time = current_time

                if self.queue.empty() and not self.upserts:
                    break

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"Error in _process_queue: {e}")
        finally:
            if self.upserts:
                await self._flush_batch()
            if self.is_running:
                async with self._lock:
                    self.is_running = False

    async def stop(self):
        self.is_running = False
        self._event.set()

        shutdown_time = time.time()
        while True:
            try:
                file_info, media_id = self.queue.get_nowait()
                self._process_file_info(file_info, media_id, shutdown_time)
            except QueueEmpty:
                break
            except Exception as e:
                logger.warning(
                    f"Error processing remaining queue items during shutdown: {e}"
                )
                break

        if self.upserts:
            await self._flush_batch()

    async def _flush_batch(self):
        if not self.upserts:
            return

        upserts_to_flush = self.upserts
        self.upserts = {}

        try:
            grouped = self._grouped_upserts
            for params in upserts_to_flush.values():
                key = _determine_conflict_key(params["season"], params["episode"])
                grouped[key].append(params)

            for key, rows in grouped.items():
                query = _get_torrent_upsert_query(key)
                try:
                    await _execute_batched_upsert(query, rows)
                except Exception as e:
                    logger.warning(f"Error processing upsert batch: {e}")

            total_upserts = len(upserts_to_flush)
            if total_upserts > 0:
                logger.log(
                    "SCRAPER",
                    f"Upserted {total_upserts} torrents in batch",
                )

                # Broadcast to CometNet P2P network (only for locally discovered torrents)
                # Filter out torrents that came from CometNet to avoid ping-pong
                local_torrents = [
                    p
                    for p in upserts_to_flush.values()
                    if not p.get("_from_cometnet", False)
                ]

                if local_torrents:
                    await broadcast_torrents(local_torrents)

        except Exception as e:
            logger.warning(f"Error in flush_batch: {e}")
        finally:
            grouped.clear()

    def _process_file_info(
        self, file_info: dict, media_id: str = None, current_time: float = None
    ):
        try:
            info_hash = file_info["info_hash"]
            season = file_info["season"]
            episode = file_info["episode"]

            if current_time is None:
                current_time = time.time()

            upsert_key = (media_id, info_hash, season, episode)

            existing = self.upserts.get(upsert_key)
            if existing and existing["timestamp"] >= current_time:
                return

            params = {
                "info_hash": info_hash,
                "file_index": file_info["index"],
                "season": season,
                "episode": episode,
                "title": file_info["title"],
                "seeders": file_info["seeders"],
                "size": file_info["size"],
                "tracker": file_info["tracker"],
                "sources": orjson.dumps(file_info["sources"]).decode("utf-8"),
                "parsed": orjson.dumps(
                    file_info["parsed"], default=default_dump
                ).decode("utf-8"),
                "timestamp": current_time,
                "media_id": media_id,
            }

            if self._is_postgresql:
                params["update_interval"] = UPDATE_INTERVAL

            params["lock_key"] = _compute_advisory_lock_key(
                media_id, info_hash, season, episode
            )

            # Preserve the CometNet origin flag to prevent re-broadcasting
            params["_from_cometnet"] = file_info.get("_from_cometnet", False)

            self.upserts[upsert_key] = params

        except Exception as e:
            logger.warning(f"Error processing file info: {e}")


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


def _determine_conflict_key(season, episode):
    if season is not None:
        return "series" if episode is not None else "season_only"
    return "episode_only" if episode is not None else "none"


def _compute_advisory_lock_key(media_id, info_hash, season, episode):
    payload = f"{media_id}|{info_hash}|{season}|{episode}"
    return xxhash.xxh64_intdigest(payload, seed=0) - (1 << 63)


_SQLITE_CHECK_COLS = frozenset(
    ["file_index", "title", "seeders", "size", "tracker", "sources", "parsed"]
)


async def _execute_sqlite_batched_upsert(rows: list[dict]):
    if not rows:
        return

    info_hashes = {row["info_hash"] for row in rows}

    if not info_hashes:
        await _execute_standard_sqlite_insert(rows)
        return

    info_hashes_list = list(info_hashes)
    chunk_size = 900
    existing_rows = []

    for i in range(0, len(info_hashes_list), chunk_size):
        chunk = info_hashes_list[i : i + chunk_size]
        placeholders = ",".join(f":ih{j}" for j in range(len(chunk)))
        params = {f"ih{j}": ih for j, ih in enumerate(chunk)}

        chunk_rows = await database.fetch_all(
            f"SELECT media_id, info_hash, season, episode, file_index, title, seeders, size, tracker, sources, parsed, timestamp FROM torrents WHERE info_hash IN ({placeholders})",
            params,
        )
        existing_rows.extend(chunk_rows)

    existing_map = {
        (row["media_id"], row["info_hash"], row["season"], row["episode"]): row
        for row in existing_rows
    }

    if not existing_map:
        await _execute_standard_sqlite_insert(rows)
        return

    to_insert = []
    for row in rows:
        key = (row["media_id"], row["info_hash"], row["season"], row["episode"])
        existing = existing_map.get(key)

        if not existing:
            to_insert.append(row)
            continue

        if any(row.get(col) != existing[col] for col in _SQLITE_CHECK_COLS):
            to_insert.append(row)
            continue

        if existing["timestamp"] < (row["timestamp"] - UPDATE_INTERVAL):
            to_insert.append(row)

    if to_insert:
        await _execute_standard_sqlite_insert(to_insert)


async def _execute_standard_sqlite_insert(rows: list[dict]):
    keys_to_ignore = {"lock_key", "update_interval", "_from_cometnet"}
    columns = [k for k in rows[0].keys() if k not in keys_to_ignore]
    sanitized_rows = [{k: row[k] for k in columns} for row in rows]

    query = f"""
        INSERT OR REPLACE INTO torrents ({", ".join(columns)})
        VALUES ({", ".join(f":{col}" for col in columns)})
    """

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
    rows_to_insert = []

    try:
        async with database.transaction():
            for row in ordered_rows:
                lock_key = row.get("lock_key")
                if lock_key is None:
                    rows_to_insert.append(row)
                    continue

                acquired = await database.fetch_val(
                    "SELECT pg_try_advisory_xact_lock(CAST(:lock_key AS BIGINT))",
                    {"lock_key": lock_key},
                )
                if acquired:
                    rows_to_insert.append(row)

            if rows_to_insert:
                sanitized_rows = [
                    {
                        key: value
                        for key, value in row.items()
                        if key not in ("lock_key", "_from_cometnet")
                    }
                    for row in rows_to_insert
                ]

                await database.execute_many(query, sanitized_rows)

    except Exception as e:
        logger.warning(f"Error executing batched upsert: {e}")


def _get_torrent_upsert_query(conflict_key: str):
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


async def save_torrent_from_network(metadata: TorrentMetadata):
    """
    Save a torrent received from the CometNet P2P network.

    This function processes torrent metadata received from other peers
    and queues it for insertion into the database.
    """
    if not isinstance(metadata, TorrentMetadata):
        return

    await torrent_update_queue.add_torrent_info(
        {
            "info_hash": metadata.info_hash,
            "index": metadata.file_index,
            "season": metadata.season,
            "episode": metadata.episode,
            "title": metadata.title,
            "seeders": metadata.seeders,
            "size": metadata.size,
            "tracker": metadata.tracker,
            "sources": metadata.sources,
            "parsed": metadata.parsed,
        },
        media_id=metadata.imdb_id,
        from_cometnet=True,
    )
