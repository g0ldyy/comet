import asyncio
import time
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from databases import Database

import comet.services.bandwidth as bandwidth_module
from comet.core.db_router import ReplicaAwareDatabase
from comet.core.schema_migrations import run_schema_migrations
from comet.services.bandwidth import BandwidthMonitor


class BandwidthMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_initialization_starts_one_shared_sync_task(self):
        monitor = BandwidthMonitor()
        fetch = AsyncMock(return_value=123)
        execute = AsyncMock()

        with (
            patch("comet.services.bandwidth.database.fetch_val", new=fetch),
            patch("comet.services.bandwidth.database.execute", new=execute),
        ):
            await asyncio.gather(
                monitor.initialize(),
                monitor.initialize(),
                monitor.initialize(),
            )

        try:
            fetch.assert_awaited_once()
            self.assertTrue(monitor._initialized)
            self.assertIsNotNone(monitor._sync_task)
            self.assertEqual(
                monitor.get_global_stats()["total_bytes_alltime"],
                123,
            )
        finally:
            with patch(
                "comet.services.bandwidth.database.execute",
                new=AsyncMock(),
            ):
                await monitor.shutdown()

    async def test_sampling_uses_interval_bytes_and_tracks_average(self):
        monitor = BandwidthMonitor()
        monitor._initialized = True
        started_at = time.time() - 4
        await monitor.start_connection(
            "connection",
            "192.0.2.1",
            "Example",
            "realdebrid",
            started_at=started_at,
        )
        connection = monitor._connections["connection"]
        connection._sampled_at = time.monotonic() - 2

        monitor.update_connection("connection", 2_000)
        connection.sample(time.time(), time.monotonic())

        self.assertGreater(connection.current_speed, 900)
        self.assertLess(connection.current_speed, 1_100)
        self.assertGreater(connection.average_speed, 400)
        self.assertEqual(monitor.get_global_stats()["total_bytes_session"], 2_000)

    async def test_owner_poll_cancels_only_the_targeted_stream_task(self):
        monitor = BandwidthMonitor()
        monitor._initialized = True
        await monitor.start_connection(
            "target",
            "192.0.2.1",
            "Example",
            "realdebrid",
            started_at=time.time(),
        )
        stream_task = asyncio.create_task(asyncio.sleep(3600))
        monitor._connections["target"]._stream_task = stream_task

        with (
            patch(
                "comet.services.bandwidth.database.execute_many",
                new=AsyncMock(),
            ),
            patch(
                "comet.services.bandwidth.database.fetch_all",
                new=AsyncMock(return_value=[{"id": "target"}]),
            ),
        ):
            await monitor._sync_connections(time.time())
        await asyncio.gather(stream_task, return_exceptions=True)

        self.assertTrue(stream_task.cancelled())
        self.assertTrue(monitor._connections["target"].cancel_requested)
        self.assertEqual(
            monitor._connections["target"].termination_outcome,
            "cancelled",
        )

    async def test_worker_deltas_accumulate_atomically(self):
        with TemporaryDirectory() as temp_dir:
            database = ReplicaAwareDatabase(
                Database(f"sqlite+aiosqlite:///{temp_dir}/bandwidth.db")
            )
            await database.connect()
            try:
                await run_schema_migrations(
                    database,
                    is_sqlite=True,
                    is_postgres=False,
                )
                with patch.object(bandwidth_module, "database", database):
                    await BandwidthMonitor()._persist_total_bytes(400, time.time())
                    await BandwidthMonitor()._persist_total_bytes(600, time.time())
                total = await database.fetch_val(
                    "SELECT total_bytes FROM bandwidth_stats WHERE id = 1"
                )
            finally:
                await database.disconnect()
        self.assertEqual(total, 1_000)

    async def test_shutdown_awaits_sync_persists_and_resets_for_restart(self):
        monitor = BandwidthMonitor()
        monitor._initialized = True
        monitor._sync_task = asyncio.create_task(asyncio.sleep(3600))
        sync_task = monitor._sync_task
        monitor._pending_bytes = 1_234
        monitor._total_bytes_session = 234
        monitor._peak_concurrent = 1
        persist = AsyncMock()

        with (
            patch.object(monitor, "_persist_total_bytes", new=persist),
            patch(
                "comet.services.bandwidth.database.execute",
                new=AsyncMock(),
            ),
        ):
            await monitor.shutdown()

        self.assertTrue(sync_task.cancelled())
        persist.assert_awaited_once()
        self.assertEqual(persist.await_args.args[0], 1_234)
        self.assertFalse(monitor._initialized)
        self.assertIsNone(monitor._sync_task)
        self.assertEqual(monitor._connections, {})
        self.assertEqual(monitor.get_global_stats()["total_bytes_session"], 0)
