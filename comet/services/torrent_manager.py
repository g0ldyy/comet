import asyncio
import dataclasses
import hashlib
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote

import anyio
import bencodepy
import orjson
from demagnetize.core import Demagnetizer
from RTN import parse
from torf import Magnet

from comet.cometnet import get_active_backend
from comet.cometnet.protocol import TorrentMetadata
from comet.core.constants import TORRENT_TIMEOUT
from comet.core.database import IS_SQLITE, normalize_scope_value
from comet.core.logger import logger
from comet.core.models import database, settings
from comet.utils.formatting import normalize_info_hash
from comet.utils.parsing import default_dump, ensure_multi_language, is_video

TRACKER_PATTERN = re.compile(r"[&?]tr=([^&]+)")
INFO_HASH_PATTERN = re.compile(r"btih:([a-fA-F0-9]{40}|[a-zA-Z0-9]{32})")
TORRENT_BASE_COLUMNS = (
    "media_id",
    "info_hash",
    "season",
    "episode",
    "season_norm",
    "episode_norm",
    "file_index",
    "title",
    "seeders",
    "size",
    "tracker",
)
TORRENT_DB_COLUMNS = TORRENT_BASE_COLUMNS + (
    "sources_json",
    "parsed_json",
    "updated_at",
)
POSTGRES_RECORDSET_COLUMNS = TORRENT_BASE_COLUMNS + (
    "sources",
    "parsed",
    "updated_at",
)
TORRENT_CONFLICT_COLUMNS = ("media_id", "info_hash", "season_norm", "episode_norm")
TORRENT_CONFLICT_TARGET = f"({', '.join(TORRENT_CONFLICT_COLUMNS)})"
TORRENT_UPDATE_COLUMNS = (
    "file_index",
    "title",
    "seeders",
    "size",
    "tracker",
    "sources_json",
    "parsed_json",
    "updated_at",
)
TORRENT_CHANGE_DETECTION_COLUMNS = tuple(
    column for column in TORRENT_UPDATE_COLUMNS if column != "updated_at"
)

DEFAULT_ADD_TORRENT_QUEUE_MAXSIZE = 256
DEFAULT_ADD_TORRENT_DROP_LOG_INTERVAL = 1000
DEFAULT_ADD_TORRENT_METADATA_CACHE_MAX_ENTRIES = 512
DEFAULT_TORRENT_UPDATE_QUEUE_MAXSIZE = 4096
DEFAULT_TORRENT_UPDATE_MAX_RETRIES = 3
DEFAULT_TORRENT_UPDATE_ENQUEUE_TIMEOUT = 0.25
DEFAULT_TORRENT_UPDATE_FLUSH_INTERVAL = 0.1
DEFAULT_TORRENT_UPDATE_DROP_LOG_INTERVAL = 1000
DEFAULT_TORRENT_REQUEUE_DROP_LOG_INTERVAL = 1000
DEFAULT_TORRENT_BROADCAST_QUEUE_MAXSIZE = 4096
DEFAULT_TORRENT_BROADCAST_DROP_LOG_INTERVAL = 1000
SQLITE_MAX_VARIABLES = 999
SQLITE_UPSERT_MAX_ROWS_PER_STATEMENT = max(
    1, SQLITE_MAX_VARIABLES // len(TORRENT_DB_COLUMNS)
)


def _json_dumps(value) -> str:
    return orjson.dumps(value).decode("utf-8")


def _coerce_parsed_payload(parsed) -> dict:
    if isinstance(parsed, dict):
        return parsed
    if parsed is None:
        return {}
    try:
        dumped = default_dump(parsed)
    except Exception:
        return {}
    return dumped if isinstance(dumped, dict) else {}


def _dedupe_strings(values: list[str]) -> list[str]:
    if len(values) <= 1:
        return values
    return list(dict.fromkeys(values))


def _normalize_sources(sources) -> list[str]:
    if isinstance(sources, list):
        values = sources
    elif isinstance(sources, tuple):
        values = list(sources)
    else:
        return []
    return _dedupe_strings(values)


def _prune_ordered_dict(cache: OrderedDict, *, max_entries: int):
    while len(cache) > max_entries:
        cache.popitem(last=False)


def _is_relevant_video_file(title: str) -> bool:
    return bool(title) and is_video(title) and "sample" not in title.lower()


def _parse_video_title(title: str):
    try:
        parsed = parse(title)
        ensure_multi_language(parsed)
    except Exception:
        return None
    return parsed


