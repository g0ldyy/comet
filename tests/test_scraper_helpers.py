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


if __name__ == "__main__":
    unittest.main()
