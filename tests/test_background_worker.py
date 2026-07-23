import asyncio
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from databases import Database

from comet.background_scraper.worker import (
    BackgroundScraperWorker,
    _serialize_run_row,
)
from comet.core.db_router import ReplicaAwareDatabase


class BackgroundWorkerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_insert_failure_clears_published_runtime_state(self):
        worker = BackgroundScraperWorker()
        worker._insert_run_row = AsyncMock(side_effect=RuntimeError("insert failed"))
        worker._reset_running_items = AsyncMock()
        worker._finalize_run_row = AsyncMock()

        with self.assertRaisesRegex(RuntimeError, "insert failed"):
            await worker._run_scraping_cycle()

        self.assertIsNone(worker.current_run_id)
        self.assertIsNone(worker.metadata_scraper)
        worker._reset_running_items.assert_not_awaited()
        worker._finalize_run_row.assert_not_awaited()

    async def test_reset_failure_still_finalizes_and_clears_runtime_state(self):
        worker = BackgroundScraperWorker()
        worker._insert_run_row = AsyncMock()
        worker._reset_running_items = AsyncMock(
            side_effect=RuntimeError("reset failed")
        )
        worker._finalize_run_row = AsyncMock()

        with self.assertRaisesRegex(RuntimeError, "reset failed"):
            await worker._run_scraping_cycle()

        worker._finalize_run_row.assert_awaited_once()
        self.assertIsNone(worker.current_run_id)
        self.assertIsNone(worker.metadata_scraper)

    async def test_finalize_failure_clears_runtime_state(self):
        worker = BackgroundScraperWorker()
        worker._insert_run_row = AsyncMock()
        worker._reset_running_items = AsyncMock()
        worker._finalize_run_row = AsyncMock(
            side_effect=RuntimeError("finalize failed")
        )

        with self.assertRaisesRegex(RuntimeError, "finalize failed"):
            await worker._run_scraping_cycle()

        self.assertIsNone(worker.current_run_id)
        self.assertIsNone(worker.metadata_scraper)

    async def test_continuous_runner_propagates_cancellation(self):
        worker = BackgroundScraperWorker()
        entered_sleep = asyncio.Event()

        async def blocked_sleep(_delay):
            entered_sleep.set()
            await asyncio.Event().wait()

        with (
            patch(
                "comet.background_scraper.worker.DistributedLock.acquire",
                new=AsyncMock(return_value=False),
            ),
            patch("comet.background_scraper.worker.asyncio.sleep", new=blocked_sleep),
        ):
            task = asyncio.create_task(worker._run_continuous())
            await entered_sleep.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertFalse(worker.is_running)


class BackgroundWorkerQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_snapshot_query_executes_against_sqlite(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            primary = Database(f"sqlite:///{Path(temp_dir) / 'queue.db'}")
            database = ReplicaAwareDatabase(primary)
            await database.connect()
            try:
                await database.execute(
                    """
                    CREATE TABLE background_scraper_items (
                        media_id TEXT PRIMARY KEY,
                        media_type TEXT NOT NULL,
                        next_retry_at REAL,
                        last_success_at REAL,
                        status TEXT NOT NULL,
                        consecutive_failures INTEGER NOT NULL,
                        created_at REAL
                    )
                    """
                )
                await database.execute(
                    """
                    CREATE TABLE background_scraper_episodes (
                        series_id TEXT NOT NULL,
                        season INTEGER NOT NULL,
                        episode INTEGER NOT NULL,
                        next_retry_at REAL,
                        last_success_at REAL,
                        status TEXT NOT NULL,
                        consecutive_failures INTEGER NOT NULL,
                        created_at REAL
                    )
                    """
                )
                await database.execute_many(
                    """
                    INSERT INTO background_scraper_items
                    (media_id, media_type, status, consecutive_failures, created_at)
                    VALUES (:media_id, :media_type, 'discovered', 0, :created_at)
                    """,
                    [
                        {
                            "media_id": "movie",
                            "media_type": "movie",
                            "created_at": 90.0,
                        },
                        {
                            "media_id": "series",
                            "media_type": "series",
                            "created_at": 85.0,
                        },
                    ],
                )
                await database.execute(
                    """
                    INSERT INTO background_scraper_episodes
                    (series_id, season, episode, status, consecutive_failures, created_at)
                    VALUES ('series', 1, 1, 'discovered', 0, 80.0)
                    """
                )

                with patch("comet.background_scraper.worker.database", database):
                    snapshot = await BackgroundScraperWorker()._fetch_queue_snapshot(
                        now=100.0
                    )
            finally:
                await database.disconnect()

        self.assertEqual(snapshot["total"], 3)
        self.assertEqual(snapshot["oldest_age_s"], 20.0)

    async def test_queue_snapshot_uses_one_primary_database_snapshot(self):
        worker = BackgroundScraperWorker()
        fetch_one = AsyncMock(
            return_value={
                "movie_count": 2,
                "series_count": 3,
                "oldest_item_ts": 90.0,
                "episode_count": 5,
                "oldest_episode_ts": 80.0,
            }
        )

        with patch("comet.background_scraper.worker.database.fetch_one", fetch_one):
            snapshot = await worker._fetch_queue_snapshot(now=100.0)

        self.assertEqual(
            snapshot,
            {
                "movies": 2,
                "series": 3,
                "episodes": 5,
                "total": 10,
                "oldest_age_s": 20.0,
            },
        )
        fetch_one.assert_awaited_once()
        self.assertTrue(fetch_one.await_args.kwargs["force_primary"])
        self.assertIn("CROSS JOIN episode_snapshot", fetch_one.await_args.args[0])

    async def test_queue_snapshot_rejects_corrupt_database_values(self):
        worker = BackgroundScraperWorker()
        valid = {
            "movie_count": 2,
            "series_count": 3,
            "oldest_item_ts": 90.0,
            "episode_count": 5,
            "oldest_episode_ts": 80.0,
        }
        invalid_rows = [
            None,
            valid | {"extra": 1},
            valid | {"movie_count": True},
            valid | {"series_count": -1},
            valid | {"episode_count": 1.5},
            valid | {"oldest_item_ts": 90},
            valid | {"oldest_item_ts": math.nan},
            valid | {"oldest_episode_ts": math.inf},
        ]

        for row in invalid_rows:
            with (
                self.subTest(row=row),
                patch(
                    "comet.background_scraper.worker.database.fetch_one",
                    new=AsyncMock(return_value=row),
                ),
                self.assertRaises((TypeError, ValueError)),
            ):
                await worker._fetch_queue_snapshot(now=100.0)

    async def test_requeue_dead_items_rolls_back_both_tables_on_failure(self):
        worker = BackgroundScraperWorker()

        class Transaction:
            def __init__(self):
                self.exit_error = None

            async def __aenter__(self):
                return self

            async def __aexit__(self, error_type, error, traceback):
                self.exit_error = error

        transaction = Transaction()
        fetch_val = AsyncMock(side_effect=[2, 3])
        execute = AsyncMock(side_effect=[None, RuntimeError("episode update failed")])

        with (
            patch(
                "comet.background_scraper.worker.database.transaction",
                return_value=transaction,
            ),
            patch("comet.background_scraper.worker.database.fetch_val", fetch_val),
            patch("comet.background_scraper.worker.database.execute", execute),
            self.assertRaisesRegex(RuntimeError, "episode update failed"),
        ):
            await worker.requeue_dead_items()

        self.assertIsInstance(transaction.exit_error, RuntimeError)
        self.assertEqual(fetch_val.await_count, 2)
        self.assertEqual(execute.await_count, 2)

    async def test_recent_runs_enforces_limit_and_serializes_current_schema(self):
        worker = BackgroundScraperWorker()
        row = {
            "run_id": "12345678-1234-4234-8234-123456789abc",
            "started_at": 10.0,
            "finished_at": 11.0,
            "status": "completed",
            "processed": 3,
            "success": 2,
            "failed": 1,
            "torrents_found": 4,
            "duration_ms": 1000,
            "worker_count": 2,
            "last_error": None,
        }
        fetch_all = AsyncMock(return_value=[row])

        with patch("comet.background_scraper.worker.database.fetch_all", fetch_all):
            self.assertEqual(await worker.get_recent_runs(20), [row])

        for limit in (None, True, 0, -1, 201, 1.5, "20"):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                await worker.get_recent_runs(limit)

        fetch_all.assert_awaited_once()

    def test_run_rows_reject_corrupt_schema_and_invariants(self):
        valid = {
            "run_id": "12345678-1234-4234-8234-123456789abc",
            "started_at": 10.0,
            "finished_at": 11.0,
            "status": "completed",
            "processed": 3,
            "success": 2,
            "failed": 1,
            "torrents_found": 4,
            "duration_ms": 1000,
            "worker_count": 2,
            "last_error": None,
        }
        invalid_rows = [
            None,
            valid | {"extra": 1},
            valid | {"run_id": "not-a-uuid"},
            valid | {"status": "dead"},
            valid | {"started_at": 10},
            valid | {"started_at": math.nan},
            valid | {"finished_at": 9.0},
            valid | {"finished_at": None},
            valid | {"processed": True},
            valid | {"failed": -1},
            valid | {"duration_ms": 1.5},
            valid | {"last_error": 1},
        ]

        self.assertEqual(_serialize_run_row(valid), valid)
        self.assertIsNone(
            _serialize_run_row(valid | {"status": "running", "finished_at": None})[
                "finished_at"
            ]
        )
        for row in invalid_rows:
            with self.subTest(row=row), self.assertRaises((TypeError, ValueError)):
                _serialize_run_row(row)


if __name__ == "__main__":
    unittest.main()