def extract_trackers_from_magnet(magnet_uri: str):
    return _dedupe_strings(
        [unquote(tracker) for tracker in TRACKER_PATTERN.findall(magnet_uri)]
    )


def _extract_info_hash_from_magnet(magnet_uri: str) -> str | None:
    match = INFO_HASH_PATTERN.search(magnet_uri)
    if not match:
        return None

    info_hash = match.group(1)
    if len(info_hash) == 32:
        info_hash = normalize_info_hash(info_hash)
    return info_hash.lower()


def _build_extracted_file_entry(
    index: int,
    title: str,
    size,
    *,
    parse_title: bool,
) -> dict | None:
    if not _is_relevant_video_file(title):
        return None

    entry = {
        "index": index,
        "title": title,
        "size": size,
    }
    if not parse_title:
        return entry

    parsed = _parse_video_title(title)
    if parsed is None:
        return None
    entry["parsed"] = parsed
    return entry


def _extract_relevant_file_entries(file_specs, *, parse_titles: bool) -> list[dict]:
    files = []
    for index, title, size in file_specs:
        file_entry = _build_extracted_file_entry(
            index,
            title,
            size,
            parse_title=parse_titles,
        )
        if file_entry is not None:
            files.append(file_entry)
    return files


async def download_torrent(session, url: str):
    try:
        async with session.get(
            url, allow_redirects=False, timeout=TORRENT_TIMEOUT
        ) as response:
            if response.status == 200:
                return (await response.read(), None, None)

            location = response.headers.get("Location", "")
            if location:
                info_hash = _extract_info_hash_from_magnet(location)
                if info_hash:
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
            torrent = await demagnetizer.demagnetize(magnet)
            if torrent:
                return torrent
    except Exception as e:
        logger.warning(f"Failed to get torrent from magnet: {e}")
    return None


def _resolve_torrent_metadata(torrent) -> dict:
    try:
        info_hash = normalize_info_hash(torrent.infohash).lower()
    except Exception:
        return {}

    trackers = []
    for tier in torrent.trackers:
        for tracker in tier:
            if isinstance(tracker, str):
                trackers.append(tracker)

    return {
        "info_hash": info_hash,
        "sources": _dedupe_strings(trackers),
        "files": _extract_relevant_file_entries(
            (
                (index, Path(torrent_file).name, torrent_file.size)
                for index, torrent_file in enumerate(torrent.files)
            ),
            parse_titles=False,
        ),
    }


def extract_torrent_metadata(content: bytes):
    try:
        torrent_data = bencodepy.decode(content)
        info = torrent_data[b"info"]
        info_hash = hashlib.sha1(bencodepy.encode(info)).hexdigest()

        announce_list = [
            tracker[0].decode() for tracker in torrent_data.get(b"announce-list", [])
        ]
        announce = torrent_data.get(b"announce", b"").decode()
        if announce:
            announce_list.append(announce)
        announce_list = _dedupe_strings(announce_list)

        files = info[b"files"] if b"files" in info else [info]
        return {
            "info_hash": info_hash,
            "sources": announce_list,
            "files": _extract_relevant_file_entries(
                (
                    (
                        index,
                        file_info[b"path"][-1].decode()
                        if b"path" in file_info
                        else file_info[b"name"].decode(),
                        file_info[b"length"],
                    )
                    for index, file_info in enumerate(files)
                ),
                parse_titles=False,
            ),
        }
    except Exception as e:
        logger.warning(f"Failed to extract torrent metadata: {e}")
        return {}


def _is_valid_imdb_id(value) -> bool:
    return isinstance(value, str) and value.startswith("tt")


