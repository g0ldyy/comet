import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from comet.core.scrape import ScrapeContext
from comet.scrapers.manager import ScraperManager, network_manager, settings
from comet.scrapers.models import ScrapeRequest
from comet.utils.network_manager import AsyncClientWrapper


class ScraperManagerTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_scrape_wrapper_reports_monotonic_response_time(self):
        manager = ScraperManager.__new__(ScraperManager)
        scraper = AsyncMock()
        scraper.scrape.return_value = [{"title": "Result"}]
        request = ScrapeRequest(
            media_type="movie",
            media_id="tt123",
            media_only_id="tt123",
            title="Title",
            year=2024,
            year_end=None,
            season=None,
            episode=None,
            context="live",
        )

        with patch(
            "comet.scrapers.manager.time.perf_counter",
            side_effect=(10.0, 10.875),
        ):
            name, results, response_time = await manager._scrape_wrapper(
                "Example", scraper, request, timeout=30
            )

        self.assertEqual(name, "Example")
        self.assertEqual(results, [{"title": "Result"}])
        self.assertEqual(response_time, 0.875)

    def test_timeout_resolution_uses_most_specific_override(self):
        manager = ScraperManager.__new__(ScraperManager)

        with (
            patch.object(settings, "LIVE_SCRAPE_TIMEOUT", 10.0),
            patch.object(settings, "BACKGROUND_SCRAPE_TIMEOUT", 60.0),
            patch.object(
                settings,
                "SCRAPER_TIMEOUT_OVERRIDES",
                {
                    "zilean": 90.0,
                    "jackett:live": 20.0,
                    "jackett:background": 120.0,
                },
            ),
        ):
            self.assertEqual(
                manager._resolve_timeout("Zilean", ScrapeContext.LIVE), 90.0
            )
            self.assertEqual(
                manager._resolve_timeout("Zilean", ScrapeContext.BACKGROUND), 90.0
            )
            self.assertEqual(
                manager._resolve_timeout("Jackett", ScrapeContext.LIVE), 20.0
            )
            self.assertEqual(
                manager._resolve_timeout("Jackett", ScrapeContext.BACKGROUND),
                120.0,
            )
            self.assertEqual(
                manager._resolve_timeout("Torrentio", ScrapeContext.LIVE), 10.0
            )
            self.assertEqual(
                manager._resolve_timeout("Torrentio", ScrapeContext.BACKGROUND),
                60.0,
            )

    def test_unknown_timeout_override_is_rejected(self):
        manager = ScraperManager.__new__(ScraperManager)
        manager.scrapers = {"ZileanScraper": object()}

        with (
            patch.object(
                settings,
                "SCRAPER_TIMEOUT_OVERRIDES",
                {"unknown": 10.0},
            ),
            self.assertRaisesRegex(ValueError, "unknown scrapers: unknown"),
        ):
            manager._validate_timeout_overrides()

    def test_scraper_clients_use_shared_http_request_timeout(self):
        with (
            patch.object(settings, "HTTP_CLIENT_TIMEOUT_TOTAL", 47.5),
            patch.object(settings, "GLOBAL_PROXY_URL", None),
        ):
            client = AsyncClientWrapper("Example")

        self.assertEqual(client.timeout, 47.5)

    async def test_slow_scraper_timeout_preserves_fast_results(self):
        slow_started = asyncio.Event()
        cancelled = asyncio.Event()

        class FastScraper:
            impersonate = None

            def __init__(self, manager, client, url=None):
                del manager, client, url

            async def scrape(self, request):
                del request
                await slow_started.wait()
                return [{"title": "Fast result"}]

        class SlowScraper:
            impersonate = None

            def __init__(self, manager, client, url=None):
                del manager, client, url

            async def scrape(self, request):
                del request
                slow_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        manager = ScraperManager.__new__(ScraperManager)
        manager.scrapers = {
            "NyaaScraper": FastScraper,
            "ZileanScraper": SlowScraper,
        }
        request = ScrapeRequest(
            media_type="movie",
            media_id="tt123",
            media_only_id="tt123",
            title="Title",
            context=ScrapeContext.BACKGROUND,
        )

        with (
            patch.object(settings, "SCRAPE_NYAA", True),
            patch.object(settings, "NYAA_ANIME_ONLY", False),
            patch.object(settings, "SCRAPE_ZILEAN", True),
            patch.object(settings, "BACKGROUND_SCRAPE_TIMEOUT", 1.0),
            patch.object(
                settings,
                "SCRAPER_TIMEOUT_OVERRIDES",
                {"zilean:background": 0.01},
            ),
            patch.object(network_manager, "get_client", return_value=object()),
            patch("comet.scrapers.manager.logger.warning") as warning,
        ):
            results = [result async for result in manager.scrape_all(request)]

        results_by_name = {name: torrents for name, torrents, _ in results}
        self.assertEqual(results_by_name["Nyaa"], [{"title": "Fast result"}])
        self.assertEqual(results_by_name["Zilean #1"], [])
        self.assertTrue(cancelled.is_set())
        warning.assert_called_once_with(
            "Scraper Zilean #1 timed out (context=background, budget=0.01s)"
        )

    async def test_closing_results_cancels_unfinished_scrapers(self):
        slow_started = asyncio.Event()
        slow_cancelled = asyncio.Event()

        class FastScraper:
            impersonate = None

            def __init__(self, manager, client, url=None):
                del manager, client, url

            async def scrape(self, request):
                del request
                await slow_started.wait()
                return []

        class SlowScraper:
            impersonate = None

            def __init__(self, manager, client, url=None):
                del manager, client, url

            async def scrape(self, request):
                del request
                slow_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    slow_cancelled.set()

        manager = ScraperManager.__new__(ScraperManager)
        manager.scrapers = {
            "NyaaScraper": FastScraper,
            "ZileanScraper": SlowScraper,
        }
        request = ScrapeRequest(
            media_type="movie",
            media_id="tt123",
            media_only_id="tt123",
            title="Title",
            year=2024,
            year_end=None,
            season=None,
            episode=None,
            context="live",
        )

        with (
            patch.object(settings, "SCRAPE_NYAA", True),
            patch.object(settings, "NYAA_ANIME_ONLY", False),
            patch.object(settings, "SCRAPE_ZILEAN", True),
            patch.object(network_manager, "get_client", return_value=object()),
        ):
            results = manager.scrape_all(request)
            await anext(results)
            await results.aclose()

        self.assertTrue(slow_cancelled.is_set())
