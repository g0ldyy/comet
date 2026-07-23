import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from comet.services.lock import DistributedLock


class DistributedLockLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_renews_lease_until_operation_finishes(self):
        lock = DistributedLock("media", timeout=0.02)
        lock.acquired = True
        renewed = asyncio.Event()

        async def renew():
            renewed.set()
            return True

        async def operation():
            await renewed.wait()
            return "complete"

        with patch.object(lock, "acquire", new=AsyncMock(side_effect=renew)) as acquire:
            result = await lock.run(operation())

        self.assertEqual(result, "complete")
        acquire.assert_awaited()

    async def test_run_cancels_operation_when_lease_is_lost(self):
        lock = DistributedLock("media", timeout=0.02)
        lock.acquired = True
        cancelled = asyncio.Event()

        async def operation():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with patch.object(lock, "acquire", new=AsyncMock(return_value=False)):
            with self.assertRaisesRegex(RuntimeError, "Lost distributed lock"):
                await lock.run(operation())

        self.assertTrue(cancelled.is_set())