@dataclass(slots=True)
class _TorrentUpdate:
    media_id: str
    info_hash: str
    season: int | None
    episode: int | None
    file_index: int | None
    title: str
    seeders: int | None
    size: int | None
    tracker: str | None
    sources: list[str]
    parsed: dict
    from_cometnet: bool
    attempts: int = 0
    season_norm: int = field(init=False)
    episode_norm: int = field(init=False)

    def __post_init__(self):
        self.season_norm = normalize_scope_value(self.season)
        self.episode_norm = normalize_scope_value(self.episode)

    @property
    def row_key(self) -> tuple[str, str, int, int]:
        return (
            self.media_id,
            self.info_hash,
            self.season_norm,
            self.episode_norm,
        )

    def _base_row_values(self) -> tuple:
        return (
            self.media_id,
            self.info_hash,
            self.season,
            self.episode,
            self.season_norm,
            self.episode_norm,
            self.file_index,
            self.title,
            self.seeders,
            self.size,
            self.tracker,
        )

    def iter_sqlite_params(self, index: int, updated_at: float):
        values = self._base_row_values() + (
            _json_dumps(self.sources),
            _json_dumps(self.parsed),
            updated_at,
        )
        for column, value in zip(TORRENT_DB_COLUMNS, values):
            yield f"{column}_{index}", value

    def to_postgres_payload(self, updated_at: float) -> dict:
        values = self._base_row_values() + (self.sources, self.parsed, updated_at)
        return dict(zip(POSTGRES_RECORDSET_COLUMNS, values))

    def to_broadcast_metadata(self, updated_at: float) -> TorrentMetadata:
        return TorrentMetadata(
            info_hash=self.info_hash,
            title=self.title,
            size=int(self.size or 0),
            tracker=self.tracker or "",
            imdb_id=self.media_id,
            file_index=self.file_index,
            seeders=self.seeders,
            season=self.season,
            episode=self.episode,
            sources=self.sources,
            parsed=self.parsed,
            updated_at=updated_at,
        )


def _build_torrent_update(
    *,
    media_id: str,
    info_hash,
    title,
    file_index=None,
    season=None,
    episode=None,
    seeders=None,
    size=None,
    tracker=None,
    sources=None,
    parsed=None,
    from_cometnet: bool,
) -> _TorrentUpdate | None:
    if not isinstance(info_hash, str):
        return None

    normalized_info_hash = normalize_info_hash(info_hash).lower()
    if len(normalized_info_hash) != 40:
        return None

    if not isinstance(title, str) or not title:
        return None

    return _TorrentUpdate(
        media_id=media_id,
        info_hash=normalized_info_hash,
        season=season,
        episode=episode,
        file_index=file_index,
        title=title,
        seeders=seeders,
        size=size,
        tracker=tracker,
        sources=_normalize_sources(sources),
        parsed=_coerce_parsed_payload(parsed),
        from_cometnet=from_cometnet,
    )


def _build_resolved_torrent_updates(
    resolved_torrent: dict,
    *,
    media_id: str,
    seeders: int,
    tracker: str,
    search_season: int | None,
) -> list[_TorrentUpdate]:
    info_hash = resolved_torrent.get("info_hash")
    if not isinstance(info_hash, str) or len(info_hash) != 40:
        return []

    sources = _normalize_sources(resolved_torrent.get("sources"))
    items = []
    for resolved_file in resolved_torrent.get("files", []):
        title = resolved_file.get("title")
        if not isinstance(title, str) or not title:
            continue

        parsed_data = _parse_video_title(title)
        if parsed_data is None:
            continue

        seasons = getattr(parsed_data, "seasons", None) or [search_season]
        parsed_episodes = getattr(parsed_data, "episodes", None) or [None]
        parsed = _coerce_parsed_payload(parsed_data)
        episode = parsed_episodes[0] if len(parsed_episodes) == 1 else None
        for season in seasons:
            item = _build_torrent_update(
                media_id=media_id,
                info_hash=info_hash,
                title=title,
                file_index=resolved_file.get("index"),
                season=season,
                episode=episode,
                seeders=seeders,
                size=resolved_file.get("size"),
                tracker=tracker,
                sources=sources,
                parsed=parsed,
                from_cometnet=False,
            )
            if item is not None:
                items.append(item)

    return items


async def _collect_queue_batch(
    queue: asyncio.Queue,
    first_item,
    *,
    max_items: int,
    flush_interval: float,
) -> list:
    batch = [first_item]

    while len(batch) < max_items:
        try:
            batch.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break

    if len(batch) >= max_items or flush_interval <= 0:
        return batch

    deadline = time.monotonic() + flush_interval
    while len(batch) < max_items:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break

        try:
            batch.append(queue.get_nowait())
            continue
        except asyncio.QueueEmpty:
            pass

        try:
            batch.append(await asyncio.wait_for(queue.get(), timeout=remaining))
        except asyncio.TimeoutError:
            break

    return batch


@lru_cache(maxsize=32)
def _build_check_torrents_exist_query(hash_count: int) -> str:
    placeholders = ", ".join(f":info_hash_{index}" for index in range(hash_count))
    return f"""
SELECT info_hash
FROM torrents
WHERE info_hash IN ({placeholders})
"""


def _build_check_torrents_exist_params(info_hashes: list[str]) -> dict:
    return {
        f"info_hash_{index}": info_hash for index, info_hash in enumerate(info_hashes)
    }


