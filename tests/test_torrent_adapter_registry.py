import asyncio
import unittest
import uuid
from unittest.mock import patch

from comet.core.scrape import ScrapeContext
from comet.discovery.adapters.torrent.jackett import JackettScraper
from comet.discovery.adapters.torrent.prowlarr import ProwlarrScraper
from comet.discovery.adapters.torrent.torrentio import TorrentioScraper
from comet.discovery.adapters.torrent.zilean import ZileanScraper
from comet.discovery.models import DiscoveryContext, MediaQuery
from comet.discovery.torrent_base import TorrentDiscoveryAdapter
from comet.discovery.torrent_registry import (
    TorrentAdapterRegistry,
    settings,
)
from comet.utils.network_manager import AsyncClientWrapper


class TorrentAdapterRegistryTests(unittest.IsolatedAsyncioTestCase):
    def test_registered_adapter_ids_are_stable_persistable_uuids(self):
        class ExampleScraper(TorrentDiscoveryAdapter):
            async def scrape(self, request):
                del request
                return []

        first = {}
        repeated = {}
        TorrentAdapterRegistry._register_adapter(
            first, "Example", ExampleScraper(None, None), 8
        )
        TorrentAdapterRegistry._register_adapter(
            first, "Example #2", ExampleScraper(None, None), 8
        )
        TorrentAdapterRegistry._register_adapter(
            repeated, "Example", ExampleScraper(None, None), 8
        )
        TorrentAdapterRegistry._register_adapter(
            repeated, "Example #2", ExampleScraper(None, None), 8
        )

        self.assertEqual(tuple(first), tuple(repeated))
        self.assertEqual(len(first), 2)
        self.assertTrue(
            all(str(uuid.UUID(source_id)) == source_id for source_id in first)
        )

    def test_branch_fingerprints_are_stable_and_context_scoped(self):
        class ExampleScraper(TorrentDiscoveryAdapter):
            async def scrape(self, request):
                del request
                return []

        adapters = {"server-torrent:example": ExampleScraper(None, None)}

        first = TorrentAdapterRegistry.branch_fingerprints(adapters, ScrapeContext.LIVE)
        repeated = TorrentAdapterRegistry.branch_fingerprints(
            adapters, ScrapeContext.LIVE
        )
        background = TorrentAdapterRegistry.branch_fingerprints(
            adapters, ScrapeContext.BACKGROUND
        )

        self.assertEqual(first, repeated)
        self.assertNotEqual(first, background)
        identity = first[("server-torrent:example", "bittorrent")]
        self.assertTrue(identity.public_visibility)
        self.assertEqual(len(identity.fingerprint), 64)

    async def test_scraper_implements_the_discovery_adapter_contract(self):
        class ExampleScraper(TorrentDiscoveryAdapter):
            async def scrape(self, request):
                self.request = request
                return [
                    {
                        "title": "Movie.2026.1080p.WEB-DL",
                        "infoHash": "A" * 40,
                        "fileIndex": 3,
                        "seeders": 12,
                        "size": 1024,
                        "tracker": "Example",
                        "sources": ["tracker:https://tracker.example/announce"],
                    },
                ]

        adapter = ExampleScraper(None, None)
        result = await adapter.search(
            MediaQuery(
                "tt123",
                "movie",
                title_aliases=("Movie",),
                year=2026,
                request_media_id="tt123",
                title="Movie",
                search_titles=("Movie", "Film"),
            ),
            DiscoveryContext(
                frozenset({"bittorrent"}),
                configuration_id="example",
                work_class=ScrapeContext.BACKGROUND,
            ),
        )

        self.assertEqual(result.coverage, frozenset({"bittorrent"}))
        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        self.assertEqual(candidate.candidate_id, f"btih:{'a' * 40}")
        self.assertEqual(candidate.locators[0].file_index, 3)
        self.assertEqual(candidate.transport_stats["seeders"], 12)
        self.assertEqual(
            candidate.transport_stats["tracker_sources"],
            ("tracker:https://tracker.example/announce",),
        )
        self.assertIs(adapter.request.context, ScrapeContext.BACKGROUND)
        self.assertEqual(adapter.request.query_titles, ("Movie", "Film"))

    async def test_malformed_scraper_result_fails_the_source(self):
        class MalformedScraper(TorrentDiscoveryAdapter):
            async def scrape(self, request):
                del request
                return [{"title": "incomplete"}]

        with self.assertRaises(KeyError):
            await MalformedScraper(None, None).search(
                MediaQuery("tt123", "movie", title="Movie"),
                DiscoveryContext(frozenset({"bittorrent"})),
            )

    async def test_adapter_reports_monotonic_response_time(self):
        class ExampleScraper(TorrentDiscoveryAdapter):
            async def scrape(self, request):
                del request
                return []

        adapter = ExampleScraper(None, None)
        adapter.discovery_name = "Example"
        adapter.discovery_timeout = 30
        with (
            patch(
                "comet.discovery.torrent_base.time.perf_counter",
                side_effect=(10.0, 10.875),
            ),
            patch("comet.discovery.torrent_base.metrics.observe_scraper") as observe,
        ):
            await adapter.search(
                MediaQuery("tt123", "movie", title="Title"),
                DiscoveryContext(frozenset({"bittorrent"})),
            )

        observe.assert_called_once_with(
            "Example",
            "live",
            "success",
            0.875,
            0,
        )

    def test_timeout_resolution_uses_most_specific_override(self):
        manager = TorrentAdapterRegistry.__new__(TorrentAdapterRegistry)

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
                manager._resolve_timeout(ZileanScraper, ScrapeContext.LIVE), 90.0
            )
            self.assertEqual(
                manager._resolve_timeout(ZileanScraper, ScrapeContext.BACKGROUND),
                90.0,
            )
            self.assertEqual(
                manager._resolve_timeout(JackettScraper, ScrapeContext.LIVE), 20.0
            )
            self.assertEqual(
                manager._resolve_timeout(JackettScraper, ScrapeContext.BACKGROUND),
                120.0,
            )
            self.assertEqual(
                manager._resolve_timeout(TorrentioScraper, ScrapeContext.LIVE), 10.0
            )
            self.assertEqual(
                manager._resolve_timeout(TorrentioScraper, ScrapeContext.BACKGROUND),
                60.0,
            )

    def test_indexer_defaults_preserve_the_context_budget_after_initialization(self):
        manager = TorrentAdapterRegistry.__new__(TorrentAdapterRegistry)

        with (
            patch.object(settings, "LIVE_SCRAPE_TIMEOUT", 10.0),
            patch.object(settings, "BACKGROUND_SCRAPE_TIMEOUT", 60.0),
            patch.object(settings, "INDEXER_MANAGER_WAIT_TIMEOUT", 30),
            patch.object(settings, "SCRAPER_TIMEOUT_OVERRIDES", {}),
        ):
            self.assertEqual(
                manager._resolve_timeout(JackettScraper, ScrapeContext.LIVE), 40.0
            )
            self.assertEqual(
                manager._resolve_timeout(ProwlarrScraper, ScrapeContext.BACKGROUND),
                90.0,
            )
            self.assertEqual(
                manager._resolve_timeout(ZileanScraper, ScrapeContext.LIVE), 10.0
            )

    def test_indexer_override_replaces_the_derived_default(self):
        manager = TorrentAdapterRegistry.__new__(TorrentAdapterRegistry)

        with (
            patch.object(settings, "LIVE_SCRAPE_TIMEOUT", 10.0),
            patch.object(settings, "INDEXER_MANAGER_WAIT_TIMEOUT", 30),
            patch.object(
                settings,
                "SCRAPER_TIMEOUT_OVERRIDES",
                {"jackett": 15.0, "prowlarr:live": 20.0},
            ),
        ):
            self.assertEqual(
                manager._resolve_timeout(JackettScraper, ScrapeContext.LIVE), 15.0
            )
            self.assertEqual(
                manager._resolve_timeout(ProwlarrScraper, ScrapeContext.LIVE), 20.0
            )

    def test_scraper_clients_use_shared_http_request_timeout(self):
        with (
            patch.object(settings, "HTTP_CLIENT_TIMEOUT_TOTAL", 47.5),
            patch.object(settings, "GLOBAL_PROXY_URL", None),
        ):
            client = AsyncClientWrapper()

        self.assertEqual(client.timeout, 47.5)

    async def test_adapter_timeout_is_bounded_and_cancels_work(self):
        cancelled = asyncio.Event()

        class SlowScraper(TorrentDiscoveryAdapter):
            impersonate = None

            async def scrape(self, request):
                del request
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        adapter = SlowScraper(None, None)
        adapter.discovery_name = "Slow"
        adapter.discovery_timeout = 0.01
        with (
            patch("comet.discovery.torrent_base.metrics.observe_scraper") as observe,
        ):
            with self.assertRaises(TimeoutError):
                await adapter.search(
                    MediaQuery("tt123", "movie", title="Title"),
                    DiscoveryContext(
                        frozenset({"bittorrent"}),
                        work_class=ScrapeContext.BACKGROUND,
                    ),
                )

        self.assertTrue(cancelled.is_set())
        observe.assert_called_once()
