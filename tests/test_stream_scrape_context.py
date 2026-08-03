import unittest
from unittest.mock import AsyncMock, patch

import orjson
from fastapi import BackgroundTasks
from starlette.requests import Request

from comet.api.endpoints.stream import stream
from comet.core.models import settings
from comet.core.scrape import ScrapeContext
from comet.services.media_search import (
    MediaSearchResult,
    MediaSearchStatus,
    background_scrape,
    episode_matching_policy,
)


class StreamScrapeContextTests(unittest.IsolatedAsyncioTestCase):
    def test_debrid_only_episode_search_keeps_matching_season_packs(self):
        self.assertEqual(
            episode_matching_policy(
                "series",
                "tt1234567",
                3,
                6,
                has_debrid=True,
                enable_torrent=False,
            ),
            (True, False),
        )

    def test_direct_episode_search_requires_an_explicit_episode(self):
        self.assertEqual(
            episode_matching_policy(
                "series",
                "tt1234567",
                3,
                6,
                has_debrid=True,
                enable_torrent=True,
            ),
            (True, True),
        )

    async def test_inflight_timeout_keeps_the_stremio_retry_notice(self):
        request = Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "https",
                "path": "/stream/movie/tt123.json",
                "raw_path": b"/stream/movie/tt123.json",
                "query_string": b"",
                "headers": [],
                "client": ("127.0.0.1", 1234),
                "server": ("example.test", 443),
            }
        )
        config = {
            "_debridEntries": [],
            "_enableTorrent": True,
            "schemaVersion": 1,
            "scrapeDebridAccountTorrents": False,
        }
        with (
            patch.object(settings, "HTTP_CACHE_ENABLED", True),
            patch("comet.api.endpoints.stream.config_check", return_value=config),
            patch(
                "comet.api.endpoints.stream.search_media",
                new=AsyncMock(return_value=MediaSearchResult(MediaSearchStatus.BUSY)),
            ),
        ):
            response = await stream(
                request,
                "movie",
                "tt123",
                BackgroundTasks(),
            )

        payload = orjson.loads(response.body)
        self.assertEqual(len(payload["streams"]), 1)
        self.assertIn("Scraping in progress", payload["streams"][0]["description"])
        self.assertIn("no-store", response.headers["cache-control"])

    async def test_empty_cache_refresh_does_not_mark_scope_scraped(self):
        manager = AsyncMock()
        manager.torrents = {}

        with (
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

    async def test_populated_cache_refresh_marks_scope_scraped(self):
        manager = AsyncMock()
        manager.torrents = {"a" * 40: {}}

        with (
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

    async def test_background_scrape_failure_surfaces(self):
        manager = AsyncMock()
        manager.scrape_torrents.side_effect = RuntimeError("scrape failed")

        with self.assertRaisesRegex(RuntimeError, "scrape failed"):
            await background_scrape(
                manager,
                media_id="tt123",
                debrid_entries=[],
                ip="127.0.0.1",
                session=None,
            )
