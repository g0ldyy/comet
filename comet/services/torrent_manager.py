import asyncio
import hashlib
import heapq
import random
import re
import time
from collections import OrderedDict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from functools import lru_cache
from itertools import chain
from pathlib import Path
from urllib.parse import unquote

import anyio
import bencodepy
import orjson
from demagnetize.core import Demagnetizer
from RTN import parse
from torf import Magnet

from comet.cometnet import CometNetService, get_active_backend
from comet.cometnet.protocol import TorrentMetadata
from comet.core.constants import TORRENT_TIMEOUT
from comet.core.database import (IS_SQLITE, NULL_SCOPE_SENTINEL,
                                 normalize_scope_value)
from comet.core.logger import logger
from comet.core.models import database, settings
from comet.utils.formatting import normalize_info_hash
from comet.utils.parsing import default_dump, ensure_multi_language, is_video

TRACKER_PATTERN = re.compile(r"[&?]tr=([^&]+)")
INFO_HASH_PATTERN = re.compile(r"btih:([a-fA-F0-9]{40}|[a-zA-Z0-9]{32})")
TORRENT_DB_COLUMNS = (
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
    "sources_json",
    "parsed_json",
    "updated_at",
)
TORRENT_UPSERT_PARAM_COLUMNS = (
    "media_id",
    "info_hash",
    "season",
    "episode",
    "file_index",
    "title",
    "seeders",
    "size",
    "tracker",
    "sources_json",
    "parsed_json",
)
TORRENT_UPSERT_ROW_PARAM_COUNT = len(TORRENT_UPSERT_PARAM_COLUMNS)
TORRENT_UPSERT_SHARED_PARAM_COUNT = 2
TORRENT_UPSERT_PARAM_COUNT = (
    TORRENT_UPSERT_ROW_PARAM_COUNT + TORRENT_UPSERT_SHARED_PARAM_COUNT
)
TORRENT_CONFLICT_COLUMNS = ("media_id", "info_hash", "season_norm", "episode_norm")
TORRENT_CONFLICT_TARGET = f"({', '.join(TORRENT_CONFLICT_COLUMNS)})"
TORRENT_IMMUTABLE_COLUMNS = frozenset(
    {
        "media_id",
        "info_hash",
        "season",
        "episode",
        "season_norm",
        "episode_norm",
    }
)
TORRENT_UPDATE_COLUMNS = tuple(
    column for column in TORRENT_DB_COLUMNS if column not in TORRENT_IMMUTABLE_COLUMNS
)
TORRENT_CHANGE_DETECTION_COLUMNS = tuple(
    column for column in TORRENT_UPDATE_COLUMNS if column != "updated_at"
)
TORRENT_STABLE_REFRESH_INTERVAL = 31536000
if settings.LIVE_TORRENT_CACHE_TTL is not None and settings.LIVE_TORRENT_CACHE_TTL >= 0:
    TORRENT_STABLE_REFRESH_INTERVAL = settings.LIVE_TORRENT_CACHE_TTL // 2

