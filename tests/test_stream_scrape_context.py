import unittest
from unittest.mock import AsyncMock, patch

from comet.core.scrape import ScrapeContext
from comet.services.media_search import background_scrape


class StreamScrapeContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_cache_refresh_does_not_mark_scope_scraped(self):
        manager = AsyncMock()
        manager.torrents = {}
        lock = AsyncMock()
        lock.acquire.return_value = True

        async def run(operation):
            return await operation

        lock.run.side_effect = run

        with (
            patch(
                "comet.services.media_search.DistributedLock",
                return_value=lock,
            ),
            patch(
                "comet.services.media_search.mark_scope_scraped",
                new=AsyncMock(),
            ) as mark_scraped,
        ):
            await background_scrape(
                manager,
                media_id="tt123",
                debrid_entries=[],
                ip="127.0.0.1",
                session=None,
            )

        manager.scrape_torrents.assert_awaited_once_with(ScrapeContext.BACKGROUND)
        mark_scraped.assert_not_awaited()
        lock.release.assert_awaited_once()

    async def test_populated_cache_refresh_marks_scope_scraped(self):
        manager = AsyncMock()
        manager.torrents = {"a" * 40: {}}
        lock = AsyncMock()
        lock.acquire.return_value = True

        async def run(operation):
            return await operation

        lock.run.side_effect = run

        with (
            patch(
                "comet.services.media_search.DistributedLock",
                return_value=lock,
            ),
            patch(
                "comet.services.media_search.mark_scope_scraped",
                new=AsyncMock(),
            ) as mark_scraped,
        ):
            await background_scrape(
                manager,
                media_id="tt123",
                debrid_entries=[],
                ip="127.0.0.1",
                session=None,
            )

        manager.scrape_torrents.assert_awaited_once_with(ScrapeContext.BACKGROUND)
        mark_scraped.assert_awaited_once_with("tt123")
        lock.release.assert_awaited_once()

    async def test_background_scrape_failure_surfaces_after_lock_release(self):
        manager = AsyncMock()
        manager.scrape_torrents.side_effect = RuntimeError("scrape failed")
        lock = AsyncMock()
        lock.acquire.return_value = True

        async def run(operation):
            return await operation

        lock.run.side_effect = run

        with (
            patch(
                "comet.services.media_search.DistributedLock",
                return_value=lock,
            ),
            self.assertRaisesRegex(RuntimeError, "scrape failed"),
        ):
            await background_scrape(
                manager,
                media_id="tt123",
                debrid_entries=[],
                ip="127.0.0.1",
                session=None,
            )

        lock.release.assert_awaited_once()
