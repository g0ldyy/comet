import asyncio
import os
import threading
import time
from dataclasses import dataclass, field

from comet.core.models import database, settings
from comet.core.runtime_registry import RuntimeIdentity
from comet.observability import create_detached_task, log, metrics

_SYNC_INTERVAL_SECONDS = 1
_TOTAL_SYNC_INTERVAL_SECONDS = 5
_HISTORY_RETENTION_SECONDS = 7 * 24 * 60 * 60
UPSERT_BANDWIDTH_STATS_QUERY = """
    INSERT INTO bandwidth_stats (id, total_bytes, updated_at)
    VALUES (1, :bytes_delta, :timestamp)
    ON CONFLICT (id) DO UPDATE SET
        total_bytes = bandwidth_stats.total_bytes + excluded.total_bytes,
        updated_at = excluded.updated_at
"""


@dataclass(slots=True)
class ConnectionMetrics:
    connection_id: str
    ip: str
    content: str
    service: str
    start_time: float
    last_update: float
    bytes_transferred: int = 0
    current_speed: float = 0.0
    peak_speed: float = 0.0
    duration: float = 0.0
    _bytes_at_sample: int = 0
    _sampled_at: float = field(default_factory=time.monotonic)
    _stream_task: asyncio.Task | None = None
    cancel_requested: bool = False
    termination_outcome: str | None = None

    def add_bytes(self, size: int, now: float) -> None:
        self.bytes_transferred += size
        self.last_update = now

    def sample(self, now: float, monotonic_now: float) -> None:
        elapsed = monotonic_now - self._sampled_at
        self.current_speed = (self.bytes_transferred - self._bytes_at_sample) / elapsed
        self.peak_speed = max(self.peak_speed, self.current_speed)
        self.duration = now - self.start_time
        self._bytes_at_sample = self.bytes_transferred
        self._sampled_at = monotonic_now

    @property
    def average_speed(self) -> float:
        return self.bytes_transferred / self.duration if self.duration > 0 else 0.0