DEFAULT_ADD_TORRENT_QUEUE_MAXSIZE = 256
DEFAULT_ADD_TORRENT_METADATA_CACHE_MAX_ENTRIES = 512
DEFAULT_TORRENT_UPDATE_BATCH_SIZE = 1000
DEFAULT_TORRENT_UPDATE_QUEUE_MAXSIZE = 8192
DEFAULT_TORRENT_UPDATE_MAX_RETRIES = None
DEFAULT_TORRENT_UPDATE_FLUSH_INTERVAL = 0.1
DEFAULT_TORRENT_BROADCAST_QUEUE_MAXSIZE = 4096
DEFAULT_TORRENT_BROADCAST_BATCH_QUEUE_MIN_SIZE = 32
DEFAULT_TORRENT_BROADCAST_MAX_RETRIES = 5
SQLITE_MAX_VARIABLES = 999
POSTGRES_MAX_PARAMETERS = 32767
SQLITE_UPSERT_MAX_ROWS_PER_STATEMENT = max(
    1,
    (SQLITE_MAX_VARIABLES - TORRENT_UPSERT_SHARED_PARAM_COUNT)
    // TORRENT_UPSERT_ROW_PARAM_COUNT,
)
POSTGRES_UPSERT_MAX_ROWS_PER_STATEMENT = max(
    1,
    (POSTGRES_MAX_PARAMETERS - TORRENT_UPSERT_SHARED_PARAM_COUNT)
    // TORRENT_UPSERT_ROW_PARAM_COUNT,
)
DEFAULT_TORRENT_UPDATE_RETRY_BASE_DELAY = 0.05
DEFAULT_TORRENT_UPDATE_RETRY_MAX_DELAY = 1.0
RETRYABLE_DB_SQLSTATES = frozenset({"40001", "40P01", "55P03"})
RETRYABLE_DB_ERROR_MARKERS = (
    "database is locked",
    "database is busy",
    "deadlock detected",
    "deadlock",
    "lock wait timeout",
    "could not serialize access",
    "serialization failure",
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


def _get_cached_normalized_sources(
    sources,
    cache: dict[int, list[str]] | None = None,
) -> list[str]:
    if cache is None:
        return _normalize_sources(sources)

    value_id = id(sources)
    cached = cache.get(value_id)
    if cached is None:
        cached = _normalize_sources(sources)
        cache[value_id] = cached
    return cached


def _get_cached_parsed_payload(
    parsed,
    cache: dict[int, dict] | None = None,
) -> dict:
    if cache is None:
        return _coerce_parsed_payload(parsed)

    value_id = id(parsed)
    cached = cache.get(value_id)
    if cached is None:
        cached = _coerce_parsed_payload(parsed)
        cache[value_id] = cached
    return cached


def _prune_ordered_dict(cache: OrderedDict, *, max_entries: int):
    while len(cache) > max_entries:
        cache.popitem(last=False)


def _is_relevant_video_file(title: str) -> bool:
    return bool(title) and is_video(title) and "sample" not in title.lower()


def _normalize_valid_info_hash(info_hash) -> str | None:
    if not isinstance(info_hash, str):
        return None

    normalized_info_hash = normalize_info_hash(info_hash).lower()
    if len(normalized_info_hash) != 40:
        return None
    return normalized_info_hash


def _parse_video_title(title: str):
    try:
        parsed = parse(title)
        ensure_multi_language(parsed)
    except Exception:
        return None
    return parsed


@lru_cache(maxsize=4096)
def _parse_video_title_payload(title: str):
    parsed = _parse_video_title(title)
    if parsed is None:
        return None

    return (
        tuple(getattr(parsed, "seasons", None) or ()),
        tuple(getattr(parsed, "episodes", None) or ()),
        _coerce_parsed_payload(parsed),
    )


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
        try:
            info_hash = normalize_info_hash(info_hash)
        except (TypeError, ValueError):
            return None
        if len(info_hash) != 40:
            return None
    return info_hash.lower()


def _extract_relevant_file_entries(file_specs) -> list[dict]:
    files = []
    for index, title, size in file_specs:
        if _is_relevant_video_file(title):
            files.append(
                {
                    "index": index,
                    "title": title,
                    "size": size,
                }
            )
    return files


def _build_torrent_metadata_payload(info_hash: str, sources, file_specs) -> dict:
    return {
        "info_hash": info_hash,
        "sources": _normalize_sources(sources),
        "files": _extract_relevant_file_entries(file_specs),
    }


def _construct_torrent_metadata(
    *,
    info_hash: str,
    title: str,
    size: int,
    seeders: int | None,
    tracker: str,
    imdb_id: str | None,
    file_index: int | None,
    season: int | None,
    episode: int | None,
    sources: list[str],
    parsed: dict,
    updated_at: float,
) -> TorrentMetadata:
    return TorrentMetadata.model_construct(
        info_hash=info_hash,
        title=title,
        size=size,
        seeders=seeders,
        tracker=tracker,
        imdb_id=imdb_id,
        file_index=file_index,
        season=season,
        episode=episode,
        sources=sources,
        parsed=parsed,
        updated_at=updated_at,
        contributor_id="",
        contributor_public_key="",
        contributor_signature="",
        pool_id=None,
    )


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
        logger.debug(
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
        logger.debug(f"Failed to get torrent from magnet: {e}")
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

    return _build_torrent_metadata_payload(
        info_hash,
        trackers,
        (
            (index, Path(torrent_file).name, torrent_file.size)
            for index, torrent_file in enumerate(torrent.files)
        ),
    )


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
        files = info[b"files"] if b"files" in info else [info]
        return _build_torrent_metadata_payload(
            info_hash,
            announce_list,
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
        )
    except Exception as e:
        logger.debug(f"Failed to extract torrent metadata: {e}")
        return {}


def _is_empty_merge_value(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value == ""
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return False


def _merge_parsed_payloads(existing: dict, incoming: dict) -> dict:
    if not existing:
        return incoming
    if not incoming:
        return existing

    merged = dict(existing)
    for key, incoming_value in incoming.items():
        existing_value = merged.get(key)
        if key not in merged or _is_empty_merge_value(existing_value):
            merged[key] = incoming_value
            continue
        if _is_empty_merge_value(incoming_value):
            continue
        if isinstance(existing_value, dict) and isinstance(incoming_value, dict):
            merged[key] = _merge_parsed_payloads(existing_value, incoming_value)
            continue
        if isinstance(existing_value, list) and isinstance(incoming_value, list):
            if len(incoming_value) > len(existing_value):
                merged[key] = incoming_value
            continue
        if isinstance(existing_value, str) and isinstance(incoming_value, str):
            if len(incoming_value) > len(existing_value):
                merged[key] = incoming_value
            continue
        merged[key] = incoming_value
    return merged


def _merge_torrent_updates(
    existing: "_TorrentUpdate", incoming: "_TorrentUpdate"
) -> "_TorrentUpdate":
    if incoming.file_index is not None:
        existing.file_index = incoming.file_index
    if incoming.title:
        existing.title = incoming.title
    if incoming.seeders is not None:
        existing.seeders = incoming.seeders
    if incoming.size is not None:
        existing.size = incoming.size
    if incoming.tracker:
        existing.tracker = incoming.tracker
    if incoming.sources:
        existing.sources = _dedupe_strings([*existing.sources, *incoming.sources])
    existing.parsed = _merge_parsed_payloads(existing.parsed, incoming.parsed)
    existing.from_cometnet = existing.from_cometnet and incoming.from_cometnet
    if incoming.attempts > existing.attempts:
        existing.attempts = incoming.attempts
    return existing


def _construct_torrent_update(
    *,
    media_id: str,
    info_hash: str,
    season: int | None,
    episode: int | None,
    file_index: int | None,
    title: str,
    seeders: int | None,
    size: int | None,
    tracker: str | None,
    sources: list[str],
    parsed: dict,
    from_cometnet: bool,
    attempts: int = 0,
) -> "_TorrentUpdate":
    season_norm = normalize_scope_value(season)
    episode_norm = normalize_scope_value(episode)
    item = object.__new__(_TorrentUpdate)
    item.media_id = media_id
    item.info_hash = info_hash
    item.season = season
    item.episode = episode
    item.file_index = file_index
    item.title = title
    item.seeders = seeders
    item.size = size
    item.tracker = tracker
    item.sources = sources
    item.parsed = parsed
    item.from_cometnet = from_cometnet
    item.attempts = attempts
    item.season_norm = season_norm
    item.episode_norm = episode_norm
    item.row_key = (media_id, info_hash, season_norm, episode_norm)
    return item


def _build_torrent_update_from_source(
    *,
    media_id: str,
    source: dict,
    from_cometnet: bool,
    sources_cache: dict[int, list[str]] | None = None,
    parsed_cache: dict[int, dict] | None = None,
) -> "_TorrentUpdate | None":
    info_hash = _normalize_valid_info_hash(source.get("info_hash"))
    title = source.get("title")
    if info_hash is None or not isinstance(title, str) or not title:
        return None

    file_index = source.get("index")
    if file_index is None:
        file_index = source.get("file_index")

    return _construct_torrent_update(
        media_id=media_id,
        info_hash=info_hash,
        season=source.get("season"),
        episode=source.get("episode"),
        file_index=file_index,
        title=title,
        seeders=source.get("seeders"),
        size=source.get("size"),
        tracker=source.get("tracker"),
        sources=_get_cached_normalized_sources(
            source.get("sources"),
            sources_cache,
        ),
        parsed=_get_cached_parsed_payload(
            source.get("parsed"),
            parsed_cache,
        ),
        from_cometnet=from_cometnet,
    )


def _build_torrent_update_from_metadata(
    metadata: TorrentMetadata,
) -> "_TorrentUpdate | None":
    info_hash = metadata.info_hash.lower()
    if len(info_hash) != 40 or not metadata.title:
        return None

    return _construct_torrent_update(
        media_id=metadata.imdb_id,
        info_hash=info_hash,
        season=metadata.season,
        episode=metadata.episode,
        file_index=metadata.file_index,
        title=metadata.title,
        seeders=metadata.seeders,
        size=metadata.size,
        tracker=metadata.tracker,
        sources=_dedupe_strings(metadata.sources),
        parsed=metadata.parsed or {},
        from_cometnet=True,
    )


def _iter_torrent_updates_from_file_infos(
    file_infos: Iterable[dict], *, media_id: str, from_cometnet: bool
) -> Iterator["_TorrentUpdate"]:
    sources_cache = {}
    parsed_cache = {}
    for file_info in file_infos:
        item = _build_torrent_update_from_source(
            media_id=media_id,
            source=file_info,
            from_cometnet=from_cometnet,
            sources_cache=sources_cache,
            parsed_cache=parsed_cache,
        )
        if item is not None:
            yield item


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
    row_key: tuple[str, str, int, int] = field(init=False)

    def to_broadcast_payload(self, updated_at: float) -> dict:
        return {
            "info_hash": self.info_hash,
            "title": self.title,
            "size": int(self.size or 0),
            "tracker": self.tracker or "",
            "imdb_id": self.media_id,
            "file_index": self.file_index,
            "seeders": self.seeders,
            "season": self.season,
            "episode": self.episode,
            "sources": self.sources,
            "parsed": self.parsed,
            "updated_at": updated_at,
        }

    def to_broadcast_metadata(self, updated_at: float) -> TorrentMetadata:
        return _construct_torrent_metadata(
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


def _iter_resolved_torrent_updates(
    resolved_torrent: dict,
    *,
    media_id: str,
    seeders: int,
    tracker: str,
    search_season: int | None,
) -> Iterator[_TorrentUpdate]:
    normalized_info_hash = _normalize_valid_info_hash(resolved_torrent.get("info_hash"))
    if normalized_info_hash is None:
        return

    normalized_sources = _normalize_sources(resolved_torrent.get("sources"))
    for resolved_file in resolved_torrent.get("files", []):
        if not isinstance(resolved_file, dict):
            continue

        title = resolved_file.get("title")
        if not isinstance(title, str) or not title:
            continue

        parsed_title = _parse_video_title_payload(title)
        if parsed_title is None:
            continue

        parsed_seasons, parsed_episodes, parsed_payload = parsed_title
        seasons = parsed_seasons or (
            (search_season,) if search_season is not None else (None,)
        )
        episode_candidates = parsed_episodes or (None,)
        episode = episode_candidates[0] if len(episode_candidates) == 1 else None
        for season in seasons:
            yield _construct_torrent_update(
                media_id=media_id,
                info_hash=normalized_info_hash,
                season=season,
                episode=episode,
                file_index=resolved_file.get("index"),
                title=title,
                seeders=seeders,
                size=resolved_file.get("size"),
                tracker=tracker,
                sources=normalized_sources,
                parsed=parsed_payload,
                from_cometnet=False,
            )


async def _collect_queue_batch(
    queue: asyncio.Queue,
    first_item,
    *,
    max_items: int,
    flush_interval: float,
) -> list:
    batch = [first_item]

    def drain_nowait():
        while len(batch) < max_items:
            try:
                batch.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                return

    drain_nowait()
    if len(batch) >= max_items or flush_interval <= 0:
        return batch

    deadline = time.monotonic() + flush_interval
    while len(batch) < max_items:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break

        try:
            batch.append(await asyncio.wait_for(queue.get(), timeout=remaining))
        except asyncio.TimeoutError:
            break
        drain_nowait()

    return batch


def _compute_batched_queue_maxsize(
    batch_size: int,
    *,
    total_slots: int,
    min_batches: int,
) -> int:
    return max(min_batches, total_slots // max(1, min(batch_size, 128)))


@lru_cache(maxsize=32)
def _build_check_torrents_exist_query(hash_count: int) -> str:
    placeholders = ", ".join(f":info_hash_{index}" for index in range(hash_count))
    return f"""
SELECT info_hash
FROM torrents
WHERE info_hash IN ({placeholders})
"""


@lru_cache(maxsize=4096)
def _build_check_torrents_exist_param_key(index: int) -> str:
    return f"info_hash_{index}"


def _build_check_torrents_exist_params(info_hashes: list[str]) -> dict:
    return {
        _build_check_torrents_exist_param_key(index): info_hash
        for index, info_hash in enumerate(info_hashes)
    }


def _normalize_unique_info_hashes(info_hashes: Iterable[str]) -> tuple[str, ...]:
    unique_hashes = []
    seen_hashes = set()
    for info_hash in info_hashes:
        normalized_info_hash = _normalize_valid_info_hash(info_hash)
        if normalized_info_hash is None or normalized_info_hash in seen_hashes:
            continue
        seen_hashes.add(normalized_info_hash)
        unique_hashes.append(normalized_info_hash)
    return tuple(unique_hashes)


async def check_torrents_exist(info_hashes: list[str]) -> set[str]:
    if not info_hashes:
        return set()

    unique_hashes = _normalize_unique_info_hashes(info_hashes)
    if not unique_hashes:
        return set()

    chunk_size = SQLITE_MAX_VARIABLES if IS_SQLITE else POSTGRES_MAX_PARAMETERS
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
        self._queue_waiters = 0
        self._queue_waiters_event = asyncio.Event()
        self._queue_waiters_event.set()
        self._resolved_torrent_cache = OrderedDict()
        self._inflight_resolutions = {}

    def _reset_runtime_state(self):
        self._workers = []
        self._pending_torrents.clear()
        self._queue_waiters = 0
        self._queue_waiters_event.set()
        self._inflight_resolutions.clear()
        self._resolved_torrent_cache.clear()

    async def add_torrent(
        self,
        magnet_url: str,
        seeders: int,
        tracker: str,
        media_id: str,
        search_season: int | None,
        magnet_key: str | None = None,
    ):
        if not settings.DOWNLOAD_TORRENT_FILES or self._stopping:
            return

        magnet_key = magnet_key or _extract_info_hash_from_magnet(magnet_url)
        if not magnet_key:
            return

        queue_key = (media_id, magnet_key, search_season)

        async with self._lock:
            if queue_key in self._pending_torrents:
                return
            self._pending_torrents.add(queue_key)

        await self._ensure_workers()

        payload = (
            queue_key,
            magnet_url,
            seeders,
            tracker,
        )
        try:
            self.queue.put_nowait(payload)
        except asyncio.QueueFull:
            self._queue_waiters += 1
            self._queue_waiters_event.clear()
            try:
                await self.queue.put(payload)
            except asyncio.CancelledError:
                async with self._lock:
                    self._pending_torrents.discard(queue_key)
                raise
            finally:
                self._queue_waiters -= 1
                if self._queue_waiters == 0:
                    self._queue_waiters_event.set()

    async def _ensure_workers(self):
        workers = self._workers
        if (
            workers
            and len(workers) >= self.max_concurrent
            and all(not task.done() for task in workers)
        ):
            return

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
        inflight_key = (magnet_key, magnet_url)
        async with self._lock:
            cached = self._resolved_torrent_cache.get(magnet_key)
            if cached is not None:
                self._resolved_torrent_cache.move_to_end(magnet_key)
                return cached

            future = self._inflight_resolutions.get(inflight_key)
            if future is None:
                future = loop.create_future()
                self._inflight_resolutions[inflight_key] = future
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
                if resolved_torrent:
                    self._resolved_torrent_cache[magnet_key] = resolved_torrent
                    self._resolved_torrent_cache.move_to_end(magnet_key)
                    _prune_ordered_dict(
                        self._resolved_torrent_cache,
                        max_entries=self.metadata_cache_max_entries,
                    )

                inflight = self._inflight_resolutions.pop(inflight_key, None)
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

        while True:
            while self._queue_waiters > 0:
                await self._queue_waiters_event.wait()
            await self.queue.join()
            if self._queue_waiters == 0:
                break
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
        batch_size: int = DEFAULT_TORRENT_UPDATE_BATCH_SIZE,
        flush_interval: float = DEFAULT_TORRENT_UPDATE_FLUSH_INTERVAL,
    ):
        self.batch_size = max(1, batch_size)
        self.queue = asyncio.Queue(maxsize=DEFAULT_TORRENT_UPDATE_QUEUE_MAXSIZE)
        self._broadcast_queue = asyncio.Queue(
            maxsize=_compute_batched_queue_maxsize(
                self.batch_size,
                total_slots=DEFAULT_TORRENT_BROADCAST_QUEUE_MAXSIZE,
                min_batches=DEFAULT_TORRENT_BROADCAST_BATCH_QUEUE_MIN_SIZE,
            )
        )
        self.flush_interval = max(0.0, flush_interval)
        self.max_retries = DEFAULT_TORRENT_UPDATE_MAX_RETRIES
        self.broadcast_max_retries = DEFAULT_TORRENT_BROADCAST_MAX_RETRIES
        self._state_lock = asyncio.Lock()
        self._pending_lock = asyncio.Lock()
        self._retry_lock = asyncio.Lock()
        self._worker = None
        self._broadcast_worker = None
        self._retry_worker = None
        self._stopping = False
        self._dropped_updates = 0
        self._dropped_requeues = 0
        self._dropped_broadcasts = 0
        self._pending_updates = {}
        self._queue_waiters = 0
        self._queue_waiters_event = asyncio.Event()
        self._queue_waiters_event.set()
        self._broadcast_waiters = 0
        self._broadcast_waiters_event = asyncio.Event()
        self._broadcast_waiters_event.set()
        self._retry_heap = []
        self._retry_event = asyncio.Event()
        self._retry_sequence = 0

    def _can_accept_media_id(self, media_id: str | None) -> bool:
        return not self._stopping and isinstance(media_id, str) and bool(media_id)

    async def add_torrent_info(
        self, file_info: dict, media_id: str | None = None, from_cometnet: bool = False
    ):
        if not self._can_accept_media_id(media_id):
            return

        await self._enqueue_prepared_item(
            _build_torrent_update_from_source(
                media_id=media_id,
                source=file_info,
                from_cometnet=from_cometnet,
            )
        )

    async def add_torrent_infos(
        self,
        file_infos: list[dict],
        media_id: str | None = None,
        from_cometnet: bool = False,
    ):
        if not file_infos or not self._can_accept_media_id(media_id):
            return

        await self._enqueue_prepared_items(
            _iter_torrent_updates_from_file_infos(
                file_infos,
                media_id=media_id,
                from_cometnet=from_cometnet,
            )
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
        if not self._can_accept_media_id(media_id):
            return

        await self._enqueue_prepared_items(
            _iter_resolved_torrent_updates(
                resolved_torrent,
                media_id=media_id,
                seeders=seeders,
                tracker=tracker,
                search_season=search_season,
            )
        )

    async def add_network_torrent(self, metadata: TorrentMetadata):
        if not self._can_accept_media_id(metadata.imdb_id):
            return

        await self._enqueue_prepared_item(_build_torrent_update_from_metadata(metadata))

    async def _ensure_task(self, attr_name: str, worker_factory):
        worker = getattr(self, attr_name)
        if worker is not None and not worker.done():
            return

        async with self._state_lock:
            worker = getattr(self, attr_name)
            if worker is not None and not worker.done():
                return
            setattr(self, attr_name, asyncio.create_task(worker_factory()))

    async def _enqueue_prepared_item(self, item: _TorrentUpdate | None):
        if self._stopping or item is None:
            return

        await self._ensure_task("_worker", self._process_queue)
        await self._queue_items((item,))

    async def _enqueue_prepared_items(self, items: Iterable[_TorrentUpdate]):
        if self._stopping:
            return

        iterator = iter(items)
        first_item = next(iterator, None)
        if first_item is None:
            return

        await self._ensure_task("_worker", self._process_queue)
        await self._queue_items(chain((first_item,), iterator))

    async def _stage_pending_items(
        self, items: Iterable[_TorrentUpdate]
    ) -> list[_TorrentUpdate]:
        slow_items = []
        queue_put_nowait = self.queue.put_nowait
        pending_updates = self._pending_updates
        queue_full = False

        async with self._pending_lock:
            for item in items:
                existing = pending_updates.get(item.row_key)
                if existing is not None:
                    _merge_torrent_updates(existing, item)
                    continue

                pending_updates[item.row_key] = item
                if queue_full:
                    slow_items.append(item)
                    continue

                try:
                    queue_put_nowait(item)
                except asyncio.QueueFull:
                    queue_full = True
                    slow_items.append(item)

            if slow_items:
                self._queue_waiters += 1
                self._queue_waiters_event.clear()

        return slow_items

    async def _discard_pending_item(self, item: _TorrentUpdate):
        async with self._pending_lock:
            if self._pending_updates.get(item.row_key) is item:
                self._pending_updates.pop(item.row_key, None)

    async def _queue_items(self, items: Iterable[_TorrentUpdate]):
        if self._stopping:
            return

        slow_items = await self._stage_pending_items(items)
        if not slow_items:
            return

        queued_count = 0
        try:
            for item in slow_items:
                await self.queue.put(item)
                queued_count += 1
        except asyncio.CancelledError:
            for item in slow_items[queued_count:]:
                await self._discard_pending_item(item)
            raise
        finally:
            self._queue_waiters -= 1
            if self._queue_waiters == 0:
                self._queue_waiters_event.set()

    async def _enqueue_broadcast_items(
        self, batch_items: list[_TorrentUpdate], updated_at: float
    ):
        backend = get_active_backend()
        if backend is None:
            return

        if isinstance(backend, CometNetService):
            metadata_batch = [
                item.to_broadcast_metadata(updated_at)
                for item in batch_items
                if not item.from_cometnet
            ]
        else:
            metadata_batch = [
                item.to_broadcast_payload(updated_at)
                for item in batch_items
                if not item.from_cometnet
            ]
        if not metadata_batch:
            return

        await self._ensure_task("_broadcast_worker", self._process_broadcast_queue)

        try:
            self._broadcast_queue.put_nowait((backend, metadata_batch))
        except asyncio.QueueFull:
            self._broadcast_waiters += 1
            self._broadcast_waiters_event.clear()
            try:
                await self._broadcast_queue.put((backend, metadata_batch))
            finally:
                self._broadcast_waiters -= 1
                if self._broadcast_waiters == 0:
                    self._broadcast_waiters_event.set()

    def _retry_delay_seconds(self, attempt: int) -> float:
        delay = min(
            DEFAULT_TORRENT_UPDATE_RETRY_MAX_DELAY,
            DEFAULT_TORRENT_UPDATE_RETRY_BASE_DELAY * (2 ** max(0, attempt - 1)),
        )
        return delay + (random.random() * min(0.05, delay))

    async def _schedule_requeue_batch(
        self, batch_items: list[_TorrentUpdate], delay: float
    ):
        ready_at = time.monotonic() + delay
        async with self._retry_lock:
            heapq.heappush(
                self._retry_heap,
                (ready_at, self._retry_sequence, batch_items),
            )
            self._retry_sequence += 1
            self._retry_event.set()

        await self._ensure_task("_retry_worker", self._process_retry_queue)

    async def _requeue_batch_items(self, batch_items: list[_TorrentUpdate]):
        if self._stopping:
            return

        requeue_candidates = []
        for item in batch_items:
            if self.max_retries is not None and item.attempts >= self.max_retries:
                self._dropped_requeues += 1
                continue
            item.attempts += 1
            requeue_candidates.append(item)

        if not requeue_candidates:
            return

        await self._schedule_requeue_batch(
            requeue_candidates,
            self._retry_delay_seconds(
                max(item.attempts for item in requeue_candidates)
            ),
        )

    async def _process_retry_queue(self):
        try:
            while True:
                batch_items = None
                wait_timeout = None
                async with self._retry_lock:
                    if self._retry_heap:
                        ready_at, _, _ = self._retry_heap[0]
                        wait_timeout = ready_at - time.monotonic()
                        if wait_timeout <= 0:
                            _, _, batch_items = heapq.heappop(self._retry_heap)
                        else:
                            self._retry_event.clear()
                    else:
                        self._retry_event.clear()

                if batch_items is not None:
                    if not self._stopping:
                        await self._queue_items(batch_items)
                    continue

                if wait_timeout is None:
                    await self._retry_event.wait()
                    continue

                try:
                    await asyncio.wait_for(
                        self._retry_event.wait(), timeout=wait_timeout
                    )
                except asyncio.TimeoutError:
                    pass
        finally:
            async with self._state_lock:
                if self._retry_worker is asyncio.current_task():
                    self._retry_worker = None

    async def _finalize_batch_items(self, batch_items: list[_TorrentUpdate]):
        async with self._pending_lock:
            ready_items = []
            for item in batch_items:
                if self._pending_updates.get(item.row_key) is not item:
                    continue
                self._pending_updates.pop(item.row_key, None)
                ready_items.append(item)
            return ready_items

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
                batch_items = await self._finalize_batch_items(batch_keys)

                updated_at = 0.0
                try:
                    if batch_items:
                        updated_at = time.time()
                        await _execute_batched_upsert(
                            batch_items, updated_at=updated_at
                        )
                except Exception as e:
                    logger.warning(f"Error in torrent update batch: {e}")
                    if _is_retryable_db_error(e):
                        await self._requeue_batch_items(batch_items)
                else:
                    if batch_items:
                        await self._enqueue_broadcast_items(batch_items, updated_at)
                finally:
                    for _ in batch_keys:
                        self.queue.task_done()
        finally:
            async with self._state_lock:
                if self._worker is asyncio.current_task():
                    self._worker = None

    async def _process_broadcast_queue(self):
        try:
            while True:
                payload = await self._broadcast_queue.get()
                if payload is self._STOP:
                    self._broadcast_queue.task_done()
                    return

                backend, metadata_batch = payload
                max_attempts = max(1, self.broadcast_max_retries)
                try:
                    for attempt in range(1, max_attempts + 1):
                        try:
                            await backend.broadcast_torrents(metadata_batch)
                            break
                        except Exception as e:
                            stopping = self._stopping
                            final_attempt = stopping or attempt >= max_attempts
                            if attempt == 1 or final_attempt or (attempt % 10) == 0:
                                logger.warning(
                                    "Error while broadcasting torrents "
                                    f"(attempt {attempt}/{max_attempts}, "
                                    f"batch={len(metadata_batch)}): {e}"
                                )
                            if final_attempt:
                                self._dropped_broadcasts += len(metadata_batch)
                                logger.warning(
                                    "Dropping torrent broadcast batch "
                                    f"(attempts={attempt}, batch={len(metadata_batch)}, "
                                    f"stopping={stopping})"
                                )
                                break
                            await asyncio.sleep(self._retry_delay_seconds(attempt))
                finally:
                    self._broadcast_queue.task_done()
        finally:
            async with self._state_lock:
                if self._broadcast_worker is asyncio.current_task():
                    self._broadcast_worker = None

    async def stop(self):
        async with self._state_lock:
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
            retry_worker = (
                self._retry_worker
                if self._retry_worker is not None and not self._retry_worker.done()
                else None
            )

        if worker is not None:
            while True:
                while self._queue_waiters > 0:
                    await self._queue_waiters_event.wait()
                await self.queue.join()
                if self._queue_waiters == 0:
                    break
            await self.queue.put(self._STOP)
            await asyncio.gather(worker, return_exceptions=True)

        if broadcast_worker is not None:
            while True:
                while self._broadcast_waiters > 0:
                    await self._broadcast_waiters_event.wait()
                await self._broadcast_queue.join()
                if self._broadcast_waiters == 0:
                    break
            await self._broadcast_queue.put(self._STOP)
            await asyncio.gather(broadcast_worker, return_exceptions=True)

        if retry_worker is not None:
            retry_worker.cancel()
            await asyncio.gather(retry_worker, return_exceptions=True)

        async with self._retry_lock:
            discarded_retry_count = len(self._retry_heap)
            if discarded_retry_count:
                logger.debug(
                    "Discarding pending torrent retry entries during shutdown "
                    f"(count={discarded_retry_count})"
                )
            self._retry_heap.clear()
            self._retry_event.clear()

        async with self._pending_lock:
            self._pending_updates.clear()
        async with self._state_lock:
            self._queue_waiters = 0
            self._queue_waiters_event.set()
            self._broadcast_waiters = 0
            self._broadcast_waiters_event.set()
            self._retry_sequence = 0


TORRENT_COLUMNS_SQL = ",\n    ".join(TORRENT_DB_COLUMNS)
TORRENT_UPDATE_SET_SQL = ",\n        ".join(
    f"{column} = EXCLUDED.{column}" for column in TORRENT_UPDATE_COLUMNS
)
SQLITE_DISTINCT_UPDATE_WHERE_SQL = " OR ".join(
    f"torrents.{column} IS NOT excluded.{column}"
    for column in TORRENT_CHANGE_DETECTION_COLUMNS
)
POSTGRES_DISTINCT_UPDATE_WHERE_SQL = (
    "ROW("
    + ", ".join(f"torrents.{column}" for column in TORRENT_CHANGE_DETECTION_COLUMNS)
    + ") IS DISTINCT FROM ROW("
    + ", ".join(f"EXCLUDED.{column}" for column in TORRENT_CHANGE_DETECTION_COLUMNS)
    + ")"
)
TORRENT_REFRESH_UPDATE_WHERE_SQL = "COALESCE(torrents.updated_at, 0) < :refresh_before"


def estimate_upsert_row_count(params: dict) -> int:
    return max(
        0,
        (len(params) - TORRENT_UPSERT_SHARED_PARAM_COUNT)
        // TORRENT_UPSERT_ROW_PARAM_COUNT,
    )


def _build_batched_upsert_query(
    row_count: int,
    *,
    distinct_where_sql: str,
) -> str:
    values_sql = ",\n".join(
        "(\n    "
        + ",\n    ".join(
            (
                f":media_id_{index}",
                f":info_hash_{index}",
                f":season_{index}",
                f":episode_{index}",
                f"COALESCE(:season_{index}, {NULL_SCOPE_SENTINEL})",
                f"COALESCE(:episode_{index}, {NULL_SCOPE_SENTINEL})",
                f":file_index_{index}",
                f":title_{index}",
                f":seeders_{index}",
                f":size_{index}",
                f":tracker_{index}",
                f":sources_json_{index}",
                f":parsed_json_{index}",
                ":updated_at",
            )
        )
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
    ({distinct_where_sql})
    OR ({TORRENT_REFRESH_UPDATE_WHERE_SQL})
"""


@lru_cache(maxsize=64)
def _build_sqlite_batched_upsert_query(row_count: int) -> str:
    return _build_batched_upsert_query(
        row_count,
        distinct_where_sql=SQLITE_DISTINCT_UPDATE_WHERE_SQL,
    )


@lru_cache(maxsize=64)
def _build_postgres_batched_upsert_query(row_count: int) -> str:
    return _build_batched_upsert_query(
        row_count,
        distinct_where_sql=POSTGRES_DISTINCT_UPDATE_WHERE_SQL,
    )


@lru_cache(maxsize=8192)
def _build_batched_param_keys(index: int) -> tuple[str, ...]:
    return tuple(f"{column}_{index}" for column in TORRENT_UPSERT_PARAM_COLUMNS)


def _get_cached_json_dump(value, cache: dict[int, str]) -> str:
    value_id = id(value)
    cached = cache.get(value_id)
    if cached is None:
        cached = _json_dumps(value)
        cache[value_id] = cached
    return cached


def _build_batched_params(
    rows: list[_TorrentUpdate],
    *,
    updated_at: float,
    sources_json_cache: dict[int, str] | None = None,
    parsed_json_cache: dict[int, str] | None = None,
) -> dict:
    params = {
        "updated_at": updated_at,
        "refresh_before": updated_at - TORRENT_STABLE_REFRESH_INTERVAL,
    }
    if sources_json_cache is None:
        sources_json_cache = {}
    if parsed_json_cache is None:
        parsed_json_cache = {}
    for index, item in enumerate(rows):
        param_keys = _build_batched_param_keys(index)
        (
            media_id_key,
            info_hash_key,
            season_key,
            episode_key,
            file_index_key,
            title_key,
            seeders_key,
            size_key,
            tracker_key,
            sources_json_key,
            parsed_json_key,
        ) = param_keys
        params[media_id_key] = item.media_id
        params[info_hash_key] = item.info_hash
        params[season_key] = item.season
        params[episode_key] = item.episode
        params[file_index_key] = item.file_index
        params[title_key] = item.title
        params[seeders_key] = item.seeders
        params[size_key] = item.size
        params[tracker_key] = item.tracker
        params[sources_json_key] = _get_cached_json_dump(
            item.sources, sources_json_cache
        )
        params[parsed_json_key] = _get_cached_json_dump(item.parsed, parsed_json_cache)
    return params


def _is_retryable_db_error(exc: Exception) -> bool:
    seen = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))

        sqlstate = getattr(current, "sqlstate", None) or getattr(
            current, "pgcode", None
        )
        if sqlstate in RETRYABLE_DB_SQLSTATES:
            return True

        error_message = str(current).lower()
        if any(marker in error_message for marker in RETRYABLE_DB_ERROR_MARKERS):
            return True

        current = getattr(current, "__cause__", None) or getattr(
            current, "__context__", None
        )

    return False


async def _execute_batched_upsert(rows: list[_TorrentUpdate], *, updated_at: float):
    if not rows:
        return

    max_rows_per_statement = (
        SQLITE_UPSERT_MAX_ROWS_PER_STATEMENT
        if IS_SQLITE
        else POSTGRES_UPSERT_MAX_ROWS_PER_STATEMENT
    )
    query_builder = (
        _build_sqlite_batched_upsert_query
        if IS_SQLITE
        else _build_postgres_batched_upsert_query
    )
    sources_json_cache = {}
    parsed_json_cache = {}

    async with database.transaction():
        for start in range(0, len(rows), max_rows_per_statement):
            chunk = rows[start : start + max_rows_per_statement]
            await database.execute(
                query_builder(len(chunk)),
                _build_batched_params(
                    chunk,
                    updated_at=updated_at,
                    sources_json_cache=sources_json_cache,
                    parsed_json_cache=parsed_json_cache,
                ),
            )


torrent_update_queue = TorrentUpdateQueue()
