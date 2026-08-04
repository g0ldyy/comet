import asyncio
import hashlib
import heapq
import random
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass, field
from functools import lru_cache
from itertools import chain
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

import aiohttp
import anyio
import bencodepy
from bencodepy import BencodeDecodeError
from demagnetize.core import Demagnetizer
from demagnetize.errors import DemagnetizeError
from pydantic import ValidationError
from RTN import parse
from torf import Magnet, MagnetError

from comet.cometnet import get_active_backend
from comet.cometnet.protocol import TorrentMetadata
from comet.core.constants import torrent_timeout
from comet.core.database import (
    encode_json_param,
    is_retryable_database_error,
    normalize_scope_value,
)
from comet.core.models import database, settings
from comet.core.provider_json import is_success_status
from comet.discovery.torrent_repository import TorrentReleaseRepository
from comet.observability.context import create_detached_task
from comet.usenet.outbound import (
    OutboundUrlError,
    PinnedResolver,
    validate_http_url,
)
from comet.utils.formatting import normalize_info_hash
from comet.utils.http_client import read_bounded_body
from comet.utils.parsing import default_dump, ensure_multi_language, is_video

TRACKER_PATTERN = re.compile(r"[&?]tr=([^&]+)")
DEFAULT_ADD_TORRENT_QUEUE_MAXSIZE = 256
DEFAULT_ADD_TORRENT_METADATA_CACHE_MAX_ENTRIES = 512
DEFAULT_TORRENT_UPDATE_BATCH_SIZE = 1000
DEFAULT_TORRENT_UPDATE_QUEUE_MAXSIZE = 8192
DEFAULT_TORRENT_UPDATE_MAX_RETRIES = None
DEFAULT_TORRENT_UPDATE_FLUSH_INTERVAL = 0.1
DEFAULT_TORRENT_BROADCAST_QUEUE_MAXSIZE = 4096
DEFAULT_TORRENT_BROADCAST_BATCH_QUEUE_MIN_SIZE = 32
DEFAULT_TORRENT_UPDATE_RETRY_BASE_DELAY = 0.05
DEFAULT_TORRENT_UPDATE_RETRY_MAX_DELAY = 1.0
MAX_TORRENT_DOCUMENT_BYTES = 16 * 1024 * 1024
MAX_TORRENT_REDIRECTS = 3


def _coerce_parsed_payload(parsed) -> dict:
    if isinstance(parsed, dict):
        return parsed
    if parsed is None:
        return {}
    dumped = default_dump(parsed)
    return {} if dumped is None else dumped


def _dedupe_strings(values: list[str]) -> list[str]:
    if len(values) <= 1:
        return values
    return list(dict.fromkeys(values))


def _normalize_sources(sources) -> list[str]:
    return [] if sources is None else _dedupe_strings(list(sources))


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


def _normalize_valid_info_hash(info_hash) -> str | None:
    if not isinstance(info_hash, str):
        return None

    normalized_info_hash = normalize_info_hash(info_hash)

    if re.fullmatch(r"[0-9a-f]{40}", normalized_info_hash) is None:
        return None
    return normalized_info_hash


def _parse_video_title(title: str):
    parsed = parse(title)
    ensure_multi_language(parsed)
    return parsed


@lru_cache(maxsize=4096)
def _parse_video_title_payload(title: str):
    parsed = _parse_video_title(title)
    return (
        tuple(parsed.seasons or ()),
        tuple(parsed.episodes or ()),
        _coerce_parsed_payload(parsed),
    )


def extract_trackers_from_magnet(magnet_uri: str):
    return _dedupe_strings(
        [unquote(tracker) for tracker in TRACKER_PATTERN.findall(magnet_uri)]
    )


def _extract_info_hash_from_magnet(magnet_uri: str) -> str | None:
    if (
        not isinstance(magnet_uri, str)
        or len(magnet_uri) > 16 * 1024
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in magnet_uri
        )
    ):
        return None
    try:
        parsed = urlsplit(magnet_uri)
        if parsed.scheme.lower() != "magnet":
            return None
        query = parse_qs(
            parsed.query,
            keep_blank_values=False,
            max_num_fields=128,
        )
    except ValueError:
        return None
    hashes = {
        normalized
        for value in query.get("xt", ())
        if value.lower().startswith("urn:btih:")
        and (normalized := _normalize_valid_info_hash(value[len("urn:btih:") :]))
        is not None
    }
    return next(iter(hashes)) if len(hashes) == 1 else None