class BandwidthMonitor:
    def __init__(self):
        self._connections: dict[str, ConnectionMetrics] = {}
        self._lock = threading.RLock()
        self._persisted_total_bytes = 0
        self._total_bytes_session = 0
        self._pending_bytes = 0
        self._peak_concurrent = 0
        self._sync_task: asyncio.Task | None = None
        self._initialized = False
        self._lifecycle_lock = asyncio.Lock()
        self._identity = RuntimeIdentity.current()
        self._process_id = os.getpid()

    async def initialize(self) -> None:
        async with self._lifecycle_lock:
            if self._initialized:
                return
            total = await database.fetch_val(
                "SELECT total_bytes FROM bandwidth_stats WHERE id = 1"
            )
            self._persisted_total_bytes = int(total or 0)
            await database.execute(
                """
                DELETE FROM active_connections
                WHERE instance_id = :instance_id AND process_id = :process_id
                """,
                {
                    "instance_id": self._identity.instance_id,
                    "process_id": self._process_id,
                },
            )
            self._sync_task = create_detached_task(
                self._sync_loop(),
                name="bandwidth-sync",
            )
            self._initialized = True

    async def start_connection(
        self,
        connection_id: str,
        ip: str,
        content: str,
        service: str,
        *,
        started_at: float,
    ) -> None:
        if not self._initialized:
            await self.initialize()
        with self._lock:
            self._connections[connection_id] = ConnectionMetrics(
                connection_id=connection_id,
                ip=ip,
                content=content,
                service=service,
                start_time=started_at,
                last_update=started_at,
            )
            self._peak_concurrent = max(
                self._peak_concurrent,
                len(self._connections),
            )
        metrics.proxy_connection_started()

    def bind_stream_task(self, connection_id: str) -> None:
        task = asyncio.current_task()
        with self._lock:
            connection = self._connections.get(connection_id)
            if connection is not None:
                connection._stream_task = task

    def update_connection(self, connection_id: str, bytes_chunk: int) -> None:
        with self._lock:
            connection = self._connections.get(connection_id)
            if connection is None:
                return
            connection.add_bytes(bytes_chunk, time.time())
            self._total_bytes_session += bytes_chunk
            self._pending_bytes += bytes_chunk

    async def end_connection(
        self,
        connection_id: str,
        *,
        outcome: str,
        error_code: str | None = None,
    ) -> ConnectionMetrics | None:
        finished_at = time.time()
        with self._lock:
            connection = self._connections.pop(connection_id, None)
            if connection is None:
                return None
            connection.sample(finished_at, time.monotonic())
        if connection.termination_outcome is not None:
            outcome = connection.termination_outcome
            error_code = None
        await database.execute(
            """
            INSERT INTO proxy_connection_history (
                id, ip, content, service, instance_id, process_id,
                started_at, finished_at, duration, bytes_transferred,
                average_speed, peak_speed, outcome, error_code
            ) VALUES (
                :id, :ip, :content, :service, :instance_id, :process_id,
                :started_at, :finished_at, :duration, :bytes_transferred,
                :average_speed, :peak_speed, :outcome, :error_code
            )
            ON CONFLICT (id) DO NOTHING
            """,
            {
                "id": connection.connection_id,
                "ip": connection.ip,
                "content": connection.content,
                "service": connection.service,
                "instance_id": self._identity.instance_id,
                "process_id": self._process_id,
                "started_at": connection.start_time,
                "finished_at": finished_at,
                "duration": connection.duration,
                "bytes_transferred": connection.bytes_transferred,
                "average_speed": connection.average_speed,
                "peak_speed": connection.peak_speed,
                "outcome": outcome,
                "error_code": error_code,
            },
        )
        metrics.proxy_connection_finished(
            connection.bytes_transferred,
            connection.duration,
        )
        return connection

    def get_all_active_connections(self) -> dict[str, ConnectionMetrics]:
        with self._lock:
            now = time.time()
            monotonic_now = time.monotonic()
            for connection in self._connections.values():
                connection.sample(now, monotonic_now)
            return self._connections.copy()

    def get_global_stats(self) -> dict[str, int | float]:
        with self._lock:
            return {
                "total_bytes_alltime": (
                    self._persisted_total_bytes + self._pending_bytes
                ),
                "total_bytes_session": self._total_bytes_session,
                "total_current_speed": sum(
                    connection.current_speed
                    for connection in self._connections.values()
                ),
                "active_connections": len(self._connections),
                "peak_concurrent": self._peak_concurrent,
            }

    @staticmethod
    def format_speed(bytes_per_second: float) -> str:
        if bytes_per_second < 1024:
            return f"{bytes_per_second:.0f} B/s"
        if bytes_per_second < 1024**2:
            return f"{bytes_per_second / 1024:.1f} KB/s"
        if bytes_per_second < 1024**3:
            return f"{bytes_per_second / 1024**2:.1f} MB/s"
        return f"{bytes_per_second / 1024**3:.2f} GB/s"

    async def _sync_connections(self, now: float) -> None:
        monotonic_now = time.monotonic()
        inactivity_threshold = settings.PROXY_DEBRID_STREAM_INACTIVITY_THRESHOLD
        with self._lock:
            connections = list(self._connections.values())
            for connection in connections:
                connection.sample(now, monotonic_now)
                if (
                    inactivity_threshold > 0
                    and now - connection.last_update > inactivity_threshold
                    and connection.termination_outcome is None
                ):
                    connection.termination_outcome = "inactive"
                    if connection._stream_task is not None:
                        connection._stream_task.cancel()
            values = [
                {
                    "id": connection.connection_id,
                    "updated_at": now,
                    "bytes_transferred": connection.bytes_transferred,
                    "current_speed": connection.current_speed,
                    "peak_speed": connection.peak_speed,
                    "instance_id": self._identity.instance_id,
                    "process_id": self._process_id,
                }
                for connection in connections
            ]
        if values:
            await database.execute_many(
                """
                UPDATE active_connections
                SET updated_at = :updated_at,
                    bytes_transferred = :bytes_transferred,
                    current_speed = :current_speed,
                    peak_speed = :peak_speed
                WHERE id = :id
                  AND instance_id = :instance_id
                  AND process_id = :process_id
                """,
                values,
            )
        requested = await database.fetch_all(
            """
            SELECT id
            FROM active_connections
            WHERE instance_id = :instance_id
              AND process_id = :process_id
              AND cancel_requested = 1
            """,
            {
                "instance_id": self._identity.instance_id,
                "process_id": self._process_id,
            },
            force_primary=True,
        )
        with self._lock:
            for row in requested:
                connection = self._connections.get(row["id"])
                if connection is not None and not connection.cancel_requested:
                    connection.cancel_requested = True
                    connection.termination_outcome = "cancelled"
                    if connection._stream_task is not None:
                        connection._stream_task.cancel()

    async def _persist_total_bytes(
        self, bytes_delta: int, sync_timestamp: float
    ) -> None:
        await database.execute(
            UPSERT_BANDWIDTH_STATS_QUERY,
            {"bytes_delta": bytes_delta, "timestamp": sync_timestamp},
        )

    async def _flush_pending_bytes(self, sync_timestamp: float) -> None:
        with self._lock:
            bytes_delta = self._pending_bytes
            self._pending_bytes = 0
        if bytes_delta == 0:
            return
        try:
            await self._persist_total_bytes(bytes_delta, sync_timestamp)
        except BaseException:
            with self._lock:
                self._pending_bytes += bytes_delta
            raise
        with self._lock:
            self._persisted_total_bytes += bytes_delta

    async def _sync_loop(self) -> None:
        total_sync_due = 0.0
        history_cleanup_due = 0.0
        while True:
            await asyncio.sleep(_SYNC_INTERVAL_SECONDS)
            now = time.time()
            try:
                await self._sync_connections(now)
                sampled_at = float(int(now // 60) * 60)
                with self._lock:
                    active_connections = len(self._connections)
                    current_speed = sum(
                        connection.current_speed
                        for connection in self._connections.values()
                    )
                await database.execute(
                    """
                    INSERT INTO proxy_traffic_samples (
                        instance_id, process_id, sampled_at,
                        active_connections, current_speed
                    ) VALUES (
                        :instance_id, :process_id, :sampled_at,
                        :active_connections, :current_speed
                    )
                    ON CONFLICT (instance_id, process_id, sampled_at) DO UPDATE SET
                        active_connections = excluded.active_connections,
                        current_speed = excluded.current_speed
                    """,
                    {
                        "instance_id": self._identity.instance_id,
                        "process_id": self._process_id,
                        "sampled_at": sampled_at,
                        "active_connections": active_connections,
                        "current_speed": current_speed,
                    },
                )
                if now >= total_sync_due:
                    await self._flush_pending_bytes(now)
                    total_sync_due = now + _TOTAL_SYNC_INTERVAL_SECONDS
                if now >= history_cleanup_due:
                    await database.execute(
                        """
                        DELETE FROM proxy_connection_history
                        WHERE finished_at < :cutoff
                        """,
                        {"cutoff": now - _HISTORY_RETENTION_SECONDS},
                    )
                    await database.execute(
                        """
                        DELETE FROM proxy_traffic_samples
                        WHERE sampled_at < :cutoff
                        """,
                        {"cutoff": now - _HISTORY_RETENTION_SECONDS},
                    )
                    history_cleanup_due = now + 60 * 60
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.warning(
                    "proxy.telemetry.sync_failed",
                    "Proxy telemetry synchronization failed",
                    error_code="database_warning",
                    exc=error,
                )

    async def shutdown(self) -> None:
        async with self._lifecycle_lock:
            if self._sync_task is not None:
                self._sync_task.cancel()
                await asyncio.gather(self._sync_task, return_exceptions=True)
            await self._flush_pending_bytes(time.time())
            await database.execute(
                """
                DELETE FROM active_connections
                WHERE instance_id = :instance_id AND process_id = :process_id
                """,
                {
                    "instance_id": self._identity.instance_id,
                    "process_id": self._process_id,
                },
            )
            self._sync_task = None
            self._initialized = False
            with self._lock:
                self._connections.clear()
                self._total_bytes_session = 0
                self._peak_concurrent = 0


bandwidth_monitor = BandwidthMonitor()
