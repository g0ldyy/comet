import asyncio
import time
import traceback
from dataclasses import dataclass

import aiohttp

from comet.core.database import ON_CONFLICT_DO_NOTHING, OR_IGNORE, database
from comet.core.logger import logger
from comet.core.models import settings
from comet.metadata.manager import MetadataScraper
from comet.services.lock import DistributedLock
from comet.services.orchestration import TorrentManager

from .cinemata_client import CinemataClient

LOCK_KEY = "background_scraper_lock"
LOCK_TTL = 60


@dataclass
class ScrapingStats:
    total_processed: int = 0
    total_torrents_found: int = 0
    errors: int = 0
    start_time: float = 0.0

    @property
    def duration(self):
        return time.time() - self.start_time if self.start_time else 0


class BackgroundScraperWorker:
    def __init__(self):
        self.is_running = False
        self.current_session = None
        self.metadata_scraper = None
        self.semaphore = None
        self.stats = ScrapingStats()

    async def start(self):
        if self.is_running:
            logger.log("BACKGROUND_SCRAPER", "Background scraper is already running")
            return

        logger.log("BACKGROUND_SCRAPER", "Starting background scraper")
        await self._run_continuous()

    async def stop(self):
        logger.log("BACKGROUND_SCRAPER", "Stopping background scraper")
        self.is_running = False

        if self.current_session:
            await self.current_session.close()

    async def _run_continuous(self):
        self.is_running = True

        interval_seconds = settings.BACKGROUND_SCRAPER_INTERVAL

        while self.is_running:
            try:
                lock = DistributedLock(LOCK_KEY, timeout=LOCK_TTL)
                if await lock.acquire(wait_timeout=None):
                    try:
                        lock_task = asyncio.create_task(self._maintain_lock(lock))
                        scrape_task = asyncio.create_task(self._run_scraping_cycle())

                        done, pending = await asyncio.wait(
                            [lock_task, scrape_task],
                            return_when=asyncio.FIRST_COMPLETED,
                        )

                        if lock_task in done:
                            logger.log(
                                "BACKGROUND_SCRAPER",
                                "Lock lost during processing. Stopping current cycle.",
                            )
                            scrape_task.cancel()
                            try:
                                await scrape_task
                            except asyncio.CancelledError:
                                pass
                        else:
                            lock_task.cancel()
                            try:
                                await lock_task
                            except asyncio.CancelledError:
                                pass

                            if scrape_task.exception():
                                raise scrape_task.exception()
                    finally:
                        await lock.release()
                else:
                    logger.log(
                        "BACKGROUND_SCRAPER",
                        "Another instance is running. Skipping.",
                    )

                if self.is_running:
                    logger.log(
                        "BACKGROUND_SCRAPER",
                        f"Waiting {interval_seconds}s until next run",
                    )
                    await asyncio.sleep(interval_seconds)
            except asyncio.CancelledError:
                self.is_running = False
            except Exception as e:
                logger.error(f"Error in background scraper cycle: {e}")
                await asyncio.sleep(300)

    async def _maintain_lock(self, lock: DistributedLock):
        try:
            while True:
                await asyncio.sleep(LOCK_TTL / 2)
                if not await lock.acquire():
                    logger.warning("BACKGROUND_SCRAPER: Failed to renew lock")
                    return
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error in lock maintenance: {e}")
            return

    async def _run_scraping_cycle(self):
        self.stats = ScrapingStats()
        self.stats.start_time = time.time()

        try:
            self.current_session = aiohttp.ClientSession()
            self.metadata_scraper = MetadataScraper(self.current_session)
            self.semaphore = asyncio.Semaphore(
                settings.BACKGROUND_SCRAPER_CONCURRENT_WORKERS
            )

            logger.log(
                "BACKGROUND_SCRAPER",
                f"Starting scraping cycle with {settings.BACKGROUND_SCRAPER_CONCURRENT_WORKERS} concurrent workers",
            )

            await self._scrape_media_type(
                "movie", settings.BACKGROUND_SCRAPER_MAX_MOVIES_PER_RUN
            )

            await self._scrape_media_type(
                "series", settings.BACKGROUND_SCRAPER_MAX_SERIES_PER_RUN
            )

            logger.log(
                "BACKGROUND_SCRAPER",
                f"Scraping cycle completed. Processed: {self.stats.total_processed}, "
                f"Torrents found: {self.stats.total_torrents_found}, "
                f"Errors: {self.stats.errors}, "
                f"Duration: {self.stats.duration:.2f}s",
            )

        except Exception as e:
            logger.error(f"Error in scraping cycle: {e}")
            traceback.print_exc()
        finally:
            if self.current_session:
                await self.current_session.close()
                self.current_session = None

    async def _scrape_media_type(self, media_type: str, max_items: int) -> None:
        if max_items <= 0:
            logger.log(
                "BACKGROUND_SCRAPER",
                f"Skipping {media_type} scraping (max_items={max_items})",
            )
            return

        logger.log(
            "BACKGROUND_SCRAPER", f"Starting {media_type} scraping (max: {max_items})"
        )

        async with CinemataClient() as cinemata_client:
            processed_count = 0
            tasks = []

            media_generator = cinemata_client.fetch_all_of_type(media_type)

            async for media_item in media_generator:
                if not self.is_running or processed_count >= max_items:
                    break

                if await self._should_skip_media(media_item["imdb_id"]):
                    continue

                task = asyncio.create_task(
                    self._scrape_single_media(media_item, media_type)
                )
                tasks.append(task)

                processed_count += 1

                if len(tasks) >= settings.BACKGROUND_SCRAPER_CONCURRENT_WORKERS * 2:
                    await asyncio.gather(*tasks, return_exceptions=True)
                    tasks.clear()

            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        logger.log(
            "BACKGROUND_SCRAPER",
            f"Completed {media_type} scraping. Processed: {processed_count} unique items",
        )

    async def _should_skip_media(self, media_id: str):
        """Check if media should be skipped (recently scraped or too many failures)."""
        row = await database.fetch_one(
            "SELECT scraped_at, scrape_failed_attempts FROM background_scraper_state WHERE media_id = :media_id",
            {"media_id": media_id},
        )

        if not row:
            return False

        # Skip if scraped within the last 7 days
        seven_days_ago = time.time() - (7 * 24 * 3600)
        if row["scraped_at"] and row["scraped_at"] > seven_days_ago:
            return True

        # Skip if too many failed attempts (more than 3)
        if row["scrape_failed_attempts"] and row["scrape_failed_attempts"] > 3:
            return True

        return False

    async def _scrape_single_media(self, media_item, media_type: str):
        async with self.semaphore:
            media_id = media_item["imdb_id"]
            title = media_item["name"]

            year = media_item["year"]
            year_end = None
            if "–" in year:
                splitted = year.split("–")
                if splitted[1]:
                    year = int(splitted[0])
                    year_end = int(splitted[1])
                else:
                    year = int(splitted[0])
            else:
                year = int(year)

            torrents_found = 0

            try:
                logger.log(
                    "BACKGROUND_SCRAPER",
                    f"Scraping {media_type}: {title} ({year}) - {media_id}",
                )

                if media_type == "series":
                    torrents_found = await self._scrape_series_episodes(
                        media_id, title, year, year_end, media_item.get("videos", [])
                    )
                else:
                    torrents_found = await self._scrape_movie(media_id, title, year)

                self.stats.total_torrents_found += torrents_found

                increment_attempt = 1 if torrents_found == 0 else 0
            except Exception as e:
                self.stats.errors += 1
                increment_attempt = 1

                logger.error(f"Error scraping {media_type} {media_id}: {e}")

            await database.execute(
                """
                INSERT INTO background_scraper_state 
                (media_id, media_type, title, year, scraped_at, total_torrents_found, 
                scrape_failed_attempts)
                VALUES (:media_id, :media_type, :title, :year, :scraped_at, 
                        :torrents_found, :increment_attempt)
                ON CONFLICT (media_id) DO UPDATE SET
                    scraped_at = :scraped_at,
                    total_torrents_found = :torrents_found,
                    scrape_failed_attempts = background_scraper_state.scrape_failed_attempts + :increment_attempt
                """,
                {
                    "media_id": media_id,
                    "media_type": media_type,
                    "title": title,
                    "year": year,
                    "scraped_at": time.time(),
                    "torrents_found": torrents_found,
                    "increment_attempt": increment_attempt,
                },
            )

            # Add to first_searches table so Stremio won't re-scrape this item
            # This marks the media as "already searched" to avoid duplicate scraping
            if torrents_found > 0:
                await database.execute(
                    f"""
                    INSERT {OR_IGNORE}
                    INTO first_searches 
                    VALUES (:media_id, :timestamp)
                    {ON_CONFLICT_DO_NOTHING}
                    """,
                    {"media_id": media_id, "timestamp": time.time()},
                )

            logger.log(
                "BACKGROUND_SCRAPER",
                f"✅ Successfully scraped {media_id} - {torrents_found} torrents found",
            )

            self.stats.total_processed += 1

    async def _scrape_movie(self, media_id: str, title: str, year: int):
        metadata, aliases = await self.metadata_scraper.fetch_aliases_with_metadata(
            "movie", media_id, title, year, id=media_id
        )

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
        return len(manager.torrents)

    async def _scrape_series_episodes(
        self, media_id: str, title: str, year: int, year_end: int, episodes: list
    ):
        total_torrents = 0

        series_media_id = f"{media_id}:1:1"

        metadata, aliases = await self.metadata_scraper.fetch_aliases_with_metadata(
            "series", series_media_id, title, year, year_end, id=media_id
        )

        for episode in episodes:
            season = episode["season"]
            episode_number = episode.get("episode")
            if episode_number is None:
                episode_number = episode.get("number")
            episode_media_id = f"{media_id}:{season}:{episode_number}"

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
            total_torrents += episode_torrents

            # Mark this specific episode as searched to avoid re-scraping
            if episode_torrents > 0:
                await database.execute(
                    f"""
                    INSERT {OR_IGNORE}
                    INTO first_searches
                    VALUES (:media_id, :timestamp)
                    {ON_CONFLICT_DO_NOTHING}
                    """,
                    {"media_id": episode_media_id, "timestamp": time.time()},
                )

            logger.log(
                "BACKGROUND_SCRAPER",
                f"✅ Successfully scraped {episode_media_id} - {episode_torrents} torrents found",
            )

        return total_torrents


background_scraper = BackgroundScraperWorker()