def _extract_relevant_file_entries(file_specs) -> list[dict]:
    files = []
    for index, title, size in file_specs:
        if is_video(title):
            files.append(
                {
                    "index": index,
                    "title": title,
                    "size": size,
                }
            )
    return files


def _iter_bencoded_file_specs(info: dict) -> Iterator[tuple[int, str, int]]:
    files = info.get(b"files")
    file_entries = files if isinstance(files, list) else (info,)

    for index, file_info in enumerate(file_entries):
        if not isinstance(file_info, dict):
            continue

        path = file_info.get(b"path")
        if isinstance(path, (list, tuple)) and path:
            encoded_title = path[-1]
        else:
            encoded_title = file_info.get(b"name")
        size = file_info.get(b"length")
        if not isinstance(encoded_title, bytes) or not isinstance(size, int):
            continue

        try:
            title = encoded_title.decode()
        except UnicodeDecodeError:
            continue
        yield index, title, size


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
    imdb_id: str,
    file_index: int | None,
    season: int | None,
    episode: int | None,
    sources: list[str],
    parsed: dict,
    updated_at: float,
) -> TorrentMetadata:
    return TorrentMetadata(
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


async def download_torrent(
    session,
    url: str,
    *,
    allowed_private_origins: frozenset[str] = frozenset(),
):
    del session
    try:
        async with asyncio.timeout(settings.GET_TORRENT_TIMEOUT):
            return await _download_torrent_document(
                url,
                allowed_private_origins=allowed_private_origins,
            )
    except (TimeoutError, aiohttp.ClientError, OutboundUrlError):
        return (None, None, None)


async def _download_torrent_document(
    url: str,
    *,
    allowed_private_origins: frozenset[str],
):
    current_url = url
    for _ in range(MAX_TORRENT_REDIRECTS + 1):
        target = await validate_http_url(
            current_url,
            allowed_private_origins=allowed_private_origins,
        )
        connector = aiohttp.TCPConnector(
            resolver=PinnedResolver(target.addresses),
            use_dns_cache=False,
            limit=1,
        )
        async with aiohttp.ClientSession(
            connector=connector,
            timeout=torrent_timeout(),
        ) as request_session:
            async with request_session.get(
                target.url,
                headers={
                    "Accept": "application/x-bittorrent",
                    "Accept-Encoding": "identity",
                },
                allow_redirects=False,
            ) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not isinstance(location, str) or not location:
                        raise OutboundUrlError("torrent redirect is invalid")
                    info_hash = _extract_info_hash_from_magnet(location)
                    if info_hash:
                        return (None, info_hash, location)
                    current_url = urljoin(target.url, location)
                    continue
                if not is_success_status(response.status):
                    return (None, None, None)
                try:
                    document = await read_bounded_body(
                        response,
                        MAX_TORRENT_DOCUMENT_BYTES,
                    )
                except ValueError as exc:
                    raise OutboundUrlError(str(exc)) from exc
                return (document, None, None)
    raise OutboundUrlError("torrent redirected too many times")


demagnetizer = Demagnetizer()


async def get_torrent_from_magnet(magnet_uri: str):
    try:
        magnet = Magnet.from_string(magnet_uri)
        with anyio.fail_after(settings.MAGNET_RESOLVE_TIMEOUT):
            return await demagnetizer.demagnetize(magnet)
    except (TimeoutError, DemagnetizeError, MagnetError):
        return None


def _resolve_torrent_metadata(torrent) -> dict:
    info_hash = normalize_info_hash(torrent.infohash)

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
    except BencodeDecodeError:
        return {}
    if not isinstance(torrent_data, dict):
        return {}
    info = torrent_data.get(b"info")
    if not isinstance(info, dict):
        return {}
    info_hash = hashlib.sha1(bencodepy.encode(info)).hexdigest()

    announce_list = []
    for tier in torrent_data.get(b"announce-list", []):
        if not isinstance(tier, (list, tuple)):
            continue
        for tracker in tier:
            if not isinstance(tracker, bytes):
                continue
            try:
                announce_list.append(tracker.decode())
            except UnicodeDecodeError:
                continue

    announce_value = torrent_data.get(b"announce", b"")
    try:
        announce = announce_value.decode() if isinstance(announce_value, bytes) else ""
    except UnicodeDecodeError:
        announce = ""
    if announce:
        announce_list.append(announce)
    return _build_torrent_metadata_payload(
        info_hash,
        announce_list,
        _iter_bencoded_file_specs(info),
    )


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
    existing.attempts = max(existing.attempts, incoming.attempts)
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
) -> "_TorrentUpdate":
    file_index = source.get("index")
    if file_index is None:
        file_index = source.get("file_index")

    return _construct_torrent_update(
        media_id=media_id,
        info_hash=normalize_info_hash(source["info_hash"]),
        season=source.get("season"),
        episode=source.get("episode"),
        file_index=file_index,
        title=source["title"],
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
) -> "_TorrentUpdate":
    return _construct_torrent_update(
        media_id=metadata.imdb_id,
        info_hash=metadata.info_hash,
        season=metadata.season,
        episode=metadata.episode,
        file_index=metadata.file_index,
        title=metadata.title,
        seeders=metadata.seeders,
        size=metadata.size,
        tracker=metadata.tracker,
        sources=_dedupe_strings(metadata.sources),
        parsed={} if metadata.parsed is None else metadata.parsed,
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

    def to_broadcast_metadata(self, updated_at: float) -> TorrentMetadata | None:
        try:
            return _construct_torrent_metadata(
                info_hash=self.info_hash,
                title=self.title,
                size=self.size,
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
        except ValidationError:
            return None


def _iter_resolved_torrent_updates(
    resolved_torrent: dict,
    *,
    media_id: str,
    seeders: int,
    tracker: str,
    search_season: int | None,
) -> Iterator[_TorrentUpdate]:
    info_hash = resolved_torrent["info_hash"]
    sources = resolved_torrent["sources"]
    for resolved_file in resolved_torrent["files"]:
        title = resolved_file["title"]
        parsed_seasons, parsed_episodes, parsed_payload = _parse_video_title_payload(
            title
        )
        seasons = parsed_seasons or (
            (search_season,) if search_season is not None else (None,)
        )
        episode_candidates = parsed_episodes or (None,)
        episode = episode_candidates[0] if len(episode_candidates) == 1 else None
        for season in seasons:
            yield _construct_torrent_update(
                media_id=media_id,
                info_hash=info_hash,
                season=season,
                episode=episode,
                file_index=resolved_file["index"],
                title=title,
                seeders=seeders,
                size=resolved_file["size"],
                tracker=tracker,
                sources=sources,
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
        except TimeoutError:
            break
        drain_nowait()

    return batch


async def _wait_for_tracked_queue_drain(
    queue: asyncio.Queue,
    *,
    waiters_count: Callable[[], int],
    waiters_event: asyncio.Event,
    extra_idle_check: Callable[[], Awaitable[bool]] | None = None,
    extra_idle_wait: Callable[[], Awaitable[None]] | None = None,
) -> None:
    while True:
        while waiters_count() > 0:
            await waiters_event.wait()

        await queue.join()
        if waiters_count() != 0:
            continue
        if extra_idle_check is None or await extra_idle_check():
            return
        if extra_idle_wait is not None:
            await extra_idle_wait()


def _compute_batched_queue_maxsize(
    batch_size: int,
    *,
    total_slots: int,
    min_batches: int,
) -> int:
    return max(min_batches, total_slots // min(batch_size, 128))


def _begin_queue_wait(waiters: int, event: asyncio.Event) -> int:
    event.clear()
    return waiters + 1


def _end_queue_wait(waiters: int, event: asyncio.Event) -> int:
    waiters -= 1
    if waiters == 0:
        event.set()
    return waiters


def _normalize_unique_info_hashes(info_hashes: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(normalize_info_hash(value) for value in info_hashes))


async def check_torrents_exist(info_hashes: list[str]) -> set[str]:
    if not info_hashes:
        return set()

    unique_hashes = _normalize_unique_info_hashes(info_hashes)
    if not unique_hashes:
        return set()

    return await TorrentReleaseRepository(database).existing_hashes(unique_hashes)


class AddTorrentQueue:
    _STOP = object()

    def __init__(self, max_concurrent: int = 10):
        self.queue = asyncio.Queue(maxsize=DEFAULT_ADD_TORRENT_QUEUE_MAXSIZE)
        self.max_concurrent = max_concurrent
        self.metadata_cache_max_entries = DEFAULT_ADD_TORRENT_METADATA_CACHE_MAX_ENTRIES
        self._lock = asyncio.Lock()
        self._workers = []
        self._stopping = False
        self._pending_torrents = {}
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

    def _begin_queue_wait(self):
        self._queue_waiters = _begin_queue_wait(
            self._queue_waiters, self._queue_waiters_event
        )

    def _end_queue_wait(self):
        self._queue_waiters = _end_queue_wait(
            self._queue_waiters, self._queue_waiters_event
        )

    @staticmethod
    def _build_pending_metadata(seeders: int, tracker: str) -> dict[str, int | str]:
        return {
            "seeders": seeders,
            "tracker": tracker,
        }

    @staticmethod
    def _merge_pending_metadata(
        current: dict[str, int | str],
        *,
        seeders: int,
        tracker: str,
    ) -> None:
        current_seeders = current["seeders"]
        if seeders is not None and (
            current_seeders is None or seeders > current_seeders
        ):
            current["seeders"] = seeders

        if tracker:
            current["tracker"] = tracker

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
            pending_metadata = self._pending_torrents.get(queue_key)
            if pending_metadata is not None:
                self._merge_pending_metadata(
                    pending_metadata,
                    seeders=seeders,
                    tracker=tracker,
                )
                return
            self._pending_torrents[queue_key] = self._build_pending_metadata(
                seeders, tracker
            )

        await self._ensure_workers()

        payload = (queue_key, magnet_url)
        try:
            self.queue.put_nowait(payload)
        except asyncio.QueueFull:
            self._begin_queue_wait()
            try:
                await self.queue.put(payload)
            except asyncio.CancelledError:
                async with self._lock:
                    self._pending_torrents.pop(queue_key, None)
                raise
            finally:
                self._end_queue_wait()

    async def _ensure_workers(self):
        workers = self._workers
        for task in workers:
            if task.done():
                task.result()
        if (
            workers
            and len(workers) >= self.max_concurrent
            and all(not task.done() for task in workers)
        ):
            return

        async with self._lock:
            for task in self._workers:
                if task.done():
                    task.result()
            self._workers[:] = [task for task in self._workers if not task.done()]
            missing_workers = self.max_concurrent - len(self._workers)
            if missing_workers <= 0:
                return

            self._workers.extend(
                create_detached_task(
                    self._worker(),
                    name="torrent-resolution-worker",
                )
                for _ in range(missing_workers)
            )

    async def _worker(self):
        while True:
            item = await self.queue.get()
            if item is self._STOP:
                self.queue.task_done()
                return

            queue_key, magnet_url = item
            media_id, magnet_key, search_season = queue_key

            try:
                resolved_torrent = await self._get_resolved_torrent(
                    magnet_key, magnet_url
                )
                async with self._lock:
                    pending_metadata = self._pending_torrents.get(queue_key)

                if resolved_torrent and pending_metadata is not None:
                    await torrent_update_queue.add_resolved_torrent(
                        resolved_torrent,
                        media_id=media_id,
                        seeders=pending_metadata["seeders"],
                        tracker=pending_metadata["tracker"],
                        search_season=search_season,
                    )
            finally:
                async with self._lock:
                    self._pending_torrents.pop(queue_key, None)
                self.queue.task_done()

    def _discard_inflight_resolution(
        self,
        inflight_key: tuple[str, str],
        task: asyncio.Task,
    ) -> None:
        if self._inflight_resolutions.get(inflight_key) is task:
            self._inflight_resolutions.pop(inflight_key)

    async def _resolve_and_cache_torrent(
        self,
        magnet_key: str,
        magnet_url: str,
    ) -> dict:
        torrent = await get_torrent_from_magnet(magnet_url)
        resolved_torrent = (
            await asyncio.to_thread(_resolve_torrent_metadata, torrent)
            if torrent is not None
            else {}
        )
        if resolved_torrent:
            async with self._lock:
                self._resolved_torrent_cache[magnet_key] = resolved_torrent
                self._resolved_torrent_cache.move_to_end(magnet_key)
                _prune_ordered_dict(
                    self._resolved_torrent_cache,
                    max_entries=self.metadata_cache_max_entries,
                )
        return resolved_torrent

    async def _get_resolved_torrent(self, magnet_key: str, magnet_url: str) -> dict:
        inflight_key = (magnet_key, magnet_url)
        async with self._lock:
            cached = self._resolved_torrent_cache.get(magnet_key)
            if cached is not None:
                self._resolved_torrent_cache.move_to_end(magnet_key)
                return cached

            task = self._inflight_resolutions.get(inflight_key)
            if task is None:
                task = create_detached_task(
                    self._resolve_and_cache_torrent(magnet_key, magnet_url),
                    name="torrent-metadata-resolution",
                )
                self._inflight_resolutions[inflight_key] = task
                task.add_done_callback(
                    lambda completed: self._discard_inflight_resolution(
                        inflight_key, completed
                    )
                )

        return await asyncio.shield(task)

    async def stop(self):
        async with self._lock:
            self._stopping = True
            workers = list(self._workers)
            active_workers = [task for task in self._workers if not task.done()]

        try:
            if active_workers:
                await _wait_for_tracked_queue_drain(
                    self.queue,
                    waiters_count=lambda: self._queue_waiters,
                    waiters_event=self._queue_waiters_event,
                )
                for _ in active_workers:
                    await self.queue.put(self._STOP)
            if workers:
                await asyncio.gather(*workers)
        finally:
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
        self.batch_size = batch_size
        self.queue = asyncio.Queue(maxsize=DEFAULT_TORRENT_UPDATE_QUEUE_MAXSIZE)
        self._broadcast_queue = asyncio.Queue(
            maxsize=_compute_batched_queue_maxsize(
                self.batch_size,
                total_slots=DEFAULT_TORRENT_BROADCAST_QUEUE_MAXSIZE,
                min_batches=DEFAULT_TORRENT_BROADCAST_BATCH_QUEUE_MIN_SIZE,
            )
        )
        self.flush_interval = flush_interval
        self.max_retries = DEFAULT_TORRENT_UPDATE_MAX_RETRIES
        self._state_lock = asyncio.Lock()
        self._pending_lock = asyncio.Lock()
        self._retry_lock = asyncio.Lock()
        self._tasks = {
            "persistence": None,
            "broadcast": None,
            "retry": None,
        }
        self._stopping = False
        self._pending_updates = {}
        self._queue_waiters = 0
        self._queue_waiters_event = asyncio.Event()
        self._queue_waiters_event.set()
        self._broadcast_waiters = 0
        self._broadcast_waiters_event = asyncio.Event()
        self._broadcast_waiters_event.set()
        self._retry_heap = []
        self._retry_event = asyncio.Event()
        self._retry_idle_event = asyncio.Event()
        self._retry_idle_event.set()
        self._retry_inflight = 0
        self._retry_sequence = 0

    async def add_torrent_info(
        self, file_info: dict, media_id: str, from_cometnet: bool = False
    ):
        if self._stopping:
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
        media_id: str,
        from_cometnet: bool = False,
    ):
        if self._stopping:
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
        if self._stopping:
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
        if self._stopping:
            return

        await self._enqueue_prepared_item(_build_torrent_update_from_metadata(metadata))

    async def _ensure_task(self, kind: str, worker_factory):
        task = self._tasks[kind]
        if task is not None:
            if not task.done():
                return
            task.result()

        async with self._state_lock:
            task = self._tasks[kind]
            if task is not None:
                if not task.done():
                    return
                task.result()
            self._tasks[kind] = create_detached_task(
                worker_factory(),
                name=f"torrent-{kind}-worker",
            )

    async def _enqueue_prepared_item(self, item: _TorrentUpdate):
        if self._stopping:
            return

        await self._ensure_task("persistence", self._process_queue)
        await self._queue_items((item,))

    async def _enqueue_prepared_items(self, items: Iterable[_TorrentUpdate]):
        if self._stopping:
            return

        iterator = iter(items)
        first_item = next(iterator, None)
        if first_item is None:
            return

        await self._ensure_task("persistence", self._process_queue)
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
        return slow_items

    async def _discard_pending_item(self, item: _TorrentUpdate):
        async with self._pending_lock:
            if self._pending_updates.get(item.row_key) is item:
                self._pending_updates.pop(item.row_key, None)

    async def _queue_items(
        self, items: Iterable[_TorrentUpdate], *, force: bool = False
    ):
        if self._stopping and not force:
            return

        slow_items = await self._stage_pending_items(items)
        if not slow_items:
            return

        queued_count = 0
        self._queue_waiters = _begin_queue_wait(
            self._queue_waiters, self._queue_waiters_event
        )
        try:
            for item in slow_items:
                await self.queue.put(item)
                queued_count += 1
        except asyncio.CancelledError:
            for item in slow_items[queued_count:]:
                await self._discard_pending_item(item)
            raise
        finally:
            self._queue_waiters = _end_queue_wait(
                self._queue_waiters, self._queue_waiters_event
            )

    async def _enqueue_broadcast_items(
        self, batch_items: list[_TorrentUpdate], updated_at: float
    ):
        backend = get_active_backend()
        if backend is None:
            return

        metadata_batch = [
            metadata
            for item in batch_items
            if not item.from_cometnet
            and (metadata := item.to_broadcast_metadata(updated_at)) is not None
        ]
        if not metadata_batch:
            return

        await self._ensure_task("broadcast", self._process_broadcast_queue)

        try:
            self._broadcast_queue.put_nowait((backend, metadata_batch))
        except asyncio.QueueFull:
            self._broadcast_waiters = _begin_queue_wait(
                self._broadcast_waiters, self._broadcast_waiters_event
            )
            try:
                await self._broadcast_queue.put((backend, metadata_batch))
            finally:
                self._broadcast_waiters = _end_queue_wait(
                    self._broadcast_waiters, self._broadcast_waiters_event
                )

    def _retry_delay_seconds(self, attempt: int) -> float:
        delay = min(
            DEFAULT_TORRENT_UPDATE_RETRY_MAX_DELAY,
            DEFAULT_TORRENT_UPDATE_RETRY_BASE_DELAY * (2 ** (attempt - 1)),
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
            self._retry_idle_event.clear()
            self._retry_event.set()

        await self._ensure_task("retry", self._process_retry_queue)

    async def _requeue_batch_items(self, batch_items: list[_TorrentUpdate]):
        if self._stopping:
            return

        requeue_candidates = []
        for item in batch_items:
            if self.max_retries is not None and item.attempts >= self.max_retries:
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

    async def _retry_is_idle(self) -> bool:
        async with self._retry_lock:
            return not self._retry_heap and self._retry_inflight == 0

    async def _wait_for_retry_drain(self):
        while True:
            async with self._retry_lock:
                if not self._retry_heap and self._retry_inflight == 0:
                    self._retry_idle_event.set()
                    return
                retry_idle_event = self._retry_idle_event
            await retry_idle_event.wait()

    async def _process_retry_queue(self):
        while True:
            batch_items = None
            wait_timeout = None
            async with self._retry_lock:
                if self._retry_heap:
                    ready_at, _, _ = self._retry_heap[0]
                    wait_timeout = ready_at - time.monotonic()
                    if wait_timeout <= 0:
                        _, _, batch_items = heapq.heappop(self._retry_heap)
                        self._retry_inflight += 1
                    else:
                        self._retry_event.clear()
                        self._retry_idle_event.clear()
                else:
                    self._retry_event.clear()
                    if self._retry_inflight == 0:
                        self._retry_idle_event.set()

            if batch_items is not None:
                try:
                    await self._queue_items(batch_items, force=True)
                finally:
                    async with self._retry_lock:
                        self._retry_inflight -= 1
                        if not self._retry_heap and self._retry_inflight == 0:
                            self._retry_idle_event.set()
                continue

            if wait_timeout is None:
                await self._retry_event.wait()
                continue

            try:
                await asyncio.wait_for(self._retry_event.wait(), timeout=wait_timeout)
            except TimeoutError:
                pass

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
            persisted_items = []
            try:
                if batch_items:
                    updated_at = time.time()
                    persisted_items = await _execute_batched_upsert(
                        batch_items, updated_at=updated_at
                    )
            except Exception as error:
                if is_retryable_database_error(error):
                    await self._requeue_batch_items(batch_items)
                else:
                    raise
            else:
                if persisted_items:
                    await self._enqueue_broadcast_items(persisted_items, updated_at)
            finally:
                for _ in batch_keys:
                    self.queue.task_done()

    async def _process_broadcast_queue(self):
        while True:
            payload = await self._broadcast_queue.get()
            if payload is self._STOP:
                self._broadcast_queue.task_done()
                return

            try:
                backend, metadata_batch = payload
                await backend.broadcast_torrents(metadata_batch)
            finally:
                self._broadcast_queue.task_done()

    async def stop(self):
        async with self._state_lock:
            self._stopping = True
            worker = self._tasks["persistence"]

        if worker is not None:
            if not worker.done():
                await _wait_for_tracked_queue_drain(
                    self.queue,
                    waiters_count=lambda: self._queue_waiters,
                    waiters_event=self._queue_waiters_event,
                    extra_idle_check=self._retry_is_idle,
                    extra_idle_wait=self._wait_for_retry_drain,
                )
                await self.queue.put(self._STOP)
            await worker

        async with self._state_lock:
            broadcast_worker = self._tasks["broadcast"]
        if broadcast_worker is not None:
            if not broadcast_worker.done():
                await _wait_for_tracked_queue_drain(
                    self._broadcast_queue,
                    waiters_count=lambda: self._broadcast_waiters,
                    waiters_event=self._broadcast_waiters_event,
                )
                await self._broadcast_queue.put(self._STOP)
            await broadcast_worker

        async with self._state_lock:
            retry_worker = self._tasks["retry"]
        if retry_worker is not None:
            if retry_worker.done():
                await retry_worker
            else:
                retry_worker.cancel()
                try:
                    await retry_worker
                except asyncio.CancelledError:
                    pass

        async with self._retry_lock:
            self._retry_heap.clear()
            self._retry_event.clear()
            self._retry_idle_event.set()
            self._retry_inflight = 0

        async with self._pending_lock:
            self._pending_updates.clear()
        async with self._state_lock:
            self._queue_waiters = 0
            self._queue_waiters_event.set()
            self._broadcast_waiters = 0
            self._broadcast_waiters_event.set()
            self._retry_sequence = 0


def _release_torrent_row(item: _TorrentUpdate, updated_at: float) -> dict:
    return {
        "media_id": item.media_id,
        "info_hash": item.info_hash,
        "season_norm": item.season_norm,
        "episode_norm": item.episode_norm,
        "file_index": item.file_index,
        "title": item.title,
        "seeders": item.seeders,
        "size": item.size,
        "tracker": item.tracker,
        "sources_json": encode_json_param(item.sources),
        "parsed_json": encode_json_param(item.parsed),
        "updated_at": updated_at,
    }


async def _execute_batched_upsert(rows: list[_TorrentUpdate], *, updated_at: float):
    async with database.transaction():
        persisted = await TorrentReleaseRepository(database).persist_rows(
            [_release_torrent_row(item, updated_at) for item in rows]
        )
        if persisted != len(rows):
            raise RuntimeError("torrent release persistence lost an update")
    return rows


torrent_update_queue = TorrentUpdateQueue()
