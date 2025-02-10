import asyncio
import time

from comet.utils.models import database, settings
from comet.utils.logger import logger
from comet.utils.torrent import get_torrent_from_magnet, extract_torrent_metadata


class FileIndexQueue:
    def __init__(self, max_concurrent: int = 10):
        self.queue = asyncio.Queue()
        self.max_concurrent = max_concurrent
        self.is_running = False
        self.semaphore = asyncio.Semaphore(max_concurrent)

    async def add_torrent(
        self, info_hash: str, magnet_url: str, season: int, episode: int
    ):
        if not settings.DOWNLOAD_TORRENTS:
            return

        cached = await database.fetch_one(
            """
            SELECT file_index 
            FROM torrent_file_indexes 
            WHERE info_hash = :info_hash 
            AND ((cast(:season as INTEGER) IS NULL AND season IS NULL) OR season = cast(:season as INTEGER))
            AND ((cast(:episode as INTEGER) IS NULL AND episode IS NULL) OR episode = cast(:episode as INTEGER))
            AND timestamp + :cache_ttl >= :current_time
            """,
            {
                "info_hash": info_hash,
                "season": season,
                "episode": episode,
                "cache_ttl": settings.CACHE_TTL,
                "current_time": time.time(),
            },
        )
        if cached:
            return

        await self.queue.put((info_hash, magnet_url, season, episode))
        if not self.is_running:
            self.is_running = True
            asyncio.create_task(self._process_queue())

    async def _process_queue(self):
        while self.is_running:
            try:
                info_hash, magnet_url, season, episode = await self.queue.get()

                async with self.semaphore:
                    try:
                        content = await get_torrent_from_magnet(magnet_url)
                        if content:
                            metadata = extract_torrent_metadata(
                                content, season, episode
                            )
                            if metadata and "file_data" in metadata:
                                for file_info in metadata["file_data"]:
                                    file_season = file_info["season"]
                                    file_episode = file_info["episode"]

                                    await database.execute(
                                        f"""
                                        INSERT {'OR REPLACE ' if settings.DATABASE_TYPE == 'sqlite' else ''}
                                        INTO torrent_file_indexes 
                                        VALUES (:info_hash, :season, :episode, :file_index, :file_size, :timestamp)
                                        {' ON CONFLICT DO NOTHING' if settings.DATABASE_TYPE == 'postgresql' else ''}
                                        """,
                                        {
                                            "info_hash": info_hash,
                                            "season": file_season,
                                            "episode": file_episode,
                                            "file_index": file_info["index"],
                                            "file_size": file_info["size"],
                                            "timestamp": time.time(),
                                        },
                                    )

                                additional = (
                                    f" S{file_season:02d}E{file_episode:02d}"
                                    if file_season and file_episode
                                    else ""
                                )
                                logger.log(
                                    "SCRAPER",
                                    f"Updated file index and size for {info_hash}{additional}",
                                )
                    except:
                        pass
                    finally:
                        self.queue.task_done()

            except:
                await asyncio.sleep(1)

        self.is_running = False


file_index_queue = FileIndexQueue()
