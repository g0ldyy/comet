import asyncio
import math
import re
import time
import uuid
from dataclasses import dataclass

import orjson

from comet.core.database import ON_CONFLICT_DO_NOTHING, OR_IGNORE, database
from comet.core.logger import logger
from comet.core.models import settings
from comet.metadata.manager import MetadataScraper
from comet.services.lock import DistributedLock
from comet.services.orchestration import TorrentManager
from comet.utils.http_client import http_client_manager

from .cinemata_client import CinemataClient

LOCK_KEY = "background_scraper_lock"
LOCK_TTL = 60
YEAR_PATTERN = re.compile(r"\d{4}")


@dataclass
class ScrapingStats:
    run_id: str = ""
    total_processed: int = 0
    total_success: int = 0
    total_failed: int = 0
    total_torrents_found: int = 0
    discovered_items: int = 0
    errors: int = 0
    start_time: float = 0.0

    @property
    def duration(self) -> float:
        return time.time() - self.start_time if self.start_time else 0.0


class BackgroundScraperWorker:
    def __init__(self):
        self.is_running = False
        self.is_paused = False
        self.pause_event = asyncio.Event()
        self.pause_event.set()
        self.current_run_id = None
        self.last_error = None
        self.stats = ScrapingStats()
        self.metadata_scraper = None
        self.task: asyncio.Task | None = None
        self._active_scrape_task = None
        self._discovery_paused_for_backlog = False

    def clear_finished_task(self):
        if not self.task or not self.task.done():
            return

        if not self.task.cancelled():
            try:
                error = self.task.exception()
                if error:
                    self.last_error = str(error)
                    logger.error(f"Background scraper task failed: {error}")
            except Exception as e:
                self.last_error = str(e)
                logger.error(f"Background scraper task failed: {e}")
        self.task = None

    async def _cancel_task(self, task: asyncio.Task | None):
        if not task or task.done():
            return

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def _queue_query_context(self, now: float | None = None):
        current_now = now if now is not None else time.time()
        return current_now, {
            "now": current_now,
            "success_cutoff": current_now
            - (settings.BACKGROUND_SCRAPER_SUCCESS_TTL or 0),
            "max_retries": self._max_retries_for_query(),
        }

    async def _fetch_queue_snapshot(self, now: float | None = None):
        current_now, query_context = self._queue_query_context(now=now)

        item_counts_rows = await database.fetch_all(
            """
            SELECT media_type, COUNT(*) AS count
            FROM background_scraper_items
            WHERE media_type IN ('movie', 'series')
              AND (next_retry_at IS NULL OR next_retry_at <= :now)
              AND (last_success_at IS NULL OR last_success_at <= :success_cutoff)
              AND (status != 'dead' OR consecutive_failures < :max_retries)
            GROUP BY media_type
            """,
            query_context,
        )
        item_counts = {"movie": 0, "series": 0}
        for row in item_counts_rows:
            media_type = row["media_type"]
            if media_type in item_counts:
                item_counts[media_type] = int(row["count"])

        queue_episodes = await database.fetch_val(
            """
            SELECT COUNT(*) FROM background_scraper_episodes
            WHERE season >= 1
              AND episode >= 1
              AND (next_retry_at IS NULL OR next_retry_at <= :now)
              AND (last_success_at IS NULL OR last_success_at <= :success_cutoff)
              AND (status != 'dead' OR consecutive_failures < :max_retries)
            """,
            query_context,
        )
        oldest_item_ts = await database.fetch_val(
            """
            SELECT MIN(created_at) FROM background_scraper_items
            WHERE (next_retry_at IS NULL OR next_retry_at <= :now)
              AND (last_success_at IS NULL OR last_success_at <= :success_cutoff)
              AND (status != 'dead' OR consecutive_failures < :max_retries)
            """,
            query_context,
        )
        oldest_episode_ts = await database.fetch_val(
            """
            SELECT MIN(created_at) FROM background_scraper_episodes
            WHERE season >= 1
              AND episode >= 1
              AND (next_retry_at IS NULL OR next_retry_at <= :now)
              AND (last_success_at IS NULL OR last_success_at <= :success_cutoff)
              AND (status != 'dead' OR consecutive_failures < :max_retries)
            """,
            query_context,
        )
        candidate_timestamps = [
            ts for ts in (oldest_item_ts, oldest_episode_ts) if ts is not None
        ]
        oldest_queue_age_s = (
            max(0.0, current_now - float(min(candidate_timestamps)))
            if candidate_timestamps
            else 0.0
        )
        queue_movies = int(item_counts["movie"])
        queue_series = int(item_counts["series"])
        queue_episodes = int(queue_episodes or 0)
        total_queue = queue_movies + queue_series + queue_episodes

        return {
            "movies": queue_movies,
            "series": queue_series,
            "episodes": queue_episodes,
            "total": total_queue,
            "oldest_age_s": oldest_queue_age_s,
        }

    def _discovery_queue_limits(self):
        low = max(0, int(settings.BACKGROUND_SCRAPER_QUEUE_LOW_WATERMARK or 0))
        high = max(0, int(settings.BACKGROUND_SCRAPER_QUEUE_HIGH_WATERMARK or 0))
        hard = max(0, int(settings.BACKGROUND_SCRAPER_QUEUE_HARD_CAP or 0))

        if high <= 0 and hard > 0:
            high = hard
        if high > 0 and (low <= 0 or low > high):
            low = max(1, high // 2)
        if hard > 0 and high > 0 and hard < high:
            hard = high

        return low, high, hard

    def _evaluate_discovery_policy(self, total_queue: int, update_state: bool = True):
        low, high, hard = self._discovery_queue_limits()
        paused_for_backlog = self._discovery_paused_for_backlog

        def set_paused(value: bool):
            nonlocal paused_for_backlog
            paused_for_backlog = value
            if update_state:
                self._discovery_paused_for_backlog = value

        if high <= 0 and hard <= 0:
            set_paused(False)
            return (
                True,
                None,
                {"low": low, "high": high, "hard": hard},
                paused_for_backlog,
            )

        if hard > 0 and total_queue >= hard:
            set_paused(True)
            return (
                False,
                "hard_cap_reached",
                {"low": low, "high": high, "hard": hard},
                paused_for_backlog,
            )

        if paused_for_backlog:
            if low > 0 and total_queue <= low:
                set_paused(False)
            else:
                return (
                    False,
                    "above_low_watermark",
                    {"low": low, "high": high, "hard": hard},
                    paused_for_backlog,
                )

        if high > 0 and total_queue >= high:
            set_paused(True)
            return (
                False,
                "above_high_watermark",
                {"low": low, "high": high, "hard": hard},
                paused_for_backlog,
            )

        return (
            True,
            None,
            {"low": low, "high": high, "hard": hard},
            paused_for_backlog,
        )

    def _apply_discovery_headroom(
        self,
        movies_target: int,
        series_target: int,
        total_queue: int,
        discovery_limits: dict,
    ):
        target_total = max(0, movies_target) + max(0, series_target)
        if target_total <= 0:
            return 0, 0

        queue_cap = int(discovery_limits.get("high") or 0)
        if queue_cap <= 0:
            queue_cap = int(discovery_limits.get("hard") or 0)
        if queue_cap <= 0:
            return max(0, movies_target), max(0, series_target)

        headroom = max(0, queue_cap - max(0, total_queue))
        if headroom <= 0:
            return 0, 0
        if headroom >= target_total:
            return max(0, movies_target), max(0, series_target)

        movies_weight = max(0, movies_target)
        series_weight = max(0, series_target)
        weighted_total = movies_weight + series_weight
        if weighted_total <= 0:
            return 0, 0

        movies_capped = min(
            movies_weight, int((headroom * movies_weight) / weighted_total)
        )
        series_capped = min(series_weight, headroom - movies_capped)
        allocated = movies_capped + series_capped
        leftover = headroom - allocated
        if leftover > 0:
            add_movies = min(movies_weight - movies_capped, leftover)
            movies_capped += add_movies
            leftover -= add_movies
            if leftover > 0:
                add_series = min(series_weight - series_capped, leftover)
                series_capped += add_series

        return movies_capped, series_capped

    def _planning_batch_size_per_type(self):
        workers = max(1, int(settings.BACKGROUND_SCRAPER_CONCURRENT_WORKERS or 1))
        return max(500, min(10000, workers * 500))

    async def _run_items_in_bounded_chunks(
        self, planned_items: list[dict], deadline: float | None
    ):
        if not planned_items:
            return

        worker_limit = max(1, int(settings.BACKGROUND_SCRAPER_CONCURRENT_WORKERS or 1))
        next_item_index = 0
        in_flight: set[asyncio.Task] = set()
        scheduling_stopped = False

        async def _defer_remaining():
            nonlocal next_item_index
            if next_item_index >= len(planned_items):
                return
            await self._defer_items(
                [
                    (item["media_id"], int(item["consecutive_failures"]))
                    for item in planned_items[next_item_index:]
                ]
            )
            next_item_index = len(planned_items)

        async def _start_next_item() -> bool:
            nonlocal next_item_index
            if next_item_index >= len(planned_items):
                return False
            if not self.is_running:
                return False

            await self._wait_if_paused()
            if not self.is_running:
                return False
            if deadline is not None and time.time() > deadline:
                return False

            item = planned_items[next_item_index]
            next_item_index += 1
            in_flight.add(
                asyncio.create_task(self._scrape_single_media(item, deadline))
            )
            return True

        try:
            while len(in_flight) < worker_limit and next_item_index < len(
                planned_items
            ):
                if not await _start_next_item():
                    scheduling_stopped = True
                    await _defer_remaining()
                    break

            while in_flight:
                done, pending = await asyncio.wait(
                    in_flight, return_when=asyncio.FIRST_COMPLETED
                )
                in_flight = set(pending)

                for task in done:
                    try:
                        task.result()
                    except asyncio.CancelledError:
                        continue
                    except Exception as result:
                        self.last_error = str(result)
                        logger.error(
                            f"Unhandled error while processing planned items: {result}"
                        )

                if scheduling_stopped:
                    continue

                while len(in_flight) < worker_limit and next_item_index < len(
                    planned_items
                ):
                    if not await _start_next_item():
                        scheduling_stopped = True
                        await _defer_remaining()
                        break
        except asyncio.CancelledError:
            for task in in_flight:
                task.cancel()
            if in_flight:
                await asyncio.gather(*in_flight, return_exceptions=True)
            raise

    async def start(self):
        if self.is_running:
            logger.log("BACKGROUND_SCRAPER", "Background scraper is already running")
            return

        logger.log("BACKGROUND_SCRAPER", "Starting background scraper orchestrator")
        await self._run_continuous()

    async def stop(self):
        logger.log("BACKGROUND_SCRAPER", "Stopping background scraper orchestrator")
        self.is_running = False
        self.is_paused = False
        self.pause_event.set()

        await self._cancel_task(self._active_scrape_task)
        self._active_scrape_task = None

        task = self.task
        current_task = asyncio.current_task()
        if task and task is not current_task:
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                self.last_error = str(e)
                logger.error(f"Background scraper task stopped with error: {e}")
        self.task = None

    async def pause(self):
        if not self.is_running:
            return False

        self.is_paused = True
        self.pause_event.clear()
        logger.log("BACKGROUND_SCRAPER", "Background scraper paused")
        return True

    async def resume(self):
        if not self.is_running:
            return False

        self.is_paused = False
        self.pause_event.set()
        logger.log("BACKGROUND_SCRAPER", "Background scraper resumed")
        return True

    async def get_status(self):
        now = time.time()
        lookback_24h = now - 86400

        latest_run = await database.fetch_one(
            """
            SELECT run_id, started_at, finished_at, status, processed, success, failed,
                   torrents_found, duration_ms, worker_count, last_error
            FROM background_scraper_runs
            ORDER BY started_at DESC
            LIMIT 1
            """
        )

        queue_snapshot = await self._fetch_queue_snapshot(now=now)
        dead_items_rows = await database.fetch_all(
            """
            SELECT media_type, COUNT(*) AS count
            FROM background_scraper_items
            WHERE status = 'dead'
            GROUP BY media_type
            """
        )
        dead_item_counts = {"movie": 0, "series": 0}
        for row in dead_items_rows:
            media_type = row["media_type"]
            if media_type in dead_item_counts:
                dead_item_counts[media_type] = int(row["count"])
        dead_episodes = await database.fetch_val(
            """
            SELECT COUNT(*) FROM background_scraper_episodes
            WHERE season >= 1
              AND episode >= 1
              AND status = 'dead'
            """
        )
        run_agg = await database.fetch_one(
            """
            SELECT
                COALESCE(SUM(processed), 0) AS processed,
                COALESCE(SUM(success), 0) AS success,
                COALESCE(SUM(failed), 0) AS failed,
                COALESCE(SUM(torrents_found), 0) AS torrents_found,
                COUNT(*) AS run_count
            FROM background_scraper_runs
            WHERE started_at >= :lookback_24h
            """,
            {"lookback_24h": lookback_24h},
        )
        processed_24h = int(run_agg["processed"])
        failed_24h = int(run_agg["failed"])
        torrents_24h = int(run_agg["torrents_found"])
        fail_rate_24h = (failed_24h / processed_24h) if processed_24h > 0 else 0.0
        torrents_per_item_24h = (
            (torrents_24h / processed_24h) if processed_24h > 0 else 0.0
        )
        total_queue = queue_snapshot["total"]
        oldest_queue_age_s = queue_snapshot["oldest_age_s"]
        (
            discovery_allowed,
            discovery_reason,
            discovery_limits,
            paused_for_backlog,
        ) = self._evaluate_discovery_policy(total_queue, update_state=False)
        health = self._compute_health_status(
            fail_rate_24h=fail_rate_24h,
            processed_24h=processed_24h,
            oldest_queue_age_s=oldest_queue_age_s,
            total_queue=total_queue,
        )

        return {
            "running": self.is_running,
            "paused": self.is_paused,
            "current_run_id": self.current_run_id,
            "last_error": self.last_error,
            "stats": {
                "run_id": self.stats.run_id,
                "processed": self.stats.total_processed,
                "success": self.stats.total_success,
                "failed": self.stats.total_failed,
                "torrents_found": self.stats.total_torrents_found,
                "discovered_items": self.stats.discovered_items,
                "errors": self.stats.errors,
                "duration_s": round(self.stats.duration, 2),
            },
            "queue": {
                "movies": queue_snapshot["movies"],
                "series": queue_snapshot["series"],
                "episodes": queue_snapshot["episodes"],
                "oldest_age_s": round(oldest_queue_age_s, 2),
            },
            "discovery": {
                "allowed": discovery_allowed,
                "paused_for_backlog": paused_for_backlog,
                "reason": discovery_reason,
                "queue_low_watermark": discovery_limits["low"],
                "queue_high_watermark": discovery_limits["high"],
                "queue_hard_cap": discovery_limits["hard"],
            },
            "dead": {
                "movies": dead_item_counts["movie"],
                "series": dead_item_counts["series"],
                "episodes": int(dead_episodes),
            },
            "slo": {
                "window_seconds": 86400,
                "processed": processed_24h,
                "failed": failed_24h,
                "torrents_found": torrents_24h,
                "fail_rate": round(fail_rate_24h, 4),
                "torrents_per_processed": round(torrents_per_item_24h, 4),
            },
            "health": health,
            "actions": {
                "can_requeue_dead": True,
            },
            "latest_run": dict(latest_run) if latest_run else None,
        }

    async def get_recent_runs(self, limit: int = 20):
        rows = await database.fetch_all(
            """
            SELECT run_id, started_at, finished_at, status, processed, success, failed,
                   torrents_found, duration_ms, worker_count, last_error
            FROM background_scraper_runs
            ORDER BY started_at DESC
            LIMIT :limit
            """,
            {"limit": limit},
        )
        return [dict(row) for row in rows]

    async def _run_continuous(self):
        self.is_running = True
        interval_seconds = settings.BACKGROUND_SCRAPER_INTERVAL

        while self.is_running:
            try:
                lock = DistributedLock(LOCK_KEY, timeout=LOCK_TTL)
                if await lock.acquire(wait_timeout=None):
                    lock_task = None
                    scrape_task = None
                    try:
                        lock_task = asyncio.create_task(self._maintain_lock(lock))
                        scrape_task = asyncio.create_task(self._run_scraping_cycle())
                        self._active_scrape_task = scrape_task

                        done, _ = await asyncio.wait(
                            [lock_task, scrape_task],
                            return_when=asyncio.FIRST_COMPLETED,
                        )

                        if lock_task in done:
                            logger.warning(
                                "BACKGROUND_SCRAPER: Lock lost during processing, aborting active cycle",
                            )
                        else:
                            if scrape_task.exception():
                                raise scrape_task.exception()
                    finally:
                        await self._cancel_task(lock_task)
                        await self._cancel_task(scrape_task)
                        await lock.release()
                        self._active_scrape_task = None
                else:
                    logger.log(
                        "BACKGROUND_SCRAPER",
                        "Another instance is running background scraping. Skipping.",
                    )

                if self.is_running:
                    await asyncio.sleep(interval_seconds)
            except asyncio.CancelledError:
                self.is_running = False
            except Exception as e:
                self.last_error = str(e)
                logger.error(f"Error in background scraper loop: {e}")
                await asyncio.sleep(300)

    async def _maintain_lock(self, lock: DistributedLock):
        try:
            while True:
                await asyncio.sleep(LOCK_TTL / 2)
                if not await lock.acquire():
                    logger.warning("BACKGROUND_SCRAPER: Failed to renew lock")
                    return
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"Error in background lock maintenance: {e}")
            return

    async def _run_scraping_cycle(self):
        run_id = str(uuid.uuid4())
        self.current_run_id = run_id
        self.stats = ScrapingStats(run_id=run_id, start_time=time.time())

        run_status = "completed"
        run_error = None
        await self._insert_run_row(run_id)

        try:
            await self._wait_if_paused()
            if not self.is_running:
                run_status = "cancelled"
                return
            await self._reset_running_items()

            session = await http_client_manager.get_session()
            self.metadata_scraper = MetadataScraper(session)

            max_movies = max(0, settings.BACKGROUND_SCRAPER_MAX_MOVIES_PER_RUN or 0)
            max_series = max(0, settings.BACKGROUND_SCRAPER_MAX_SERIES_PER_RUN or 0)
            discovery_multiplier = max(
                1, settings.BACKGROUND_SCRAPER_DISCOVERY_MULTIPLIER or 1
            )
            queue_snapshot = await self._fetch_queue_snapshot()
            (
                discovery_allowed,
                discovery_reason,
                discovery_limits,
                _,
            ) = self._evaluate_discovery_policy(queue_snapshot["total"])

            if discovery_allowed:
                discovery_target_movies = max_movies * discovery_multiplier
                discovery_target_series = max_series * discovery_multiplier
                (
                    discovery_target_movies,
                    discovery_target_series,
                ) = self._apply_discovery_headroom(
                    discovery_target_movies,
                    discovery_target_series,
                    queue_snapshot["total"],
                    discovery_limits,
                )
                logger.log(
                    "BACKGROUND_SCRAPER",
                    f"Run {run_id}: queue={queue_snapshot['total']} discovery targets movie={discovery_target_movies}, series={discovery_target_series}",
                )

                if discovery_target_movies > 0:
                    self.stats.discovered_items += await self._discover_media_type(
                        session, "movie", discovery_target_movies
                    )
                    if not self.is_running:
                        run_status = "cancelled"
                        return
                if discovery_target_series > 0:
                    self.stats.discovered_items += await self._discover_media_type(
                        session, "series", discovery_target_series
                    )
                    if not self.is_running:
                        run_status = "cancelled"
                        return
            else:
                logger.log(
                    "BACKGROUND_SCRAPER",
                    f"Run {run_id}: discovery paused ({discovery_reason}) queue={queue_snapshot['total']} watermarks={discovery_limits['low']}/{discovery_limits['high']} hard_cap={discovery_limits['hard']}",
                )

            runtime_budget = settings.BACKGROUND_SCRAPER_RUN_TIME_BUDGET
            deadline = (
                self.stats.start_time + runtime_budget
                if runtime_budget and runtime_budget > 0
                else None
            )
            planning_batch_size = self._planning_batch_size_per_type()
            remaining_movies = max_movies
            remaining_series = max_series
            planned_movies_total = 0
            planned_series_total = 0

            while self.is_running and (remaining_movies > 0 or remaining_series > 0):
                await self._wait_if_paused()
                if not self.is_running:
                    run_status = "cancelled"
                    return
                if deadline is not None and time.time() > deadline:
                    break

                batch_movies_limit = min(remaining_movies, planning_batch_size)
                batch_series_limit = min(remaining_series, planning_batch_size)
                planned_movies = (
                    await self._plan_items("movie", batch_movies_limit)
                    if batch_movies_limit > 0
                    else []
                )
                planned_series = (
                    await self._plan_items("series", batch_series_limit)
                    if batch_series_limit > 0
                    else []
                )
                planned_items = planned_movies + planned_series
                if not planned_items:
                    break

                planned_movies_count = len(planned_movies)
                planned_series_count = len(planned_series)
                planned_movies_total += planned_movies_count
                planned_series_total += planned_series_count
                remaining_movies = max(0, remaining_movies - planned_movies_count)
                remaining_series = max(0, remaining_series - planned_series_count)

                await self._run_items_in_bounded_chunks(planned_items, deadline)

            logger.log(
                "BACKGROUND_SCRAPER",
                f"Run {run_id}: planned {planned_movies_total + planned_series_total} items "
                f"({planned_movies_total} movies, {planned_series_total} series)",
            )

        except asyncio.CancelledError:
            run_status = "cancelled"
            raise
        except Exception as e:
            run_status = "failed"
            run_error = str(e)
            self.last_error = str(e)
            logger.error(f"Run {run_id} failed: {e}")
        finally:
            await self._reset_running_items()
            await self._finalize_run_row(run_id, run_status, run_error)
            logger.log(
                "BACKGROUND_SCRAPER",
                f"Run {run_id} finished with status={run_status} "
                f"processed={self.stats.total_processed} success={self.stats.total_success} "
                f"failed={self.stats.total_failed} torrents={self.stats.total_torrents_found} "
                f"discovered={self.stats.discovered_items} duration={self.stats.duration:.2f}s",
            )
            self.current_run_id = None
            self.metadata_scraper = None

    async def _insert_run_row(self, run_id: str):
        now = time.time()
        await database.execute(
            """
            UPDATE background_scraper_runs
            SET status = 'cancelled',
                finished_at = :finished_at,
                duration_ms = CAST((:finished_at - started_at) * 1000 AS INTEGER),
                last_error = COALESCE(last_error, 'Recovered stale running row')
            WHERE status = 'running'
            """,
            {"finished_at": now},
        )

        await database.execute(
            """
            INSERT INTO background_scraper_runs
            (run_id, started_at, status, worker_count, config_snapshot)
            VALUES (:run_id, :started_at, :status, :worker_count, :config_snapshot)
            """,
            {
                "run_id": run_id,
                "started_at": now,
                "status": "running",
                "worker_count": max(
                    1, settings.BACKGROUND_SCRAPER_CONCURRENT_WORKERS or 1
                ),
                "config_snapshot": self._serialize_config_snapshot(),
            },
        )

    async def _finalize_run_row(self, run_id: str, status: str, last_error: str | None):
        finished_at = time.time()
        duration_ms = int((finished_at - self.stats.start_time) * 1000)

        await database.execute(
            """
            UPDATE background_scraper_runs
            SET finished_at = :finished_at,
                status = :status,
                processed = :processed,
                success = :success,
                failed = :failed,
                torrents_found = :torrents_found,
                duration_ms = :duration_ms,
                last_error = :last_error
            WHERE run_id = :run_id
            """,
            {
                "run_id": run_id,
                "finished_at": finished_at,
                "status": status,
                "processed": self.stats.total_processed,
                "success": self.stats.total_success,
                "failed": self.stats.total_failed,
                "torrents_found": self.stats.total_torrents_found,
                "duration_ms": duration_ms,
                "last_error": last_error,
            },
        )

    def _serialize_config_snapshot(self) -> str:
        payload = {
            "workers": settings.BACKGROUND_SCRAPER_CONCURRENT_WORKERS,
            "interval": settings.BACKGROUND_SCRAPER_INTERVAL,
            "max_movies": settings.BACKGROUND_SCRAPER_MAX_MOVIES_PER_RUN,
            "max_series": settings.BACKGROUND_SCRAPER_MAX_SERIES_PER_RUN,
            "success_ttl": settings.BACKGROUND_SCRAPER_SUCCESS_TTL,
            "failure_base_backoff": settings.BACKGROUND_SCRAPER_FAILURE_BASE_BACKOFF,
            "failure_max_backoff": settings.BACKGROUND_SCRAPER_FAILURE_MAX_BACKOFF,
            "max_retries": settings.BACKGROUND_SCRAPER_MAX_RETRIES,
            "run_time_budget": settings.BACKGROUND_SCRAPER_RUN_TIME_BUDGET,
            "discovery_multiplier": settings.BACKGROUND_SCRAPER_DISCOVERY_MULTIPLIER,
            "max_episodes_per_series_run": settings.BACKGROUND_SCRAPER_MAX_EPISODES_PER_SERIES_PER_RUN,
            "episode_refresh_ttl": settings.BACKGROUND_SCRAPER_EPISODE_REFRESH_TTL,
            "demand_priority": settings.BACKGROUND_SCRAPER_ENABLE_DEMAND_PRIORITY,
            "demand_lookback": settings.BACKGROUND_SCRAPER_DEMAND_LOOKBACK,
            "defer_cooldown": settings.BACKGROUND_SCRAPER_DEFER_COOLDOWN,
            "min_priority_score": settings.BACKGROUND_SCRAPER_MIN_PRIORITY_SCORE,
            "priority_decay_on_miss": settings.BACKGROUND_SCRAPER_PRIORITY_DECAY_ON_MISS,
            "queue_low_watermark": settings.BACKGROUND_SCRAPER_QUEUE_LOW_WATERMARK,
            "queue_high_watermark": settings.BACKGROUND_SCRAPER_QUEUE_HIGH_WATERMARK,
            "queue_hard_cap": settings.BACKGROUND_SCRAPER_QUEUE_HARD_CAP,
            "alert_fail_rate": settings.BACKGROUND_SCRAPER_ALERT_FAIL_RATE,
            "alert_queue_age": settings.BACKGROUND_SCRAPER_ALERT_QUEUE_AGE,
        }
        return orjson.dumps(payload).decode("utf-8")

    async def _wait_if_paused(self):
        if not self.is_paused:
            return
        await self.pause_event.wait()

    async def _discover_media_type(
        self, session, media_type: str, max_discovery_items: int
    ) -> int:
        if max_discovery_items <= 0:
            return 0

        discovered = 0
        batch = []
        now = time.time()
        current_year = time.gmtime().tm_year
        success_cutoff = now - (settings.BACKGROUND_SCRAPER_SUCCESS_TTL or 0)
        blocked_cache: dict[str, bool] = {}

        async with CinemataClient(session=session) as cinemata_client:
            async for media_item in cinemata_client.fetch_all_of_type(media_type):
                if not self.is_running:
                    break
                await self._wait_if_paused()
                if not self.is_running:
                    break

                normalized = self._normalize_media_item(
                    media_item, media_type, now, current_year
                )
                if not normalized:
                    continue

                media_id = normalized["media_id"]
                if media_id in blocked_cache:
                    blocked = blocked_cache[media_id]
                else:
                    blocked = await self._is_discovery_candidate_blocked(
                        media_id=media_id,
                        media_type=media_type,
                        now=now,
                        success_cutoff=success_cutoff,
                    )
                    blocked_cache[media_id] = blocked
                if blocked:
                    continue

                batch.append(normalized)
                discovered += 1

                if len(batch) >= 200:
                    await self._upsert_discovered_items(batch)
                    batch.clear()

                if discovered >= max_discovery_items:
                    break

        if batch:
            await self._upsert_discovered_items(batch)

        return discovered

    async def _is_discovery_candidate_blocked(
        self, media_id: str, media_type: str, now: float, success_cutoff: float
    ) -> bool:
        blocked = await database.fetch_val(
            """
            SELECT 1
            FROM background_scraper_items
            WHERE media_id = :media_id
              AND media_type = :media_type
              AND (
                    (next_retry_at IS NOT NULL AND next_retry_at > :now)
                    OR (last_success_at IS NOT NULL AND last_success_at > :success_cutoff)
                  )
            LIMIT 1
            """,
            {
                "media_id": media_id,
                "media_type": media_type,
                "now": now,
                "success_cutoff": success_cutoff,
            },
        )
        return bool(blocked)

    def _normalize_media_item(
        self, media_item: dict, media_type: str, now: float, current_year: int
    ):
        media_id = media_item.get("imdb_id") or media_item.get("id")
        title = media_item.get("name") or media_item.get("title")
        if not media_id or not title:
            return None

        year_source = media_item.get("year") or media_item.get("releaseInfo")
        year, year_end = self._parse_year_range(year_source)
        if year is None:
            return None

        return {
            "media_id": media_id,
            "media_type": media_type,
            "title": title,
            "year": year,
            "year_end": year_end,
            "priority_score": self._calculate_priority(
                media_item, media_type, year, current_year
            ),
            "source": "cinemeta",
            "created_at": now,
            "updated_at": now,
        }

    def _parse_year_range(self, raw_year):
        if raw_year is None:
            return None, None

        if isinstance(raw_year, int):
            return raw_year, None

        text = str(raw_year).strip()
        matches = YEAR_PATTERN.findall(text)
        if not matches:
            return None, None

        year = int(matches[0])
        year_end = int(matches[1]) if len(matches) > 1 else None
        if year_end is not None and year_end < year:
            year_end = None

        return year, year_end

    def _calculate_priority(
        self, media_item: dict, media_type: str, year: int, current_year: int
    ) -> float:
        rating_raw = media_item.get("imdbRating") or 0
        try:
            rating = float(rating_raw)
        except (TypeError, ValueError):
            rating = 0.0

        votes_raw = media_item.get("imdbVotes") or 0
        if isinstance(votes_raw, str):
            votes_raw = votes_raw.replace(",", "")
        try:
            votes = int(votes_raw)
        except (TypeError, ValueError):
            votes = 0

        recency_bonus = max(0.0, 12.0 - (current_year - year))
        votes_bonus = min(votes / 50000.0, 4.0)
        type_bonus = 1.5 if media_type == "series" else 0.0

        return round((rating * 10.0) + recency_bonus + votes_bonus + type_bonus, 4)

    async def _upsert_discovered_items(self, batch: list[dict]):
        query = """
        INSERT INTO background_scraper_items
        (media_id, media_type, title, year, year_end, priority_score, status, source,
         consecutive_failures, created_at, updated_at)
        VALUES
        (:media_id, :media_type, :title, :year, :year_end, :priority_score, 'discovered', :source,
         0, :created_at, :updated_at)
        ON CONFLICT (media_id) DO UPDATE SET
            media_type = excluded.media_type,
            title = excluded.title,
            year = excluded.year,
            year_end = excluded.year_end,
            source = excluded.source,
            priority_score = CASE
                WHEN excluded.priority_score > background_scraper_items.priority_score
                THEN excluded.priority_score
                ELSE background_scraper_items.priority_score
            END,
            updated_at = excluded.updated_at
        """
        await database.execute_many(query, batch)

    async def _plan_items(self, media_type: str, limit: int):
        if limit <= 0:
            return []

        now = time.time()
        success_cutoff = now - (settings.BACKGROUND_SCRAPER_SUCCESS_TTL or 0)
        demand_cutoff = now - (settings.BACKGROUND_SCRAPER_DEMAND_LOOKBACK or 0)
        max_retries = self._max_retries_for_query()
        demand_enabled = 1 if settings.BACKGROUND_SCRAPER_ENABLE_DEMAND_PRIORITY else 0
        min_priority_score = max(
            0.0, float(settings.BACKGROUND_SCRAPER_MIN_PRIORITY_SCORE or 0.0)
        )

        rows = await database.fetch_all(
            """
            WITH demand_ids AS (
                SELECT DISTINCT fs.media_id
                FROM first_searches fs
                WHERE :demand_enabled = 1
                  AND fs.timestamp >= :demand_cutoff
            )
            SELECT i.media_id, i.media_type, i.title, i.year, i.year_end, i.priority_score,
                   i.consecutive_failures, i.status,
                   CASE WHEN d.media_id IS NOT NULL THEN 100.0 ELSE 0.0 END AS demand_boost
            FROM background_scraper_items i
            LEFT JOIN demand_ids d ON d.media_id = i.media_id
            WHERE i.media_type = :media_type
              AND (i.next_retry_at IS NULL OR i.next_retry_at <= :now)
              AND (i.last_success_at IS NULL OR i.last_success_at <= :success_cutoff)
              AND (i.status != 'dead' OR i.consecutive_failures < :max_retries)
              AND (
                    i.priority_score >= :min_priority_score
                    OR d.media_id IS NOT NULL
                    OR i.consecutive_failures > 0
                  )
            ORDER BY
              (i.priority_score + CASE WHEN d.media_id IS NOT NULL THEN 100.0 ELSE 0.0 END) DESC,
              COALESCE(i.last_scraped_at, 0) ASC
            LIMIT :limit
            """,
            {
                "media_type": media_type,
                "now": now,
                "success_cutoff": success_cutoff,
                "demand_cutoff": demand_cutoff,
                "max_retries": max_retries,
                "limit": limit,
                "demand_enabled": demand_enabled,
                "min_priority_score": min_priority_score,
            },
        )

        if rows:
            await database.execute_many(
                """
                UPDATE background_scraper_items
                SET status = 'running', updated_at = :updated_at
                WHERE media_id = :media_id
                """,
                [{"media_id": row["media_id"], "updated_at": now} for row in rows],
            )

        return [dict(row) for row in rows]

    async def _scrape_single_media(self, item: dict, deadline: float | None):
        item_failures = int(item["consecutive_failures"])
        if not self.is_running:
            await self._defer_item(item["media_id"], item_failures)
            return

        await self._wait_if_paused()
        if not self.is_running:
            await self._defer_item(item["media_id"], item_failures)
            return

        if deadline is not None and time.time() > deadline:
            await self._defer_item(item["media_id"], item_failures)
            return

        media_id = item["media_id"]
        media_type = item["media_type"]
        torrents_found = 0
        success = False
        error_message = None

        try:
            if media_type == "movie":
                torrents_found = await self._scrape_movie(item)
            else:
                torrents_found = await self._scrape_series(item, deadline)
            success = torrents_found > 0
        except Exception as e:
            error_message = str(e)
            self.stats.errors += 1
            logger.error(
                f"Background scrape failed for {media_type} {media_id}: {error_message}"
            )

        await self._update_item_state(item, success, torrents_found, error_message)

        if success:
            self.stats.total_success += 1
        else:
            self.stats.total_failed += 1
        self.stats.total_processed += 1
        self.stats.total_torrents_found += torrents_found

    async def _scrape_movie(self, item: dict) -> int:
        media_id = item["media_id"]
        title = item["title"]
        year = item["year"]

        metadata, aliases = await self.metadata_scraper.fetch_aliases_with_metadata(
            "movie", media_id, title, year, id=media_id
        )
        if metadata is None:
            return 0

        manager = TorrentManager(
            media_type="movie",
            media_full_id=media_id,
            media_only_id=media_id,
            title=metadata["title"],
            year=metadata["year"],
            year_end=None,
            season=None,
            episode=None,
            aliases=aliases,
            remove_adult_content=settings.REMOVE_ADULT_CONTENT,
            context="background",
        )

        await manager.scrape_torrents()
        torrents_found = len(manager.torrents)
        if torrents_found > 0:
            await self._insert_first_search(media_id)
        return torrents_found

    async def _scrape_series(self, item: dict, deadline: float | None) -> int:
        media_id = item["media_id"]
        title = item["title"]
        year = item["year"]
        year_end = item["year_end"]
        total_torrents = 0

        series_media_id = f"{media_id}:1:1"
        metadata, aliases = await self.metadata_scraper.fetch_aliases_with_metadata(
            "series", series_media_id, title, year, year_end, id=media_id
        )
        if metadata is None:
            return 0

        episodes = await self._get_or_discover_episodes(media_id)
        if not episodes:
            return 0

        for episode in episodes:
            if not self.is_running:
                break
            await self._wait_if_paused()
            if not self.is_running:
                break

            if deadline is not None and time.time() > deadline:
                break

            episode_media_id = episode["episode_media_id"]
            season = episode["season"]
            episode_number = episode["episode"]
            episode_torrents = 0
            success = False
            error_message = None

            try:
                manager = TorrentManager(
                    media_type="series",
                    media_full_id=episode_media_id,
                    media_only_id=media_id,
                    title=metadata["title"],
                    year=metadata["year"],
                    year_end=metadata["year_end"],
                    season=season,
                    episode=episode_number,
                    aliases=aliases,
                    remove_adult_content=settings.REMOVE_ADULT_CONTENT,
                    context="background",
                )
                await manager.scrape_torrents()
                episode_torrents = len(manager.torrents)
                success = episode_torrents > 0
            except Exception as e:
                error_message = str(e)
                logger.error(
                    f"Background scrape failed for episode {episode_media_id}: {error_message}"
                )

            await self._update_episode_state(
                episode, success, episode_torrents, error_message
            )

            if success:
                await self._insert_first_search(episode_media_id)
            total_torrents += episode_torrents

        return total_torrents

    async def _get_or_discover_episodes(self, series_id: str):
        now = time.time()
        has_existing_episodes = await database.fetch_val(
            """
            SELECT 1
            FROM background_scraper_episodes
            WHERE series_id = :series_id
              AND season >= 1
              AND episode >= 1
            LIMIT 1
            """,
            {"series_id": series_id},
        )
        episode_refresh_ttl = settings.BACKGROUND_SCRAPER_EPISODE_REFRESH_TTL or 0
        should_refresh_episodes = has_existing_episodes is None

        if has_existing_episodes and episode_refresh_ttl > 0:
            last_episode_refresh = await database.fetch_val(
                """
                SELECT MAX(updated_at)
                FROM background_scraper_episodes
                WHERE series_id = :series_id
                  AND season >= 1
                  AND episode >= 1
                """,
                {"series_id": series_id},
            )
            if (
                last_episode_refresh is None
                or (now - float(last_episode_refresh)) >= episode_refresh_ttl
            ):
                should_refresh_episodes = True

        if should_refresh_episodes:
            session = await http_client_manager.get_session()
            async with CinemataClient(session=session) as cinemata_client:
                discovered = await cinemata_client.fetch_series_episodes(series_id)

            if discovered:
                rows = []
                for entry in discovered:
                    season = entry["season"]
                    episode = entry["episode"]
                    episode_media_id = f"{series_id}:{season}:{episode}"
                    rows.append(
                        {
                            "episode_media_id": episode_media_id,
                            "series_id": series_id,
                            "season": season,
                            "episode": episode,
                            "created_at": now,
                            "updated_at": now,
                        }
                    )

                await database.execute_many(
                    """
                    INSERT INTO background_scraper_episodes
                    (episode_media_id, series_id, season, episode, status, created_at, updated_at)
                    VALUES
                    (:episode_media_id, :series_id, :season, :episode, 'discovered', :created_at, :updated_at)
                    ON CONFLICT (episode_media_id) DO UPDATE SET
                        season = excluded.season,
                        episode = excluded.episode,
                        updated_at = excluded.updated_at
                    """,
                    rows,
                )

        success_cutoff = now - (settings.BACKGROUND_SCRAPER_SUCCESS_TTL or 0)
        max_retries = self._max_retries_for_query()
        configured_max_episodes = (
            settings.BACKGROUND_SCRAPER_MAX_EPISODES_PER_SERIES_PER_RUN
        )
        max_episodes = (
            configured_max_episodes
            if configured_max_episodes and configured_max_episodes > 0
            else 10000
        )

        rows = await database.fetch_all(
            """
            SELECT episode_media_id, series_id, season, episode, status, consecutive_failures
            FROM background_scraper_episodes
            WHERE series_id = :series_id
              AND season >= 1
              AND episode >= 1
              AND (next_retry_at IS NULL OR next_retry_at <= :now)
              AND (last_success_at IS NULL OR last_success_at <= :success_cutoff)
              AND (status != 'dead' OR consecutive_failures < :max_retries)
            ORDER BY season DESC, episode DESC
            LIMIT :limit
            """,
            {
                "series_id": series_id,
                "now": now,
                "success_cutoff": success_cutoff,
                "max_retries": max_retries,
                "limit": max_episodes,
            },
        )
        return [dict(row) for row in rows]

    async def _update_item_state(
        self, item: dict, success: bool, torrents_found: int, error_message: str | None
    ):
        media_id = item["media_id"]
        current_failures = int(item["consecutive_failures"])
        state = self._compute_next_state(success, current_failures)

        await self._persist_entity_state(
            table_name="background_scraper_items",
            key_name="media_id",
            key_value=media_id,
            state=state,
            torrents_found=torrents_found,
        )
        item["consecutive_failures"] = state["consecutive_failures"]
        if not success:
            await self._decay_item_priority_on_miss(media_id)

        if not success and error_message:
            self.last_error = error_message

    async def _update_episode_state(
        self,
        episode: dict,
        success: bool,
        torrents_found: int,
        error_message: str | None,
    ):
        episode_media_id = episode["episode_media_id"]
        current_failures = int(episode["consecutive_failures"])
        state = self._compute_next_state(success, current_failures)

        await self._persist_entity_state(
            table_name="background_scraper_episodes",
            key_name="episode_media_id",
            key_value=episode_media_id,
            state=state,
            torrents_found=torrents_found,
        )
        episode["consecutive_failures"] = state["consecutive_failures"]

        if not success and error_message:
            self.last_error = error_message

    def _compute_next_state(self, success: bool, current_failures: int) -> dict:
        now = time.time()

        if success:
            return {
                "status": "success",
                "consecutive_failures": 0,
                "last_scraped_at": now,
                "last_success_at": now,
                "last_failure_at": None,
                "next_retry_at": now + (settings.BACKGROUND_SCRAPER_SUCCESS_TTL or 0),
                "updated_at": now,
            }

        failures = current_failures + 1
        blocked = self._is_retry_limit_reached(failures)
        return {
            "status": "dead" if blocked else "failed",
            "consecutive_failures": failures,
            "last_scraped_at": now,
            "last_success_at": None,
            "last_failure_at": now,
            "next_retry_at": None if blocked else now + self._compute_backoff(failures),
            "updated_at": now,
        }

    async def _persist_entity_state(
        self,
        table_name: str,
        key_name: str,
        key_value: str,
        state: dict,
        torrents_found: int,
    ):
        await database.execute(
            f"""
            UPDATE {table_name}
            SET status = :status,
                consecutive_failures = :consecutive_failures,
                last_scraped_at = :last_scraped_at,
                last_success_at = COALESCE(:last_success_at, last_success_at),
                last_failure_at = COALESCE(:last_failure_at, last_failure_at),
                next_retry_at = :next_retry_at,
                total_torrents_found =
                    COALESCE(total_torrents_found, 0) + :total_torrents_found,
                updated_at = :updated_at
            WHERE {key_name} = :entity_id
            """,
            {
                "entity_id": key_value,
                "status": state["status"],
                "consecutive_failures": state["consecutive_failures"],
                "last_scraped_at": state["last_scraped_at"],
                "last_success_at": state["last_success_at"],
                "last_failure_at": state["last_failure_at"],
                "next_retry_at": state["next_retry_at"],
                "total_torrents_found": torrents_found,
                "updated_at": state["updated_at"],
            },
        )

    async def _defer_items(self, items: list[tuple[str, int]]):
        if not items:
            return

        now = time.time()
        defer_cooldown = max(0, int(settings.BACKGROUND_SCRAPER_DEFER_COOLDOWN or 0))
        next_retry_at = now + defer_cooldown
        await database.execute_many(
            """
            UPDATE background_scraper_items
            SET status = 'deferred',
                consecutive_failures = :consecutive_failures,
                next_retry_at = :next_retry_at,
                updated_at = :updated_at
            WHERE media_id = :media_id
              AND status = 'running'
            """,
            [
                {
                    "media_id": media_id,
                    "consecutive_failures": current_failures,
                    "next_retry_at": next_retry_at,
                    "updated_at": now,
                }
                for media_id, current_failures in items
            ],
        )

    async def _defer_item(self, media_id: str, current_failures: int):
        await self._defer_items([(media_id, current_failures)])

    async def _reset_running_items(self):
        now = time.time()
        await database.execute(
            """
            UPDATE background_scraper_items
            SET status = 'discovered',
                next_retry_at = COALESCE(next_retry_at, :next_retry_at),
                updated_at = :updated_at
            WHERE status = 'running'
            """,
            {"next_retry_at": now, "updated_at": now},
        )

    async def _decay_item_priority_on_miss(self, media_id: str):
        decay = float(settings.BACKGROUND_SCRAPER_PRIORITY_DECAY_ON_MISS or 1.0)
        if decay <= 0 or decay >= 1:
            return

        await database.execute(
            """
            UPDATE background_scraper_items
            SET priority_score = priority_score * :decay
            WHERE media_id = :media_id
            """,
            {"media_id": media_id, "decay": decay},
        )

    async def requeue_dead_items(self):
        dead_items = int(
            await database.fetch_val(
                """
                SELECT COUNT(*) FROM background_scraper_items
                WHERE status = 'dead'
                """
            )
        )
        dead_episodes = int(
            await database.fetch_val(
                """
                SELECT COUNT(*) FROM background_scraper_episodes
                WHERE status = 'dead'
                """
            )
        )

        now = time.time()
        await database.execute(
            """
            UPDATE background_scraper_items
            SET status = 'discovered',
                consecutive_failures = 0,
                next_retry_at = :next_retry_at,
                updated_at = :updated_at
            WHERE status = 'dead'
            """,
            {"next_retry_at": now, "updated_at": now},
        )
        await database.execute(
            """
            UPDATE background_scraper_episodes
            SET status = 'discovered',
                consecutive_failures = 0,
                next_retry_at = :next_retry_at,
                updated_at = :updated_at
            WHERE status = 'dead'
            """,
            {"next_retry_at": now, "updated_at": now},
        )

        return {"items": dead_items, "episodes": dead_episodes}

    def _compute_health_status(
        self,
        fail_rate_24h: float,
        processed_24h: int,
        oldest_queue_age_s: float,
        total_queue: int,
    ) -> dict:
        reasons = []
        alert_fail_rate = float(settings.BACKGROUND_SCRAPER_ALERT_FAIL_RATE or 0.0)
        alert_queue_age = int(settings.BACKGROUND_SCRAPER_ALERT_QUEUE_AGE or 0)

        if (
            processed_24h >= 20
            and alert_fail_rate > 0
            and fail_rate_24h >= alert_fail_rate
        ):
            reasons.append("high_fail_rate_24h")
        if (
            total_queue > 0
            and alert_queue_age > 0
            and oldest_queue_age_s >= alert_queue_age
        ):
            reasons.append("old_queue_items")

        return {
            "status": "degraded" if reasons else "healthy",
            "reasons": reasons,
        }

    async def _insert_first_search(self, media_id: str):
        await database.execute(
            f"""
            INSERT {OR_IGNORE}
            INTO first_searches
            VALUES (:media_id, :timestamp)
            {ON_CONFLICT_DO_NOTHING}
            """,
            {"media_id": media_id, "timestamp": time.time()},
        )

    def _compute_backoff(self, failures: int) -> float:
        base = max(1, settings.BACKGROUND_SCRAPER_FAILURE_BASE_BACKOFF or 1)
        max_backoff = max(base, settings.BACKGROUND_SCRAPER_FAILURE_MAX_BACKOFF or base)
        exponent = max(0, failures - 1)
        return min(max_backoff, base * math.pow(2, exponent))

    def _max_retries_for_query(self) -> int:
        configured = settings.BACKGROUND_SCRAPER_MAX_RETRIES
        if configured is None or configured < 0:
            return 1000000
        return configured

    def _is_retry_limit_reached(self, failures: int) -> bool:
        configured = settings.BACKGROUND_SCRAPER_MAX_RETRIES
        if configured is None or configured < 0:
            return False
        return failures >= configured


background_scraper = BackgroundScraperWorker()