async def check_torrents_exist(info_hashes: list[str]) -> set[str]:
    if not info_hashes:
        return set()

    unique_hashes = list(dict.fromkeys(info_hashes))
    chunk_size = SQLITE_MAX_VARIABLES if IS_SQLITE else len(unique_hashes)
    existing_hashes = set()

    try:
        for start in range(0, len(unique_hashes), chunk_size):
            chunk = unique_hashes[start : start + chunk_size]
            rows = await database.fetch_all(
                _build_check_torrents_exist_query(len(chunk)),
                _build_check_torrents_exist_params(chunk),
            )
            existing_hashes.update(row["info_hash"] for row in rows)
        return existing_hashes
    except Exception as e:
        logger.warning(f"Error checking batch torrent persistence: {e}")
        return set()


class AddTorrentQueue:
    _STOP = object()

    def __init__(self, max_concurrent: int = 10):
        self.queue = asyncio.Queue(maxsize=DEFAULT_ADD_TORRENT_QUEUE_MAXSIZE)
        self.max_concurrent = max(1, max_concurrent)
        self.metadata_cache_max_entries = DEFAULT_ADD_TORRENT_METADATA_CACHE_MAX_ENTRIES
        self._lock = asyncio.Lock()
        self._workers = []
        self._stopping = False
        self._dropped_torrents = 0
        self._pending_torrents = set()
        self._resolved_torrent_cache = OrderedDict()
        self._inflight_resolutions = {}

    def _reset_runtime_state(self):
        self._workers = []
        self._pending_torrents.clear()
        self._inflight_resolutions.clear()
        self._resolved_torrent_cache.clear()

    async def add_torrent(
        self,
        magnet_url: str,
        seeders: int,
        tracker: str,
        media_id: str,
        search_season: int | None,
    ):
        if not settings.DOWNLOAD_TORRENT_FILES or self._stopping:
            return

        magnet_key = _extract_info_hash_from_magnet(magnet_url)
        if not magnet_key:
            return

        queue_key = (media_id, magnet_key, search_season)

        async with self._lock:
            if queue_key in self._pending_torrents:
                return
            self._pending_torrents.add(queue_key)

        try:
            self.queue.put_nowait(
                (
                    queue_key,
                    magnet_url,
                    seeders,
                    tracker,
                )
            )
        except asyncio.QueueFull:
            async with self._lock:
                self._pending_torrents.discard(queue_key)
            self._dropped_torrents += 1
            if (self._dropped_torrents % DEFAULT_ADD_TORRENT_DROP_LOG_INTERVAL) == 0:
                logger.warning(
                    "Add torrent queue is full, dropped "
                    f"{self._dropped_torrents} magnet enrichments total"
                )
            return

        await self._ensure_workers()

    async def _ensure_workers(self):
        async with self._lock:
            self._workers[:] = [task for task in self._workers if not task.done()]
            missing_workers = self.max_concurrent - len(self._workers)
            if missing_workers <= 0:
                return

            self._workers.extend(
                asyncio.create_task(self._worker()) for _ in range(missing_workers)
            )

    async def _worker(self):
        while True:
            item = await self.queue.get()
            if item is self._STOP:
                self.queue.task_done()
                return

            (
                queue_key,
                magnet_url,
                seeders,
                tracker,
            ) = item
            media_id, magnet_key, search_season = queue_key

            try:
                resolved_torrent = await self._get_resolved_torrent(
                    magnet_key, magnet_url
                )
                if resolved_torrent:
                    await torrent_update_queue.add_resolved_torrent(
                        resolved_torrent,
                        media_id=media_id,
                        seeders=seeders,
                        tracker=tracker,
                        search_season=search_season,
                    )
            except Exception as e:
                logger.warning(f"Error while processing magnet torrent: {e}")
            finally:
                async with self._lock:
                    self._pending_torrents.discard(queue_key)
                self.queue.task_done()

    async def _get_resolved_torrent(self, magnet_key: str, magnet_url: str) -> dict:
        loop = asyncio.get_running_loop()
        async with self._lock:
            cached = self._resolved_torrent_cache.get(magnet_key)
            if cached is not None:
                self._resolved_torrent_cache.move_to_end(magnet_key)
                return cached

            future = self._inflight_resolutions.get(magnet_key)
            if future is None:
                future = loop.create_future()
                self._inflight_resolutions[magnet_key] = future
                owner = True
            else:
                owner = False

        if not owner:
            return await future

        resolved_torrent = {}
        try:
            torrent = await get_torrent_from_magnet(magnet_url)
            if torrent:
                resolved_torrent = await asyncio.to_thread(
                    _resolve_torrent_metadata, torrent
                )
        finally:
            async with self._lock:
                self._resolved_torrent_cache[magnet_key] = resolved_torrent
                self._resolved_torrent_cache.move_to_end(magnet_key)
                _prune_ordered_dict(
                    self._resolved_torrent_cache,
                    max_entries=self.metadata_cache_max_entries,
                )

                inflight = self._inflight_resolutions.pop(magnet_key, None)
                if inflight is not None and not inflight.done():
                    inflight.set_result(resolved_torrent)

        return resolved_torrent

    async def stop(self):
        async with self._lock:
            self._stopping = True

            active_workers = [task for task in self._workers if not task.done()]
        if not active_workers:
            async with self._lock:
                self._reset_runtime_state()
            return

        await self.queue.join()
        for _ in active_workers:
            await self.queue.put(self._STOP)
        await asyncio.gather(*active_workers, return_exceptions=True)

        async with self._lock:
            self._reset_runtime_state()


