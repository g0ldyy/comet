import asyncio
import threading
import time
from dataclasses import dataclass, field

from comet.core.logger import logger
from comet.core.models import database


@dataclass
class ConnectionMetrics:
    connection_id: str
    ip: str
    content: str
    start_time: float
    last_update: float = field(default_factory=time.time)
    bytes_transferred: int = 0
    current_speed: float = 0.0  # bytes per second
    peak_speed: float = 0.0
    duration: float = 0.0

    def update_metrics(self, bytes_chunk: int):
        current_time = time.time()
        self.bytes_transferred += bytes_chunk

        # Calculate current speed (bytes/second) over last update interval
        time_diff = current_time - self.last_update
        if time_diff > 0:
            speed = bytes_chunk / time_diff
            self.current_speed = speed
            self.peak_speed = max(self.peak_speed, speed)

        self.last_update = current_time
        self.duration = current_time - self.start_time


class BandwidthMonitor:
    def __init__(self):
        self._connections = {}
        self._lock = threading.RLock()
        self._global_stats = {
            "total_bytes_alltime": 0,
            "total_bytes_session": 0,
            "active_connections": 0,
            "peak_concurrent": 0,
        }
        self._last_synced_bytes = 0
        self._cleanup_task = None
        self._db_sync_task = None
        self._initialized = False

    async def initialize(self):
        if self._initialized:
            return

        # Load existing alltime total from database
        try:
            total = await database.fetch_val(
                "SELECT total_bytes FROM bandwidth_stats WHERE id = 1"
            )
            if total is not None:
                total_bytes = int(total)
                self._global_stats["total_bytes_alltime"] = total_bytes
                self._last_synced_bytes = total_bytes
        except Exception:
            # Table might not exist yet, will be created later
            pass

        # Start background tasks
        self._cleanup_task = asyncio.create_task(self._cleanup_inactive_connections())
        self._db_sync_task = asyncio.create_task(self._sync_to_database())

        self._initialized = True

    async def start_connection(self, connection_id: str, ip: str, content: str):
        if not self._initialized:
            await self.initialize()

        with self._lock:
            metrics = ConnectionMetrics(
                connection_id=connection_id,
                ip=ip,
                content=content,
                start_time=time.time(),
            )
            self._connections[connection_id] = metrics

            # Update global stats
            self._global_stats["active_connections"] = len(self._connections)
            self._global_stats["peak_concurrent"] = max(
                self._global_stats["peak_concurrent"],
                self._global_stats["active_connections"],
            )

    def update_connection(self, connection_id: str, bytes_chunk: int):
        with self._lock:
            if connection_id in self._connections:
                self._connections[connection_id].update_metrics(bytes_chunk)
                self._global_stats["total_bytes_session"] += bytes_chunk
                self._global_stats["total_bytes_alltime"] += bytes_chunk

    async def end_connection(self, connection_id: str):
        with self._lock:
            metrics = self._connections.pop(connection_id, None)
            if metrics:
                self._global_stats["active_connections"] = len(self._connections)

                # Log final metrics (only once at end, no spam)
                # total_mb = metrics.bytes_transferred / (1024 * 1024)
                # avg_speed_mbps = (
                #     (metrics.bytes_transferred / metrics.duration / (1024 * 1024))
                #     if metrics.duration > 0
                #     else 0
                # )
                # logger.log(
                #     "STREAM",
                #     f"Stream ended - {connection_id[:8]} - {total_mb:.1f}MB in {metrics.duration:.1f}s (avg: {avg_speed_mbps:.1f}MB/s)",
                # )

            return metrics

    def get_connection_metrics(self, connection_id: str):
        with self._lock:
            return self._connections.get(connection_id)

    def get_all_active_connections(self):
        with self._lock:
            return self._connections.copy()

    def get_global_stats(self):
        with self._lock:
            stats = self._global_stats.copy()

            # Calculate total current speed across all connections
            total_current_speed = sum(
                conn.current_speed for conn in self._connections.values()
            )
            stats["total_current_speed"] = total_current_speed

            return stats

    def format_speed(self, bytes_per_second: float):
        if bytes_per_second < 1024:
            return f"{bytes_per_second:.0f} B/s"
        elif bytes_per_second < 1024**2:
            return f"{bytes_per_second / 1024:.1f} KB/s"
        elif bytes_per_second < 1024**3:
            return f"{bytes_per_second / (1024**2):.1f} MB/s"
        else:
            return f"{bytes_per_second / (1024**3):.2f} GB/s"

    async def _cleanup_inactive_connections(self):
        while True:
            try:
                await asyncio.sleep(30)  # Check every 30 seconds
                current_time = time.time()

                with self._lock:
                    inactive_connections = [
                        conn_id
                        for conn_id, metrics in self._connections.items()
                        if current_time - metrics.last_update > 60  # 60 seconds timeout
                    ]

                # Remove inactive connections
                for conn_id in inactive_connections:
                    await self.end_connection(conn_id)

            except Exception as e:
                logger.warning(f"Error in bandwidth monitor cleanup: {e}")

    async def _sync_to_database(self):
        while True:
            try:
                await asyncio.sleep(5)  # Sync every 5 seconds

                with self._lock:
                    total_bytes = self._global_stats["total_bytes_alltime"]

                    if total_bytes == self._last_synced_bytes:
                        continue

                # Update database with alltime total
                try:
                    # Try to insert first
                    await database.execute(
                        "INSERT INTO bandwidth_stats (id, total_bytes, last_updated) VALUES (1, :total_bytes, :timestamp)",
                        {"total_bytes": total_bytes, "timestamp": time.time()},
                    )
                except Exception:
                    # If insert fails (record exists), update instead
                    await database.execute(
                        "UPDATE bandwidth_stats SET total_bytes = :total_bytes, last_updated = :timestamp WHERE id = 1",
                        {"total_bytes": total_bytes, "timestamp": time.time()},
                    )

                with self._lock:
                    self._last_synced_bytes = total_bytes

            except Exception as e:
                logger.warning(f"Error syncing bandwidth stats to database: {e}")

    async def shutdown(self):
        if self._cleanup_task:
            self._cleanup_task.cancel()
        if self._db_sync_task:
            self._db_sync_task.cancel()


bandwidth_monitor = BandwidthMonitor()
