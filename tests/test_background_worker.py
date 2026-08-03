import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from databases import Database

from comet.background_scraper.worker import (
    BackgroundScraperWorker,
    _serialize_run_row,
)
from comet.core.db_router import ReplicaAwareDatabase
from comet.core.models import settings
from comet.metadata.manager import MetadataFetchResult, MetadataFetchStatus


async def _create_queue_database(path: Path) -> ReplicaAwareDatabase:
    database = ReplicaAwareDatabase(Database(f"sqlite:///{path}"))
    await database.connect()
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
    return database


class _PassthroughLock:
    def __init__(self, *_args, **_kwargs):
        pass

    async def acquire(self, **_kwargs):
        return True

    async def run(self, task):
        await task

    async def release(self):
        pass


class BackgroundWorkerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_movie_scrape_consumes_structured_metadata_result(self):
        worker = BackgroundScraperWorker()
        worker.metadata_scraper = Mock()
        worker.metadata_scraper.fetch_aliases_with_metadata = AsyncMock(
            return_value=MetadataFetchResult(
                MetadataFetchStatus.OK,
                {
                    "title": "Spider-Man: Homecoming",
                    "year": 2017,
                    "year_end": None,
                },
                {"en": ["Spider-Man: Homecoming"]},
            )
        )
        manager = Mock()
        manager.torrents = {"hash": {}}
        manager.scrape_torrents = AsyncMock()

        with (
            patch(
                "comet.background_scraper.worker.TorrentResultAccumulator",
                return_value=manager,
            ) as accumulator,
            patch(
                "comet.background_scraper.worker.mark_scope_scraped",
                new=AsyncMock(),
            ) as mark_scraped,
        ):
            count = await worker._scrape_movie(
                {
                    "media_id": "tt2250912",
                    "media_type": "movie",
                    "title": "Spider-Man: Homecoming",
                    "year": 2017,
                    "year_end": None,
                }
            )

        self.assertEqual(count, 1)
        self.assertEqual(
            accumulator.call_args.kwargs["aliases"],
            {"en": ["Spider-Man: Homecoming"]},
        )
        manager.scrape_torrents.assert_awaited_once()
        mark_scraped.assert_awaited_once_with("tt2250912")

    async def test_completed_item_emits_progress_log(self):
        worker = BackgroundScraperWorker()
        worker.is_running = True
        worker._scrape_movie = AsyncMock(return_value=3)
        worker._update_item_state = AsyncMock()

        with patch("comet.background_scraper.worker.log.info") as info:
            await worker._scrape_single_media(
                {
                    "media_id": "tt2250912",
                    "media_type": "movie",
                    "consecutive_failures": 0,
                },
                None,
            )

        self.assertEqual(worker.stats.total_processed, 1)
        self.assertEqual(worker.stats.total_success, 1)
        info.assert_called_once()
        self.assertEqual(info.call_args.args[0], "background.item.completed")
        self.assertEqual(info.call_args.kwargs["outcome"], "ok")
        self.assertEqual(info.call_args.kwargs["torrent_count"], 3)

    async def test_failed_item_logs_the_caught_exception(self):
        worker = BackgroundScraperWorker()
        worker.is_running = True
        failure = TypeError("invalid metadata result")
        worker._scrape_movie = AsyncMock(side_effect=failure)
        worker._update_item_state = AsyncMock()

        with patch("comet.background_scraper.worker.log.warning") as warning:
            await worker._scrape_single_media(
                {
                    "media_id": "tt2250912",
                    "media_type": "movie",
                    "consecutive_failures": 0,
                },
                None,
            )

        self.assertEqual(worker.stats.total_failed, 1)
        self.assertEqual(worker.stats.errors, 1)
        self.assertIs(warning.call_args.kwargs["exc"], failure)
        self.assertEqual(warning.call_args.kwargs["outcome"], "failed")

    async def test_unexpected_item_task_failure_aborts_the_batch(self):
        worker = BackgroundScraperWorker()
        worker.is_running = True
        worker._scrape_single_media = AsyncMock(
            side_effect=RuntimeError("state update failed")
        )

        with (
            patch.object(settings, "BACKGROUND_SCRAPER_CONCURRENT_WORKERS", 1),
            self.assertRaisesRegex(RuntimeError, "state update failed"),
        ):
            await worker._run_items_in_bounded_chunks(
                [{"media_id": "tt1234567", "consecutive_failures": 0}],
                None,
            )

    async def test_drain_finishes_active_cycle_without_starting_another(self):
        worker = BackgroundScraperWorker()
        cycle_started = asyncio.Event()
        finish_cycle = asyncio.Event()
        cycle_count = 0

        async def run_cycle():
            nonlocal cycle_count
            cycle_count += 1
            worker.current_run_id = "active-run"
            cycle_started.set()
            await finish_cycle.wait()
            worker.current_run_id = None

        worker._run_scraping_cycle = run_cycle

        with patch("comet.background_scraper.worker.DistributedLock", _PassthroughLock):
            task = asyncio.create_task(worker._run_continuous())
            await cycle_started.wait()

            self.assertTrue(await worker.drain())
            self.assertTrue(await worker.drain())
            self.assertTrue(worker.is_running)
            self.assertTrue(worker._drain_requested)
            worker.is_paused = True
            worker.pause_event.clear()
            worker._discovery_paused_for_backlog = True

            finish_cycle.set()
            await asyncio.wait_for(task, timeout=1)

        self.assertEqual(cycle_count, 1)
        self.assertFalse(worker.is_running)
        self.assertFalse(worker.is_paused)
        self.assertFalse(worker._drain_requested)
        self.assertTrue(worker.pause_event.is_set())
        self.assertFalse(worker._discovery_paused_for_backlog)

    async def test_drain_stops_immediately_when_between_cycles(self):
        worker = BackgroundScraperWorker()
        worker.is_running = True
        worker.stop = AsyncMock()

        self.assertFalse(await worker.drain())

        worker.stop.assert_awaited_once_with()

    async def test_scheduled_stop_can_be_cancelled(self):
        worker = BackgroundScraperWorker()
        worker.is_running = True
        worker.current_run_id = "active-run"

        self.assertTrue(await worker.drain())
        self.assertTrue(worker.cancel_drain())
        self.assertFalse(worker._drain_requested)
        self.assertFalse(worker.cancel_drain())

    async def test_drain_is_honored_when_active_cycle_fails(self):
        worker = BackgroundScraperWorker()
        cycle_started = asyncio.Event()
        fail_cycle = asyncio.Event()

        async def run_cycle():
            worker.current_run_id = "active-run"
            cycle_started.set()
            await fail_cycle.wait()
            worker.current_run_id = None
            raise RuntimeError("cycle failed")

        worker._run_scraping_cycle = run_cycle

        with patch("comet.background_scraper.worker.DistributedLock", _PassthroughLock):
            task = asyncio.create_task(worker._run_continuous())
            await cycle_started.wait()
            self.assertTrue(await worker.drain())
            fail_cycle.set()
            await asyncio.wait_for(task, timeout=1)

        self.assertEqual(worker.last_error, "cycle failed")
        self.assertFalse(worker.is_running)

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

        with patch(
            "comet.background_scraper.worker.DistributedLock.acquire",
            new=AsyncMock(return_value=False),
        ):
            task = asyncio.create_task(worker._run_continuous())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertFalse(worker.is_running)