add_torrent_queue = AddTorrentQueue()


class TorrentUpdateQueue:
    _STOP = object()

    def __init__(
        self,
        batch_size: int = 1000,
        flush_interval: float = DEFAULT_TORRENT_UPDATE_FLUSH_INTERVAL,
    ):
        self.queue = asyncio.Queue(maxsize=DEFAULT_TORRENT_UPDATE_QUEUE_MAXSIZE)
        self._broadcast_queue = asyncio.Queue(
            maxsize=DEFAULT_TORRENT_BROADCAST_QUEUE_MAXSIZE
        )
        self.batch_size = max(1, batch_size)
        self.flush_interval = max(0.0, flush_interval)
        self.max_retries = DEFAULT_TORRENT_UPDATE_MAX_RETRIES
        self.enqueue_timeout = DEFAULT_TORRENT_UPDATE_ENQUEUE_TIMEOUT
        self._lock = asyncio.Lock()
        self._worker = None
        self._broadcast_worker = None
        self._stopping = False
        self._dropped_updates = 0
        self._dropped_requeues = 0
        self._dropped_broadcasts = 0
        self._pending_updates = {}

    async def add_torrent_info(
        self, file_info: dict, media_id: str = None, from_cometnet: bool = False
    ):
        if self._stopping or not isinstance(media_id, str) or not media_id:
            return

        await self._enqueue_prepared_items(
            self._prepare_queue_items([file_info], media_id, from_cometnet)
        )

    async def add_torrent_infos(
        self, file_infos: list[dict], media_id: str = None, from_cometnet: bool = False
    ):
        if (
            not file_infos
            or self._stopping
            or not isinstance(media_id, str)
            or not media_id
        ):
            return

        await self._enqueue_prepared_items(
            self._prepare_queue_items(file_infos, media_id, from_cometnet)
        )

    async def add_resolved_torrent(
        self,
        resolved_torrent: dict,
        *,
        media_id: str,
        seeders: int,
        tracker: str,
        search_season: int | None,
    ):
        if self._stopping or not isinstance(media_id, str) or not media_id:
            return

        items = await asyncio.to_thread(
            _build_resolved_torrent_updates,
            resolved_torrent,
            media_id=media_id,
            seeders=seeders,
            tracker=tracker,
            search_season=search_season,
        )
        await self._enqueue_prepared_items(items)

    async def add_network_torrent(self, metadata: TorrentMetadata):
        if self._stopping or not _is_valid_imdb_id(metadata.imdb_id):
            return

        item = _build_torrent_update(
            media_id=metadata.imdb_id,
            info_hash=metadata.info_hash,
            title=metadata.title,
            file_index=metadata.file_index,
            season=metadata.season,
            episode=metadata.episode,
            seeders=metadata.seeders,
            size=metadata.size,
            tracker=metadata.tracker,
            sources=metadata.sources,
            parsed=metadata.parsed,
            from_cometnet=True,
        )
        if item is None:
            return

        await self._enqueue_prepared_items([item])

    async def _ensure_worker(self):
        async with self._lock:
            if self._worker is not None and not self._worker.done():
                return
            self._worker = asyncio.create_task(self._process_queue())

    async def _ensure_broadcast_worker(self):
        async with self._lock:
            if self._broadcast_worker is not None and not self._broadcast_worker.done():
                return
            self._broadcast_worker = asyncio.create_task(
                self._process_broadcast_queue()
            )

    async def _enqueue_prepared_items(self, items: list[_TorrentUpdate]):
        if not items:
            return

        await self._ensure_worker()
        await self._queue_items(items, drop_callback=self._record_dropped_updates)

    def _prepare_queue_items(
        self, file_infos: list[dict], media_id: str, from_cometnet: bool
    ) -> list[_TorrentUpdate]:
        items = []
        for file_info in file_infos:
            if not isinstance(file_info, dict):
                continue
            item = _build_torrent_update(
                media_id=media_id,
                info_hash=file_info.get("info_hash"),
                title=file_info.get("title"),
                file_index=file_info.get("index"),
                season=file_info.get("season"),
                episode=file_info.get("episode"),
                seeders=file_info.get("seeders"),
                size=file_info.get("size"),
                tracker=file_info.get("tracker"),
                sources=file_info.get("sources"),
                parsed=file_info.get("parsed"),
                from_cometnet=from_cometnet,
            )
            if item is not None:
                items.append(item)
        return items

    def _merge_pending_item(
        self, existing: _TorrentUpdate, incoming: _TorrentUpdate
    ) -> _TorrentUpdate:
        return dataclasses.replace(
            incoming,
            from_cometnet=existing.from_cometnet and incoming.from_cometnet,
            attempts=max(existing.attempts, incoming.attempts),
        )

    def _coalesce_items(self, items: list[_TorrentUpdate]) -> list[_TorrentUpdate]:
        deduped_items = {}
        for item in items:
            existing = deduped_items.get(item.row_key)
            deduped_items[item.row_key] = (
                item if existing is None else self._merge_pending_item(existing, item)
            )
        return list(deduped_items.values())

    async def _queue_item(self, item: _TorrentUpdate, *, drop_callback):
        row_key = item.row_key
        async with self._lock:
            existing = self._pending_updates.get(row_key)
            if existing is not None:
                self._pending_updates[row_key] = self._merge_pending_item(
                    existing, item
                )
                return

            self._pending_updates[row_key] = item
            try:
                self.queue.put_nowait(row_key)
                return
            except asyncio.QueueFull:
                pass

        try:
            await asyncio.wait_for(
                self.queue.put(row_key), timeout=self.enqueue_timeout
            )
        except asyncio.TimeoutError:
            async with self._lock:
                self._pending_updates.pop(row_key, None)
            drop_callback(1)

    async def _queue_items(self, items: list[_TorrentUpdate], *, drop_callback):
        coalesced_items = self._coalesce_items(items)
        if not coalesced_items:
            return
        if len(coalesced_items) == 1:
            await self._queue_item(coalesced_items[0], drop_callback=drop_callback)
            return

        deadline = None

        for index, item in enumerate(coalesced_items):
            row_key = item.row_key
            async with self._lock:
                existing = self._pending_updates.get(row_key)
                if existing is not None:
                    self._pending_updates[row_key] = self._merge_pending_item(
                        existing, item
                    )
                    continue

                self._pending_updates[row_key] = item
                try:
                    self.queue.put_nowait(row_key)
                    continue
                except asyncio.QueueFull:
                    pass

            if deadline is None:
                deadline = time.monotonic() + self.enqueue_timeout

            timeout = deadline - time.monotonic()
            if timeout <= 0:
                async with self._lock:
                    self._pending_updates.pop(row_key, None)
                drop_callback(len(coalesced_items) - index)
                return

            try:
                await asyncio.wait_for(self.queue.put(row_key), timeout=timeout)
                continue
            except asyncio.TimeoutError:
                async with self._lock:
                    self._pending_updates.pop(row_key, None)
                drop_callback(len(coalesced_items) - index)
                return

    def _record_dropped_updates(self, count: int):
        self._dropped_updates += count
        if self._dropped_updates % DEFAULT_TORRENT_UPDATE_DROP_LOG_INTERVAL < count:
            logger.warning(
                "Torrent update queue remained saturated, dropped "
                f"{self._dropped_updates} update items total"
            )

    def _record_dropped_requeues(self, count: int):
        self._dropped_requeues += count
        if self._dropped_requeues % DEFAULT_TORRENT_REQUEUE_DROP_LOG_INTERVAL < count:
            logger.warning(
                "Torrent update queue is full, dropped "
                f"{self._dropped_requeues} retry items total"
            )

    def _record_dropped_broadcasts(self, count: int):
        self._dropped_broadcasts += count
        if (
            self._dropped_broadcasts % DEFAULT_TORRENT_BROADCAST_DROP_LOG_INTERVAL
            < count
        ):
            logger.warning(
                "Torrent broadcast queue remained saturated, dropped "
                f"{self._dropped_broadcasts} broadcast items total"
            )

    async def _enqueue_broadcast_items(
        self, batch_items: list[_TorrentUpdate], updated_at: float
    ):
        if not batch_items or get_active_backend() is None:
            return

        await self._ensure_broadcast_worker()

        for index, item in enumerate(batch_items):
            try:
                self._broadcast_queue.put_nowait((updated_at, item))
            except asyncio.QueueFull:
                self._record_dropped_broadcasts(len(batch_items) - index)
                return

    async def _requeue_batch_items(self, batch_items: list[_TorrentUpdate]):
        requeue_candidates = []
        exhausted_retries = 0
        for item in batch_items:
            if item.attempts >= self.max_retries:
                exhausted_retries += 1
                continue
            requeue_candidates.append(
                dataclasses.replace(item, attempts=item.attempts + 1)
            )

        if exhausted_retries:
            logger.warning(
                f"Dropping {exhausted_retries} torrent updates after {self.max_retries} retries"
            )

        if not requeue_candidates:
            return

        await self._queue_items(
            requeue_candidates,
            drop_callback=self._record_dropped_requeues,
        )

    async def _pop_batch_items(self, batch_keys: list[tuple[str, str, int, int]]):
        async with self._lock:
            return [
                item
                for row_key in batch_keys
                if (item := self._pending_updates.pop(row_key, None)) is not None
            ]

    async def _process_queue(self):
        try:
            while True:
                first_item = await self.queue.get()
                if first_item is self._STOP:
                    self.queue.task_done()
                    return

                batch_keys = await _collect_queue_batch(
                    self.queue,
                    first_item,
                    max_items=self.batch_size,
                    flush_interval=0.0 if self._stopping else self.flush_interval,
                )
                batch_items = await self._pop_batch_items(batch_keys)

                updated_at = time.time()
                try:
                    if batch_items:
                        await _execute_batched_upsert(
                            batch_items, updated_at=updated_at
                        )
                except Exception as e:
                    logger.warning(f"Error in torrent update batch: {e}")
                    if _is_retryable_db_error(e):
                        await self._requeue_batch_items(batch_items)
                else:
                    await self._enqueue_broadcast_items(
                        [item for item in batch_items if not item.from_cometnet],
                        updated_at,
                    )
                finally:
                    for _ in batch_keys:
                        self.queue.task_done()
        finally:
            async with self._lock:
                if self._worker is asyncio.current_task():
                    self._worker = None

    async def _process_broadcast_queue(self):
        try:
            while True:
                first_item = await self._broadcast_queue.get()
                if first_item is self._STOP:
                    self._broadcast_queue.task_done()
                    return

                batch_items = await _collect_queue_batch(
                    self._broadcast_queue,
                    first_item,
                    max_items=self.batch_size,
                    flush_interval=0.0 if self._stopping else self.flush_interval,
                )

                backend = get_active_backend()
                if backend is not None:
                    try:
                        await backend.broadcast_torrents(
                            [
                                item.to_broadcast_metadata(updated_at)
                                for updated_at, item in batch_items
                            ]
                        )
                    except Exception as e:
                        logger.warning(f"Error while broadcasting torrents: {e}")
                for _ in batch_items:
                    self._broadcast_queue.task_done()
        finally:
            async with self._lock:
                if self._broadcast_worker is asyncio.current_task():
                    self._broadcast_worker = None

    async def stop(self):
        async with self._lock:
            self._stopping = True
            worker = (
                self._worker
                if self._worker is not None and not self._worker.done()
                else None
            )
            broadcast_worker = (
                self._broadcast_worker
                if self._broadcast_worker is not None
                and not self._broadcast_worker.done()
                else None
            )

        if worker is not None:
            await self.queue.join()
            await self.queue.put(self._STOP)
            await asyncio.gather(worker, return_exceptions=True)

        if broadcast_worker is not None:
            await self._broadcast_queue.join()
            await self._broadcast_queue.put(self._STOP)
            await asyncio.gather(broadcast_worker, return_exceptions=True)

        async with self._lock:
            self._pending_updates.clear()


