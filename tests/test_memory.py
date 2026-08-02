import asyncio
import unittest
from unittest.mock import patch

from comet.utils.memory import periodic_memory_trim, trim_process_memory


class MemoryUtilityLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_periodic_trim_returns_without_spawning_work(self):
        with patch("comet.utils.memory.asyncio.to_thread") as to_thread:
            await periodic_memory_trim(0)

        to_thread.assert_not_called()

    async def test_periodic_trim_propagates_cancellation(self):
        task = asyncio.create_task(periodic_memory_trim(3_600))
        await asyncio.sleep(0)
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task

    def test_active_mimalloc_precedes_platform_fallback(self):
        with (
            patch("comet.utils.memory.gc.collect") as collect,
            patch("comet.utils.memory._is_mimalloc_active", return_value=True),
            patch(
                "comet.utils.memory._trim_with_mimalloc",
                return_value=True,
            ) as mimalloc,
            patch("comet.utils.memory._trim_with_libc") as fallback,
        ):
            self.assertTrue(trim_process_memory())

        collect.assert_called_once_with()
        mimalloc.assert_called_once_with(aggressive=True)
        fallback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
