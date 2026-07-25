import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from comet.services.bandwidth import BandwidthMonitor


class BandwidthMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_awaits_tasks_persists_and_resets_for_restart(self):
        monitor = BandwidthMonitor()
        monitor._initialized = True
        monitor._cleanup_task = asyncio.create_task(asyncio.sleep(3600))
        monitor._db_sync_task = asyncio.create_task(asyncio.sleep(3600))
        cleanup_task = monitor._cleanup_task
        sync_task = monitor._db_sync_task
        monitor._connections["connection"] = object()
        monitor._global_stats.update(
            {
                "total_bytes_alltime": 1234,
                "total_bytes_session": 234,
                "active_connections": 1,
                "peak_concurrent": 1,
            }
        )
        persist = AsyncMock()

        with patch.object(monitor, "_persist_total_bytes", new=persist):
            await monitor.shutdown()

        self.assertTrue(cleanup_task.cancelled())
        self.assertTrue(sync_task.cancelled())
        persist.assert_awaited_once()
        self.assertEqual(persist.await_args.args[0], 1234)
        self.assertFalse(monitor._initialized)
        self.assertIsNone(monitor._cleanup_task)
        self.assertIsNone(monitor._db_sync_task)
        self.assertEqual(monitor._connections, {})
        self.assertEqual(monitor._global_stats["total_bytes_session"], 0)
        self.assertEqual(monitor._global_stats["active_connections"], 0)