TORRENT_COLUMNS_SQL = ",\n    ".join(TORRENT_DB_COLUMNS)
TORRENT_UPDATE_SET_SQL = ",\n        ".join(
    f"{column} = EXCLUDED.{column}" for column in TORRENT_UPDATE_COLUMNS
)
SQLITE_DISTINCT_UPDATE_WHERE_SQL = " OR ".join(
    f"torrents.{column} IS NOT excluded.{column}"
    for column in TORRENT_CHANGE_DETECTION_COLUMNS
)
POSTGRES_DISTINCT_UPDATE_WHERE_SQL = " OR ".join(
    f"torrents.{column} IS DISTINCT FROM EXCLUDED.{column}"
    for column in TORRENT_CHANGE_DETECTION_COLUMNS
)

POSTGRES_RECORDSET_COLUMN_TYPES = {
    "media_id": "TEXT",
    "info_hash": "TEXT",
    "season": "INTEGER",
    "episode": "INTEGER",
    "season_norm": "INTEGER",
    "episode_norm": "INTEGER",
    "file_index": "INTEGER",
    "title": "TEXT",
    "seeders": "INTEGER",
    "size": "BIGINT",
    "tracker": "TEXT",
    "sources": "JSONB",
    "parsed": "JSONB",
    "updated_at": "DOUBLE PRECISION",
}
POSTGRES_RECORDSET_COLUMNS_SQL = ",\n                ".join(
    f"{column} {POSTGRES_RECORDSET_COLUMN_TYPES[column]}"
    for column in POSTGRES_RECORDSET_COLUMNS
)
POSTGRES_SELECT_COLUMNS_SQL = ",\n    ".join(
    TORRENT_BASE_COLUMNS
    + (
        "CAST(COALESCE(sources, '[]'::jsonb) AS TEXT) AS sources_json",
        "CAST(COALESCE(parsed, '{}'::jsonb) AS TEXT) AS parsed_json",
        "updated_at",
    )
)

