import asyncio
import unittest

from comet.discovery.torrent_base import gather_concurrently
from comet.utils.parsing import associate_urls_credentials


class ScraperTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_gather_preserves_successful_sibling_on_partial_failure(self):
        async def succeed():
            return "result"

        async def fail():
            raise RuntimeError("transport failed")

        self.assertEqual(
            await gather_concurrently(
                (succeed(), fail()),
                preserve_successes=True,
            ),
            ["result"],
        )

    async def test_gather_propagates_partial_failure_when_coverage_is_incomplete(self):
        async def succeed():
            return "result"

        async def fail():
            raise RuntimeError("transport failed")

        with self.assertRaisesRegex(RuntimeError, "transport failed"):
            await gather_concurrently((succeed(), fail()))

    async def test_gather_does_not_absorb_task_cancellation(self):
        async def cancel():
            raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await gather_concurrently((cancel(),))

    async def test_gather_can_surface_a_complete_transport_failure(self):
        async def fail():
            raise RuntimeError("transport failed")

        with (
            self.assertRaisesRegex(RuntimeError, "transport failed"),
        ):
            await gather_concurrently((fail(),))

    async def test_gather_limit_refills_slots_without_waiting_for_slow_siblings(self):
        in_flight = peak = 0
        slow_release = asyncio.Event()
        finished = []

        async def run(index):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            if index == 0:
                await slow_release.wait()
            else:
                await asyncio.sleep(0)
            in_flight -= 1
            finished.append(index)
            if len(finished) == 4:
                slow_release.set()
            return index

        self.assertEqual(
            await gather_concurrently((run(i) for i in range(5)), limit=2),
            [0, 1, 2, 3, 4],
        )
        self.assertEqual(peak, 2)
        self.assertEqual(finished, [1, 2, 3, 4, 0])


class ScraperHelperConfigTests(unittest.TestCase):
    def test_url_credentials_follow_the_single_current_schema(self):
        self.assertEqual(associate_urls_credentials(None, None), [])
        self.assertEqual(associate_urls_credentials([], None), [])
        self.assertEqual(
            associate_urls_credentials(["one", "two"], "shared"),
            [("one", "shared"), ("two", "shared")],
        )
        self.assertEqual(
            associate_urls_credentials(["one", "two"], ["first", ""]),
            [("one", "first"), ("two", None)],
        )
        self.assertEqual(
            associate_urls_credentials(["one", "two"], ["first"]),
            [("one", "first"), ("two", None)],
        )


if __name__ == "__main__":
    unittest.main()
