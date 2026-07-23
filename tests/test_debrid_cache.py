import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from databases import Database

from comet.services import debrid_cache
from comet.utils.parsing import MediaScope


class DebridCacheTaskTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await debrid_cache.shutdown_cache_writes()

    async def test_shutdown_drains_scheduled_cache_writes(self):
        started = asyncio.Event()
        finish = asyncio.Event()

        async def write_cache(service, availability):
            self.assertEqual(service, "realdebrid")
            self.assertEqual(availability, [{"info_hash": "hash"}])
            started.set()
            await finish.wait()

        with patch.object(debrid_cache, "cache_availability", new=write_cache):
            task = debrid_cache.schedule_cache_availability(
                "realdebrid", [{"info_hash": "hash"}]
            )
            await started.wait()
            shutdown = asyncio.create_task(debrid_cache.shutdown_cache_writes())
            await asyncio.sleep(0)
            self.assertFalse(shutdown.done())
            finish.set()
            await shutdown

        self.assertTrue(task.done())
        self.assertFalse(debrid_cache._cache_write_tasks)

    async def test_scheduled_failure_is_observed_and_removed(self):
        write_cache = AsyncMock(side_effect=RuntimeError("database unavailable"))
        fake_logger = MagicMock()

        with (
            patch.object(debrid_cache, "cache_availability", new=write_cache),
            patch.object(debrid_cache, "logger", new=fake_logger),
        ):
            task = debrid_cache.schedule_cache_availability("alldebrid", [])
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)

        self.assertFalse(debrid_cache._cache_write_tasks)
        fake_logger.warning.assert_called_once()


class DebridCachePersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_conflict_scopes_are_written_once_with_last_value(self):
        first = {
            "info_hash": "hash",
            "index": 1,
            "title": "first.mkv",
            "season": 1,
            "episode": 2,
            "size": 10,
            "parsed": None,
        }
        selected = {**first, "index": 3, "title": "selected.mkv", "size": 30}

        with patch.object(
            debrid_cache.database, "execute_many", new=AsyncMock()
        ) as execute:
            await debrid_cache.cache_availability(
                "realdebrid", [first, selected, {**first, "episode": 3}]
            )

        execute.assert_awaited_once()
        _, rows = execute.await_args.args
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["file_index"], "3")
        self.assertEqual(rows[0]["title"], "selected.mkv")
        self.assertEqual(rows[1]["episode"], 3)

    async def test_empty_availability_skips_database_call(self):
        with patch.object(
            debrid_cache.database, "execute_many", new=AsyncMock()
        ) as execute:
            await debrid_cache.cache_availability("realdebrid", [])

        execute.assert_not_awaited()


class DebridAvailabilityScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        path = Path(self.temp_dir.name) / "debrid-cache.db"
        self.database = Database(f"sqlite+aiosqlite:///{path}")
        await self.database.connect()
        await self.database.execute(
            """
            CREATE TABLE debrid_availability (
                debrid_service TEXT NOT NULL,
                info_hash TEXT NOT NULL,
                season_norm INTEGER NOT NULL,
                episode_norm INTEGER NOT NULL,
                file_index TEXT,
                title TEXT,
                size BIGINT,
                parsed_json TEXT,
                updated_at REAL NOT NULL
            )
            """
        )
        await self.database.execute(
            """
            CREATE UNIQUE INDEX unq_debrid_scope
            ON debrid_availability (
                debrid_service,
                info_hash,
                season_norm,
                episode_norm
            )
            """
        )

        self.episode_pack_hash = "a" * 40
        self.pack_row_hash = "b" * 40
        self.other_season_hash = "c" * 40
        self.movie_hash = "d" * 40
        self.offcloud_hash = "e" * 40
        now = time.time()
        rows = [
            self._row(
                "torbox",
                self.episode_pack_hash,
                season=2,
                episode=1,
                title="Show.S02E01.mkv",
                file_index="1",
                updated_at=now,
            ),
            self._row(
                "torbox",
                self.episode_pack_hash,
                season=2,
                episode=2,
                title="Show.S02E02.mkv",
                file_index="2",
                updated_at=now,
            ),
            self._row(
                "torbox",
                self.pack_row_hash,
                season=2,
                episode=None,
                title="Show.S02.COMPLETE.mkv",
                file_index="3",
                updated_at=now,
            ),
            self._row(
                "torbox",
                self.other_season_hash,
                season=3,
                episode=1,
                title="Show.S03E01.mkv",
                file_index="4",
                updated_at=now,
            ),
            self._row(
                "torbox",
                self.movie_hash,
                season=None,
                episode=None,
                title="Movie.2026.mkv",
                file_index="5",
                updated_at=now,
            ),
            self._row(
                "torbox",
                self.movie_hash,
                season=None,
                episode=1,
                title="Unrelated.E01.mkv",
                file_index="6",
                updated_at=now,
            ),
            self._row(
                "offcloud",
                self.offcloud_hash,
                season=None,
                episode=None,
                title=None,
                file_index=None,
                updated_at=now,
            ),
        ]
        await self.database.execute_many(
            """
            INSERT INTO debrid_availability (
                debrid_service,
                info_hash,
                season_norm,
                episode_norm,
                file_index,
                title,
                size,
                parsed_json,
                updated_at
            ) VALUES (
                :debrid_service,
                :info_hash,
                :season_norm,
                :episode_norm,
                :file_index,
                :title,
                :size,
                :parsed_json,
                :updated_at
            )
            """,
            rows,
        )

    async def asyncTearDown(self):
        await self.database.disconnect()
        self.temp_dir.cleanup()

    @staticmethod
    def _row(
        service,
        info_hash,
        *,
        season,
        episode,
        title,
        file_index,
        updated_at,
    ):
        return {
            "debrid_service": service,
            "info_hash": info_hash,
            "season_norm": -1 if season is None else season,
            "episode_norm": -1 if episode is None else episode,
            "file_index": file_index,
            "title": title,
            "size": 100,
            "parsed_json": None,
            "updated_at": updated_at,
        }

    async def test_season_scope_matches_episode_rows_once_per_hash(self):
        with patch.object(debrid_cache, "database", self.database):
            rows = await debrid_cache.get_cached_availability(
                "torbox",
                [
                    self.episode_pack_hash,
                    self.pack_row_hash,
                    self.other_season_hash,
                    self.movie_hash,
                ],
                MediaScope.SEASON,
                season=2,
                episode=None,
            )

        self.assertEqual(
            {row["info_hash"] for row in rows},
            {self.episode_pack_hash, self.pack_row_hash},
        )
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(set(row) == {"info_hash"} for row in rows))

    async def test_episode_and_movie_scopes_remain_exact_and_enrichable(self):
        with patch.object(debrid_cache, "database", self.database):
            episode_rows = await debrid_cache.get_cached_availability(
                "torbox",
                [self.episode_pack_hash],
                MediaScope.EPISODE,
                season=2,
                episode=2,
            )
            movie_rows = await debrid_cache.get_cached_availability(
                "torbox",
                [self.movie_hash],
                MediaScope.MOVIE,
                season=None,
                episode=None,
            )

        self.assertEqual(len(episode_rows), 1)
        self.assertEqual(episode_rows[0]["title"], "Show.S02E02.mkv")
        self.assertEqual(episode_rows[0]["file_index"], "2")
        self.assertEqual(len(movie_rows), 1)
        self.assertEqual(movie_rows[0]["title"], "Movie.2026.mkv")
        self.assertEqual(movie_rows[0]["file_index"], "5")

    async def test_offcloud_unscoped_availability_supports_season_scope(self):
        with patch.object(debrid_cache, "database", self.database):
            rows = await debrid_cache.get_cached_availability(
                "offcloud",
                [self.offcloud_hash],
                MediaScope.SEASON,
                season=2,
                episode=None,
            )

        self.assertEqual([row["info_hash"] for row in rows], [self.offcloud_hash])

    async def test_series_scope_matches_all_cached_child_scopes_once_per_hash(self):
        with patch.object(debrid_cache, "database", self.database):
            rows = await debrid_cache.get_cached_availability(
                "torbox",
                [
                    self.episode_pack_hash,
                    self.pack_row_hash,
                    self.other_season_hash,
                ],
                MediaScope.SERIES,
            )

        self.assertEqual(
            {row["info_hash"] for row in rows},
            {
                self.episode_pack_hash,
                self.pack_row_hash,
                self.other_season_hash,
            },
        )
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(set(row) == {"info_hash"} for row in rows))


if __name__ == "__main__":
    unittest.main()
