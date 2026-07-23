import unittest
from unittest.mock import AsyncMock, patch

from comet.api.endpoints.stream import background_scrape
from comet.core.scrape import ScrapeContext


class StreamScrapeContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_cache_refresh_uses_background_context(self):
        manager = AsyncMock()
        manager.torrents = {}
        lock = AsyncMock()
        lock.acquire.return_value = True

        async def run(operation):
            return await operation

        lock.run.side_effect = run

        with patch(
            "comet.api.endpoints.stream.DistributedLock",
            return_value=lock,
        ):
            await background_scrape(
                manager,
                media_id="tt123",
                debrid_entries=[],
                ip="127.0.0.1",
                session=None,
            )

        manager.scrape_torrents.assert_awaited_once_with(ScrapeContext.BACKGROUND)
        lock.release.assert_awaited_once()
