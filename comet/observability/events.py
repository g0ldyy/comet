"""Non-blocking process-local buffering for shared operational events."""

from __future__ import annotations

import asyncio
import atexit
import os
import threading
import time
from collections import deque
from collections.abc import Mapping

from databases import Database

from comet.core.event_store import EventStore, EventWrite
from comet.core.runtime_registry import RuntimeIdentity

_MAX_PENDING_EVENTS = 4096
_WRITE_BATCH_SIZE = 128
_FLUSH_INTERVAL_SECONDS = 0.25
_PRUNE_INTERVAL_SECONDS = 300
_DETAIL_EXCLUSIONS = frozenset(
    {
        "category",
        "connection_id",
        "debug_stack",
        "details",
        "engine_generation",
        "error_code",
        "error_message",
        "event",
        "function",
        "level",
        "line",
        "media_type",
        "message",
        "module",
        "outcome",
        "pid",
        "process_role",
        "provider_name",
        "request_id",
        "run_id",
        "timestamp",
    }
)


class _EventBuffer:
    def __init__(self) -> None:
        self._items: deque[tuple[float, dict[str, object]]] = deque()
        self._dropped = 0
        self._lock = threading.Lock()

    def submit(self, record: Mapping[str, object]) -> None:
        with self._lock:
            if len(self._items) == _MAX_PENDING_EVENTS:
                self._dropped += 1
                return
            self._items.append((time.time(), dict(record)))

    def drain(self) -> tuple[list[tuple[float, dict[str, object]]], int]:
        with self._lock:
            count = min(len(self._items), _WRITE_BATCH_SIZE)
            items = [self._items.popleft() for _ in range(count)]
            dropped = self._dropped
            self._dropped = 0
            return items, dropped

    def restore(
        self,
        items: list[tuple[float, dict[str, object]]],
        dropped: int,
    ) -> None:
        with self._lock:
            room = _MAX_PENDING_EVENTS - len(self._items)
            restored = items[:room]
            self._items.extendleft(reversed(restored))
            self._dropped += dropped + len(items) - len(restored)

    def pending(self) -> bool:
        with self._lock:
            return bool(self._items) or self._dropped > 0


_buffer = _EventBuffer()
_lifecycle_lock = threading.Lock()
_owner_pid: int | None = None
_stop_event: threading.Event | None = None
_writer_thread: threading.Thread | None = None


class _PrimaryDatabase:
    """Expose the primary-only subset used by EventStore."""

    def __init__(self, url: str) -> None:
        self._database = Database(url)
        self.url = self._database.url

    async def connect(self) -> None:
        await self._database.connect()

    async def disconnect(self) -> None:
        await self._database.disconnect()

    def transaction(self, **kwargs):
        return self._database.transaction(**kwargs)

    async def execute(self, query, values=None, *, force_primary: bool = False):
        return await self._database.execute(query, values)

    async def execute_many(self, query, values, *, force_primary: bool = False):
        return await self._database.execute_many(query, values)

    async def fetch_val(self, query, values=None, *, force_primary: bool = False):
        return await self._database.fetch_val(query, values)


def capture_event(record: Mapping[str, object]) -> None:
    if _owner_pid == os.getpid():
        _buffer.submit(record)


def start_event_persistence(database_url: str) -> None:
    global _owner_pid, _stop_event, _writer_thread
    pid = os.getpid()
    with _lifecycle_lock:
        if (
            _owner_pid == pid
            and _writer_thread is not None
            and _writer_thread.is_alive()
        ):
            return
        _owner_pid = pid
        _stop_event = threading.Event()
        _writer_thread = threading.Thread(
            target=_run_writer,
            args=(database_url, _stop_event),
            name="comet-event-writer",
            daemon=True,
        )
        _writer_thread.start()


def stop_event_persistence() -> None:
    global _owner_pid, _stop_event, _writer_thread
    pid = os.getpid()
    with _lifecycle_lock:
        if _owner_pid != pid or _stop_event is None or _writer_thread is None:
            return
        stop = _stop_event
        writer = _writer_thread
        _owner_pid = None
        _stop_event = None
        _writer_thread = None
    stop.set()
    writer.join(timeout=3)


def _reset_after_fork() -> None:
    global _buffer, _lifecycle_lock, _owner_pid, _stop_event, _writer_thread
    _buffer = _EventBuffer()
    _lifecycle_lock = threading.Lock()
    _owner_pid = None
    _stop_event = None
    _writer_thread = None


def _run_writer(database_url: str, stop: threading.Event) -> None:
    try:
        asyncio.run(_writer(database_url, stop))
    except Exception as exc:
        from comet.observability.logging import log

        log.error(
            "events.persistence.stopped",
            "Operational event persistence stopped",
            error_code="event_persistence_failure",
            exc=exc,
        )


async def _writer(database_url: str, stop: threading.Event) -> None:
    database = _PrimaryDatabase(database_url)
    identity = RuntimeIdentity.current()
    store = EventStore(database)
    await database.connect()
    try:
        next_prune = time.monotonic() + _PRUNE_INTERVAL_SECONDS
        write_degraded = False
        while not stop.is_set() or _buffer.pending():
            batch, dropped = _buffer.drain()
            if batch or dropped:
                try:
                    await store.append(
                        [
                            _event_write(created_at, identity.instance_id, record)
                            for created_at, record in batch
                        ],
                        dropped=dropped,
                    )
                except Exception as exc:
                    _buffer.restore(batch, dropped)
                    if not write_degraded:
                        from comet.observability.logging import log

                        log.error(
                            "events.persistence.degraded",
                            "Operational event persistence is degraded",
                            error_code="event_persistence_failure",
                            exc=exc,
                        )
                        write_degraded = True
                    if stop.is_set():
                        break
                    await asyncio.sleep(1)
                    continue
                if write_degraded:
                    from comet.observability.logging import log

                    log.info(
                        "events.persistence.recovered",
                        "Operational event persistence recovered",
                    )
                    write_degraded = False
            now = time.monotonic()
            if now >= next_prune:
                try:
                    await store.prune()
                except Exception as exc:
                    from comet.observability.logging import log

                    log.warning(
                        "events.retention.failed",
                        "Operational event retention failed",
                        error_code="event_retention_failure",
                        exc=exc,
                    )
                next_prune = now + _PRUNE_INTERVAL_SECONDS
            await asyncio.sleep(_FLUSH_INTERVAL_SECONDS)
    finally:
        await database.disconnect()


def _event_write(
    created_at: float,
    instance_id: str,
    record: Mapping[str, object],
) -> EventWrite:
    details = {
        key: value for key, value in record.items() if key not in _DETAIL_EXCLUSIONS
    }
    return EventWrite(
        created_at=created_at,
        instance_id=instance_id,
        process_id=record["pid"],
        role=record["process_role"],
        level=record["level"],
        category=record["category"],
        event=record["event"],
        message=record["message"],
        request_id=record.get("request_id"),
        run_id=record.get("run_id"),
        connection_id=record.get("connection_id"),
        media_type=record.get("media_type"),
        provider_name=record.get("provider_name"),
        outcome=record.get("outcome"),
        error_code=record.get("error_code"),
        details=details,
    )


__all__ = (
    "capture_event",
    "start_event_persistence",
    "stop_event_persistence",
)

if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)
atexit.register(stop_event_persistence)