class BackgroundWorkerQueryTests(unittest.IsolatedAsyncioTestCase):
    def test_queue_policy_consumes_validated_watermarks_without_rewriting(self):
        worker = BackgroundScraperWorker()
        with (
            patch.object(settings, "BACKGROUND_SCRAPER_QUEUE_LOW_WATERMARK", 100),
            patch.object(settings, "BACKGROUND_SCRAPER_QUEUE_HIGH_WATERMARK", 200),
            patch.object(settings, "BACKGROUND_SCRAPER_QUEUE_HARD_CAP", 300),
        ):
            self.assertEqual(worker._discovery_queue_limits(), (100, 200, 300))
            allowed, reason, limits, paused = worker._evaluate_discovery_policy(200)
            self.assertFalse(allowed)
            self.assertEqual(reason, "above_high_watermark")
            self.assertEqual(limits, {"low": 100, "high": 200, "hard": 300})
            self.assertTrue(paused)

            allowed, reason, _limits, paused = worker._evaluate_discovery_policy(100)
            self.assertTrue(allowed)
            self.assertIsNone(reason)
            self.assertFalse(paused)

        with self.assertRaises(KeyError):
            worker._apply_discovery_headroom(10, 10, 0, {"high": 0})

    def test_retry_policy_consumes_validated_backoff_and_sentinel(self):
        worker = BackgroundScraperWorker()
        with (
            patch.object(settings, "BACKGROUND_SCRAPER_FAILURE_BASE_BACKOFF", 10),
            patch.object(settings, "BACKGROUND_SCRAPER_FAILURE_MAX_BACKOFF", 25),
        ):
            self.assertEqual(worker._compute_backoff(1), 10)
            self.assertEqual(worker._compute_backoff(2), 20)
            self.assertEqual(worker._compute_backoff(3), 25)

        with patch.object(settings, "BACKGROUND_SCRAPER_MAX_RETRIES", -1):
            self.assertEqual(worker._max_retries_for_query(), 1_000_000)
            self.assertFalse(worker._is_retry_limit_reached(1_000_000))

    async def test_queue_snapshot_query_executes_against_sqlite(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = await _create_queue_database(Path(temp_dir) / "queue.db")
            try:
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
        self.assertEqual(snapshot["oldest_age_s"], 15.0)

    async def test_queue_age_starts_when_success_becomes_refresh_eligible(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = await _create_queue_database(Path(temp_dir) / "queue.db")
            try:
                await database.execute(
                    """
                    INSERT INTO background_scraper_items
                    (media_id, media_type, next_retry_at, last_success_at, status,
                     consecutive_failures, created_at)
                    VALUES ('movie', 'movie', 97.0, 89.0, 'success', 0, 1.0)
                    """
                )

                with (
                    patch.object(settings, "BACKGROUND_SCRAPER_SUCCESS_TTL", 10),
                    patch("comet.background_scraper.worker.database", database),
                ):
                    snapshot = await BackgroundScraperWorker()._fetch_queue_snapshot(
                        now=100.0
                    )
            finally:
                await database.disconnect()

        self.assertEqual(snapshot["movies"], 1)
        self.assertEqual(snapshot["oldest_age_s"], 1.0)

    async def test_queue_age_starts_when_retry_becomes_eligible(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = await _create_queue_database(Path(temp_dir) / "queue.db")
            try:
                await database.execute(
                    """
                    INSERT INTO background_scraper_items
                    (media_id, media_type, next_retry_at, last_success_at, status,
                     consecutive_failures, created_at)
                    VALUES ('movie', 'movie', 98.0, NULL, 'failed', 1, 1.0)
                    """
                )

                with patch("comet.background_scraper.worker.database", database):
                    snapshot = await BackgroundScraperWorker()._fetch_queue_snapshot(
                        now=100.0
                    )
            finally:
                await database.disconnect()

        self.assertEqual(snapshot["movies"], 1)
        self.assertEqual(snapshot["oldest_age_s"], 2.0)

    async def test_episode_queue_age_waits_for_parent_series(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = await _create_queue_database(Path(temp_dir) / "queue.db")
            try:
                await database.execute(
                    """
                    INSERT INTO background_scraper_items
                    (media_id, media_type, next_retry_at, last_success_at, status,
                     consecutive_failures, created_at)
                    VALUES ('series', 'series', 99.0, NULL, 'deferred', 0, 1.0)
                    """
                )
                await database.execute(
                    """
                    INSERT INTO background_scraper_episodes
                    (series_id, season, episode, next_retry_at, last_success_at,
                     status, consecutive_failures, created_at)
                    VALUES ('series', 1, 1, 90.0, NULL, 'failed', 1, 1.0)
                    """
                )

                with patch("comet.background_scraper.worker.database", database):
                    snapshot = await BackgroundScraperWorker()._fetch_queue_snapshot(
                        now=100.0
                    )
            finally:
                await database.disconnect()

        self.assertEqual(snapshot["series"], 1)
        self.assertEqual(snapshot["episodes"], 1)
        self.assertEqual(snapshot["oldest_age_s"], 1.0)

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

    async def test_recent_runs_serializes_current_schema(self):
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

        fetch_all.assert_awaited_once()

    def test_run_rows_serialize_the_projection(self):
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
        self.assertEqual(_serialize_run_row(valid), valid)
        self.assertEqual(
            _serialize_run_row(valid | {"extra": 1})["run_id"], valid["run_id"]
        )
        self.assertIsNone(
            _serialize_run_row(valid | {"status": "running", "finished_at": None})[
                "finished_at"
            ]
        )


if __name__ == "__main__":
    unittest.main()
