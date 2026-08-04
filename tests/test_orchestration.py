import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from RTN import ParsedData

from comet.core.scrape import ScrapeContext
from comet.core.sources import (
    MAX_SIGNED_BIGINT,
    LocatorKind,
    LocatorPolicy,
    RealNzbRef,
    ReleaseCandidate,
    ReleaseScope,
    TorrentLocator,
    TransportKind,
)
from comet.discovery.manager import DiscoveryResult
from comet.discovery.models import DiscoveryBatch, MediaQuery
from comet.discovery.torrent_repository import (
    torrent_candidate_from_runtime,
    torrent_candidate_from_scrape_result,
)
from comet.services.orchestration import (
    TorrentResultAccumulator,
    settings,
    torrent_adapter_registry,
    torrent_update_queue,
)


class TorrentOrchestrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_scrape_rejects_candidate_outside_torrent_plan(self):
        manager = TorrentResultAccumulator(
            media_type="movie",
            media_full_id="tt123",
            media_only_id="tt123",
            title="Movie",
            year=2026,
            year_end=None,
            season=None,
            episode=None,
            aliases={},
            remove_adult_content=False,
        )
        usenet_candidate = ReleaseCandidate(
            candidate_id="usenet-candidate",
            media_id="tt123",
            scope=ReleaseScope.MOVIE,
            transport=TransportKind.USENET,
            title="Movie.2026.1080p.WEB-DL",
            locators=(
                RealNzbRef(
                    locator_id="nzb",
                    kind=LocatorKind.REAL_NZB,
                    policy=LocatorPolicy(frozenset({"comet_native_usenet"})),
                    adapter_configuration_id="source",
                    remote_guid="guid",
                ),
            ),
            size=1000,
            source="unexpected",
        )
        captured = {}

        class CaptureCoordinator:
            def __init__(self, adapters, **kwargs):
                captured["adapters"] = adapters
                captured["options"] = kwargs

            async def search(self, query, plan, **kwargs):
                captured["query"] = query
                captured["plan"] = plan
                captured["search_options"] = kwargs
                return DiscoveryResult((usenet_candidate,), (), plan)

        class TorrentAdapter:
            discovery_timeout = 120.0

        with (
            patch.object(
                torrent_adapter_registry,
                "build_adapters",
                return_value={"torrent-source": TorrentAdapter()},
            ),
            patch(
                "comet.services.orchestration.SearchCoordinator",
                CaptureCoordinator,
            ),
            patch.object(manager, "filter_manager", new=AsyncMock()) as filter_manager,
            patch.object(manager, "cache_torrents", new=AsyncMock()),
        ):
            with self.assertRaisesRegex(ValueError, "non-torrent candidate"):
                await manager.scrape_torrents(ScrapeContext.BACKGROUND)

        plan = captured["plan"]
        self.assertEqual(plan.transports, frozenset({TransportKind.BITTORRENT}))
        self.assertEqual(plan.discovery_source_ids, ("torrent-source",))
        self.assertEqual(
            [provider.kind for provider in plan.providers],
            ["direct_torrent"],
        )
        self.assertIs(
            captured["search_options"]["work_class"],
            ScrapeContext.BACKGROUND,
        )
        self.assertEqual(captured["options"]["hard_timeout"], 120.0)
        filter_manager.assert_not_awaited()
        self.assertEqual(manager.torrents, {})

    async def test_discovered_torrent_candidate_uses_existing_filter_contract(self):
        manager = TorrentResultAccumulator(
            media_type="movie",
            media_full_id="tt123",
            media_only_id="tt123",
            title="Movie",
            year=2026,
            year_end=None,
            season=None,
            episode=None,
            aliases={},
            remove_adult_content=False,
        )
        candidate = ReleaseCandidate(
            candidate_id="candidate",
            media_id="tt123",
            scope=ReleaseScope.MOVIE,
            transport=TransportKind.BITTORRENT,
            title="Movie.2026.1080p.WEB-DL",
            locators=(
                TorrentLocator(
                    locator_id="torrent",
                    kind=LocatorKind.TORRENT,
                    policy=LocatorPolicy(frozenset({"direct_torrent"})),
                    info_hash="a" * 40,
                ),
            ),
            size=1000,
            source="TorBox",
            transport_stats={"seeders": 4},
        )
        with patch.object(manager, "filter_manager", new=AsyncMock()) as filter_manager:
            await manager.ingest_release_candidates(
                "configured-discovery", (candidate,)
            )

        filter_manager.assert_awaited_once_with(
            [
                {
                    "title": "Movie.2026.1080p.WEB-DL",
                    "infoHash": "a" * 40,
                    "fileIndex": None,
                    "seeders": 4,
                    "size": 1000,
                    "tracker": "TorBox",
                    "sources": [],
                    "parsed": None,
                }
            ],
        )

    async def test_scrapers_receive_titles_selected_from_configured_languages(self):
        manager = TorrentResultAccumulator(
            media_type="movie",
            media_full_id="tt123",
            media_only_id="tt123",
            title="The Life Ahead",
            year=2020,
            year_end=None,
            season=None,
            episode=None,
            aliases={
                "lang:it": ["La vita davanti a sé"],
                "lang:fr": ["La Vie devant soi"],
            },
            remove_adult_content=False,
        )
        captured = []

        class CaptureAdapter:
            discovery_timeout = 8.0

            async def search(self, query, context):
                captured.append((query, context))
                return DiscoveryBatch(coverage=frozenset({"bittorrent"}))

        with (
            patch.object(settings, "INDEXER_LANGUAGES", ["it"]),
            patch.object(settings, "INDEXER_INCLUDE_CANONICAL_TITLE", False),
            patch.object(settings, "INDEXER_INCLUDE_ORIGINAL_TITLE", True),
            patch.object(
                torrent_adapter_registry,
                "build_adapters",
                return_value={"capture": CaptureAdapter()},
            ),
            patch.object(
                torrent_adapter_registry,
                "branch_fingerprints",
                return_value=None,
            ),
            patch.object(manager, "cache_torrents"),
        ):
            await manager.scrape_torrents(ScrapeContext.LIVE)

        self.assertEqual(
            captured[0][0].search_titles,
            ("La vita davanti a se",),
        )
        self.assertIs(captured[0][1].work_class, ScrapeContext.LIVE)

    async def test_filter_manager_accepts_empty_results(self):
        manager = TorrentResultAccumulator(
            media_type="movie",
            media_full_id="tt123",
            media_only_id="tt123",
            title="Movie",
            year=2026,
            year_end=None,
            season=None,
            episode=None,
            aliases={},
            remove_adult_content=False,
        )
        await manager.filter_manager([])

    async def test_filter_manager_exposes_invalid_scraper_results(self):
        manager = TorrentResultAccumulator(
            media_type="movie",
            media_full_id="tt123",
            media_only_id="tt123",
            title="Movie",
            year=2026,
            year_end=None,
            season=None,
            episode=None,
            aliases={},
            remove_adult_content=False,
        )
        with self.assertRaises(TypeError):
            await manager.filter_manager([None])

        self.assertEqual(manager.ready_to_cache, [])

    def test_torrent_projections_preserve_values_without_validation(self):
        torrent = {
            "title": "Movie.2026.1080p.WEB-DL",
            "infoHash": "a" * 40,
            "fileIndex": None,
            "seeders": 1,
            "size": MAX_SIGNED_BIGINT + 1,
            "tracker": "Test",
            "sources": [],
            "parsed": None,
        }
        runtime = torrent_candidate_from_runtime(
            torrent["infoHash"],
            torrent,
            media_id="tt123",
            scope=ReleaseScope.MOVIE,
            season_norm=-1,
            episode_norm=-1,
        )
        discovery = torrent_candidate_from_scrape_result(
            torrent,
            MediaQuery("tt123", "movie"),
        )

        self.assertEqual(runtime.size, MAX_SIGNED_BIGINT + 1)
        self.assertEqual(discovery.size, MAX_SIGNED_BIGINT + 1)

    async def test_scrape_waits_until_cache_updates_are_enqueued(self):
        manager = TorrentResultAccumulator(
            media_type="movie",
            media_full_id="tt123",
            media_only_id="tt123",
            title="Title",
            year=2024,
            year_end=None,
            season=None,
            episode=None,
            aliases={},
            remove_adult_content=False,
        )
        cache_started = asyncio.Event()
        release_cache = asyncio.Event()

        async def cache_torrents(*, defer=True):
            self.assertTrue(defer)
            cache_started.set()
            await release_cache.wait()

        with (
            patch.object(torrent_adapter_registry, "build_adapters", return_value={}),
            patch.object(manager, "cache_torrents", new=cache_torrents),
        ):
            scrape = asyncio.create_task(manager.scrape_torrents(ScrapeContext.LIVE))
            await cache_started.wait()
            await asyncio.sleep(0)
            self.assertFalse(scrape.done())
            release_cache.set()
            await scrape

    async def test_live_torrent_cache_write_is_registered_after_response(self):
        scheduled = []
        manager = TorrentResultAccumulator(
            media_type="movie",
            media_full_id="tt123",
            media_only_id="tt123",
            title="Title",
            year=2024,
            year_end=None,
            season=None,
            episode=None,
            aliases={},
            remove_adult_content=False,
            cache_task_adder=lambda *args: scheduled.append(args),
        )
        manager.ready_to_cache = [
            {
                "infoHash": "a" * 40,
                "fileIndex": 0,
                "title": "Title.2024.mkv",
                "size": 100,
                "seeders": 1,
                "tracker": "test",
                "sources": [],
                "parsed": ParsedData(raw_title="Title.2024.mkv"),
            }
        ]

        with patch.object(
            torrent_update_queue,
            "add_torrent_infos",
            new=AsyncMock(),
        ) as persist:
            await manager.cache_torrents()

            persist.assert_not_awaited()
            self.assertEqual(len(scheduled), 1)
            await scheduled[0][0](*scheduled[0][1:])

        persist.assert_awaited_once()
        self.assertEqual(persist.await_args.args[1], "tt123")

    async def test_account_cache_enqueues_only_missing_public_rows(self):
        scheduled = []
        manager = TorrentResultAccumulator(
            media_type="movie",
            media_full_id="tt123",
            media_only_id="tt123",
            title="Title",
            year=2024,
            year_end=None,
            season=None,
            episode=None,
            aliases={},
            remove_adult_content=False,
            cache_task_adder=lambda *args: scheduled.append(args),
        )
        torrents = [
            {
                "infoHash": info_hash,
                "fileIndex": index,
                "title": f"Title.2024.{index}.mkv",
                "size": 100 + index,
                "seeders": 0,
                "tracker": "DebridAccount|torbox",
                "sources": [],
                "parsed": ParsedData(raw_title=f"Title.2024.{index}.mkv"),
            }
            for index, info_hash in enumerate(("a" * 40, "b" * 40))
        ]

        with (
            patch(
                "comet.services.orchestration.TorrentReleaseRepository."
                "existing_media_keys",
                new=AsyncMock(return_value={("a" * 40, None, None)}),
            ) as existing,
            patch.object(
                torrent_update_queue,
                "add_torrent_infos",
                new=AsyncMock(),
            ) as persist,
        ):
            await manager.cache_torrents(
                torrents,
                only_missing=True,
            )

            existing.assert_not_awaited()
            persist.assert_not_awaited()
            self.assertEqual(len(scheduled), 1)
            await scheduled[0][0](*scheduled[0][1:])

        existing.assert_awaited_once_with("tt123", ("a" * 40, "b" * 40))
        persist.assert_awaited_once()
        self.assertEqual(persist.await_args.args[1], "tt123")
        self.assertEqual(
            [row["info_hash"] for row in persist.await_args.args[0]],
            ["b" * 40],
        )

    async def test_cache_media_id_reads_start_concurrently(self):
        manager = TorrentResultAccumulator(
            media_type="movie",
            media_full_id="tt123",
            media_only_id="tt123",
            title="Title",
            year=2024,
            year_end=None,
            season=None,
            episode=None,
            aliases={},
            remove_adult_content=False,
        )
        manager.cache_media_ids = ["tt123", "kitsu:456"]
        primary_started = asyncio.Event()
        alternate_started = asyncio.Event()

        async def fetch_rows(media_id):
            if media_id == "tt123":
                primary_started.set()
                await alternate_started.wait()
            else:
                alternate_started.set()
                await primary_started.wait()
            return []

        with patch.object(manager, "_fetch_cached_rows", new=fetch_rows):
            await asyncio.wait_for(manager.get_cached_torrents(), timeout=1)

        self.assertTrue(primary_started.is_set())
        self.assertTrue(alternate_started.is_set())

    async def test_corrupt_cached_parse_does_not_discard_valid_sibling(self):
        manager = TorrentResultAccumulator(
            media_type="movie",
            media_full_id="tt123",
            media_only_id="tt123",
            title="Title",
            year=2024,
            year_end=None,
            season=None,
            episode=None,
            aliases={},
            remove_adult_content=False,
        )
        base_row = {
            "file_index": 0,
            "seeders": 1,
            "size": 100,
            "tracker": "cache",
            "sources_json": '["tracker:first"]',
            "episode": None,
            "updated_at": 1,
        }
        rows = [
            {
                **base_row,
                "info_hash": "a" * 40,
                "title": "Corrupt.mkv",
                "parsed_json": "not-json",
            },
            {
                **base_row,
                "info_hash": "b" * 40,
                "title": "Valid.mkv",
                "parsed_json": '{"raw_title":"Valid.mkv"}',
            },
        ]

        with patch.object(manager, "_fetch_cached_rows", return_value=rows):
            await manager.get_cached_torrents()

        self.assertEqual(set(manager.torrents), {"b" * 40})

    async def test_empty_cached_parse_is_an_absent_selection(self):
        manager = TorrentResultAccumulator(
            media_type="movie",
            media_full_id="tt123",
            media_only_id="tt123",
            title="Title",
            year=2024,
            year_end=None,
            season=None,
            episode=None,
            aliases={},
            remove_adult_content=False,
        )
        rows = [
            {
                "file_index": None,
                "seeders": 1,
                "size": 100,
                "tracker": "cache",
                "sources_json": "[]",
                "episode": None,
                "updated_at": 1,
                "info_hash": "a" * 40,
                "title": "Unparsed.mkv",
                "parsed_json": "{}",
            }
        ]

        with patch.object(manager, "_fetch_cached_rows", return_value=rows):
            await manager.get_cached_torrents()

        self.assertEqual(manager.torrents, {})

    async def test_series_cache_projects_episode_children_without_losing_pack_title(
        self,
    ):
        manager = TorrentResultAccumulator(
            media_type="series",
            media_full_id="tt123",
            media_only_id="tt123",
            title="Show",
            year=2024,
            year_end=None,
            season=None,
            episode=None,
            aliases={},
            remove_adult_content=False,
        )
        pack_hash = "a" * 40
        episode_hash = "b" * 40
        base_row = {
            "seeders": 1,
            "size": 100,
            "tracker": "cache",
            "sources_json": "[]",
            "updated_at": 1,
        }
        rows = [
            {
                **base_row,
                "info_hash": pack_hash,
                "file_index": None,
                "title": "Show.S01.COMPLETE.mkv",
                "episode": None,
                "parsed_json": (
                    '{"raw_title":"Show.S01.COMPLETE.mkv","seasons":[1],"episodes":[]}'
                ),
            },
            {
                **base_row,
                "info_hash": pack_hash,
                "file_index": 1,
                "title": "Show.S01E01.mkv",
                "episode": 1,
                "parsed_json": (
                    '{"raw_title":"Show.S01E01.mkv","seasons":[1],"episodes":[1]}'
                ),
            },
            {
                **base_row,
                "info_hash": episode_hash,
                "file_index": 0,
                "title": "Show.S02E03.mkv",
                "episode": 3,
                "parsed_json": (
                    '{"raw_title":"Show.S02E03.mkv","seasons":[2],"episodes":[3]}'
                ),
            },
        ]

        with patch.object(manager, "_fetch_cached_rows", return_value=rows):
            await manager.get_cached_torrents()

        self.assertEqual(set(manager.torrents), {pack_hash, episode_hash})
        self.assertEqual(
            manager.torrents[pack_hash]["title"],
            "Show.S01.COMPLETE.mkv",
        )
        self.assertEqual(
            manager.torrents[episode_hash]["title"],
            "Show.S02E03.mkv",
        )