POSTGRES_BATCHED_UPSERT_QUERY = f"""
WITH incoming AS (
    SELECT *
    FROM jsonb_to_recordset(CAST(:rows_json AS JSONB)) AS incoming(
                {POSTGRES_RECORDSET_COLUMNS_SQL}
    )
)
INSERT INTO torrents (
    {TORRENT_COLUMNS_SQL}
)
SELECT
    {POSTGRES_SELECT_COLUMNS_SQL}
FROM incoming
ON CONFLICT {TORRENT_CONFLICT_TARGET}
DO UPDATE SET
        {TORRENT_UPDATE_SET_SQL}
WHERE
    {POSTGRES_DISTINCT_UPDATE_WHERE_SQL}
"""


@lru_cache(maxsize=16)
def _build_sqlite_batched_upsert_query(row_count: int) -> str:
    values_sql = ",\n".join(
        "(\n    "
        + ",\n    ".join(f":{column}_{index}" for column in TORRENT_DB_COLUMNS)
        + "\n)"
        for index in range(row_count)
    )
    return f"""
INSERT INTO torrents (
    {TORRENT_COLUMNS_SQL}
) VALUES
{values_sql}
ON CONFLICT {TORRENT_CONFLICT_TARGET}
DO UPDATE SET
        {TORRENT_UPDATE_SET_SQL}
WHERE
    {SQLITE_DISTINCT_UPDATE_WHERE_SQL}
"""


