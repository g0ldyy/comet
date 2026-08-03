import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from comet.services.lock import DistributedLock


class DistributedLockLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_wait_timeout_attempts_once_without_sleeping(self):
        database = type(
            "Database",
            (),
            {"fetch_one": AsyncMock(return_value=None)},
        )()
        lock = DistributedLock("media", database=database)

        with patch("comet.services.lock.asyncio.sleep", new=AsyncMock()) as sleep:
            acquired = await lock.acquire(wait_timeout=0)

        self.assertFalse(acquired)
        database.fetch_one.assert_awaited_once()
        sleep.assert_not_awaited()

    async def test_wait_deadline_uses_monotonic_time_when_wall_clock_moves(self):
        database = type(
            "Database",
            (),
            {"fetch_one": AsyncMock(return_value=None)},
        )()
        lock = DistributedLock(
            "media",
            timeout=10,
            retry_interval=0.1,
            database=database,
        )

        with (
            patch(
                "comet.services.lock.time.monotonic",
                side_effect=(10.0, 10.5, 11.0),
            ),
            patch(
                "comet.services.lock.time.time",
                side_effect=(1_000.0, 0.0),
            ),
            patch("comet.services.lock.asyncio.sleep", new=AsyncMock()) as sleep,
        ):
            acquired = await lock.acquire(wait_timeout=1)

        self.assertFalse(acquired)
        self.assertEqual(database.fetch_one.await_count, 2)
        sleep.assert_awaited_once_with(0.1)

    async def test_fractional_lock_lifetime_is_not_truncated(self):
        database = type(
            "Database",
            (),
            {"fetch_one": AsyncMock(return_value={"acquired": 1})},
        )()
        lock = DistributedLock("media", timeout=0.02, database=database)

        with patch("comet.services.lock.time.time", return_value=100.0):
            self.assertTrue(await lock.acquire())

        values = database.fetch_one.await_args.args[1]
        self.assertEqual(values["updated_at"], 100.0)
        self.assertEqual(values["expires_at"], 100.02)

    async def test_acquire_exposes_database_failure(self):
        database = type(
            "Database",
            (),
            {"fetch_one": AsyncMock(side_effect=RuntimeError("database failure"))},
        )()
        lock = DistributedLock("media", database=database)

        with self.assertRaisesRegex(RuntimeError, "database failure"):
            await lock.acquire()

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

    async def test_run_exposes_renewal_failure_and_cancels_operation(self):
        lock = DistributedLock("media", timeout=0.02)
        lock.acquired = True
        cancelled = asyncio.Event()

        async def operation():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with patch.object(
            lock,
            "acquire",
            new=AsyncMock(side_effect=RuntimeError("database failure")),
        ):
            with self.assertRaisesRegex(RuntimeError, "database failure"):
                await lock.run(operation())

        self.assertTrue(cancelled.is_set())

    async def test_failed_release_withdraws_local_ownership_and_surfaces_failure(self):
        database = type(
            "Database",
            (),
            {"execute": AsyncMock(side_effect=RuntimeError("secret failure"))},
        )()
        lock = DistributedLock("private-media-id", database=database)
        lock.acquired = True
        with self.assertRaisesRegex(RuntimeError, "secret failure"):
            await lock.release()

        self.assertFalse(lock.acquired)
