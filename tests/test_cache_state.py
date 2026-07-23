import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from databases import Database

import comet.services.cache_state as cache_state
from comet.core.db_router import ReplicaAwareDatabase
from comet.services.cache_state import CacheState, CacheStateManager, ScrapeDecision


class CacheStateManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_inherited_results_do_not_skip_first_exact_scope_scrape(self):
        manager = CacheStateManager("tt123:2")

        with (
            patch.object(manager, "register_demand", return_value=None),
            patch.object(manager, "_try_acquire_lock", return_value=True),
        ):
            result = await manager.check_and_decide(torrent_count=15)

        self.assertEqual(result.state, CacheState.FIRST_SEARCH)
        self.assertEqual(result.decision, ScrapeDecision.SCRAPE_FOREGROUND)
        self.assertTrue(result.has_cached_torrents)
        self.assertIsNone(result.scope_scraped_at)

    async def test_fresh_exact_scope_uses_reusable_results(self):
        manager = CacheStateManager("tt123:2")

        with (
            patch.object(manager, "register_demand", return_value=1_000),
            patch("comet.services.cache_state.time.time", return_value=1_001),
        ):
            result = await manager.check_and_decide(torrent_count=15)

        self.assertEqual(result.state, CacheState.FRESH)
        self.assertEqual(result.decision, ScrapeDecision.USE_CACHE)
        self.assertEqual(result.scope_scraped_at, 1_000)

    async def test_stale_exact_scope_refreshes_in_background(self):
        manager = CacheStateManager("tt123:2")

        with (
            patch.object(manager, "register_demand", return_value=1),
            patch("comet.services.cache_state.time.time", return_value=1_000_000),
        ):
            result = await manager.check_and_decide(torrent_count=15)

        self.assertEqual(result.state, CacheState.STALE)
        self.assertEqual(result.decision, ScrapeDecision.SCRAPE_BACKGROUND)


class ScopeCoveragePersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        path = Path(self.temp_dir.name) / "scope-coverage.db"
        self.database = ReplicaAwareDatabase(Database(f"sqlite+aiosqlite:///{path}"))
        await self.database.connect()
        await self.database.execute(
            """
            CREATE TABLE media_demand (
                media_id TEXT PRIMARY KEY,
                first_seen_at REAL NOT NULL,
                last_seen_at REAL NOT NULL,
                last_scraped_at REAL
            )
            """
        )

    async def asyncTearDown(self):
        await self.database.disconnect()
        self.temp_dir.cleanup()

    async def test_series_scrape_does_not_cover_season_scopes(self):
        series = CacheStateManager("tt123")
        season_one = CacheStateManager("tt123:1")
        season_two = CacheStateManager("tt123:2")

        with patch.object(cache_state, "database", self.database):
            self.assertIsNone(await series.register_demand())
            await cache_state.mark_scope_scraped("tt123")
            self.assertIsNotNone(await series.register_demand())
            self.assertIsNone(await season_two.register_demand())

            with patch.object(season_one, "_try_acquire_lock", return_value=True):
                first_season_request = await season_one.check_and_decide(15)
            self.assertEqual(
                first_season_request.decision,
                ScrapeDecision.SCRAPE_FOREGROUND,
            )

            await cache_state.mark_scope_scraped("tt123:1")
            repeated_season_request = await season_one.check_and_decide(15)
            self.assertEqual(
                repeated_season_request.decision,
                ScrapeDecision.USE_CACHE,
            )