def _build_sqlite_batched_params(
    rows: list[_TorrentUpdate], *, updated_at: float
) -> dict:
    params = {}
    for index, item in enumerate(rows):
        params.update(item.iter_sqlite_params(index, updated_at))
    return params


def _is_retryable_db_error(exc: Exception) -> bool:
    error_message = str(exc).lower()
    return any(
        marker in error_message
        for marker in (
            "database is locked",
            "database is busy",
            "deadlock detected",
            "deadlock",
            "lock wait timeout",
            "could not serialize access",
            "serialization failure",
        )
    )


async def _execute_sqlite_upsert(rows: list[_TorrentUpdate], *, updated_at: float):
    if not rows:
        return

    async with database.transaction():
        for start in range(0, len(rows), SQLITE_UPSERT_MAX_ROWS_PER_STATEMENT):
            chunk = rows[start : start + SQLITE_UPSERT_MAX_ROWS_PER_STATEMENT]
            await database.execute(
                _build_sqlite_batched_upsert_query(len(chunk)),
                _build_sqlite_batched_params(chunk, updated_at=updated_at),
            )


async def _execute_batched_upsert(rows: list[_TorrentUpdate], *, updated_at: float):
    if IS_SQLITE:
        await _execute_sqlite_upsert(rows, updated_at=updated_at)
        return

    await database.execute(
        POSTGRES_BATCHED_UPSERT_QUERY,
        {
            "rows_json": _json_dumps(
                [row.to_postgres_payload(updated_at) for row in rows]
            )
        },
    )


torrent_update_queue = TorrentUpdateQueue()
