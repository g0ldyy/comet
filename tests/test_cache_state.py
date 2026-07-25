import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from databases import Database

from comet.core.db_router import ReplicaAwareDatabase
from comet.services import cache_state
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

    async def test_demand_write_failure_is_visible_and_non_fatal(self):
        manager = CacheStateManager("tt123:2")

        with (
            patch.object(
                cache_state.database,
                "fetch_one",
                side_effect=[None, RuntimeError("database unavailable")],
            ),
            patch.object(cache_state.logger, "opt") as logger_opt,
        ):
            result = await manager.register_demand()

        self.assertIsNone(result)
        logger_opt.assert_called_once_with(exception=True)
        logger_opt.return_value.warning.assert_called_once()


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

    async def test_fresh_demand_touches_are_throttled_without_losing_coverage(self):
        manager = CacheStateManager("tt123")

        async def get_scope():
            return await self.database.fetch_one(
                """
                SELECT last_seen_at, last_scraped_at
                FROM media_demand
                WHERE media_id = :media_id
                """,
                {"media_id": "tt123"},
            )

        with (
            patch.object(cache_state, "database", self.database),
            patch.object(
                cache_state.settings,
                "BACKGROUND_SCRAPER_DEMAND_LOOKBACK",
                100,
            ),
        ):
            with patch.object(cache_state.time, "time", return_value=1_000):
                self.assertIsNone(await manager.register_demand())
                await cache_state.mark_scope_scraped("tt123")

            with patch.object(cache_state.time, "time", return_value=1_025):
                self.assertEqual(await manager.register_demand(), 1_000)

            row = await get_scope()
            self.assertEqual(row["last_seen_at"], 1_000)
            self.assertEqual(row["last_scraped_at"], 1_000)

            with patch.object(cache_state.time, "time", return_value=1_051):
                self.assertEqual(await manager.register_demand(), 1_000)

            row = await get_scope()
            self.assertEqual(row["last_seen_at"], 1_051)
            self.assertEqual(row["last_scraped_at"], 1_000)
