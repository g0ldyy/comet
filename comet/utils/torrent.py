import hashlib
import re
import bencodepy
import aiohttp
import anyio
import asyncio
import time

from RTN import parse
from urllib.parse import parse_qs, urlparse
from demagnetize.core import Demagnetizer
from torf import Magnet

from comet.utils.logger import logger
from comet.utils.models import settings, database
from comet.utils.general import is_video

info_hash_pattern = re.compile(r"btih:([a-fA-F0-9]{40})")


def extract_trackers_from_magnet(magnet_uri: str):
    try:
        parsed = urlparse(magnet_uri)
        params = parse_qs(parsed.query)
        return params.get("tr", [])
    except Exception as e:
        logger.warning(f"Failed to extract trackers from magnet URI: {e}")
        return []


async def download_torrent(session: aiohttp.ClientSession, url: str):
    try:
        timeout = aiohttp.ClientTimeout(total=settings.GET_TORRENT_TIMEOUT)
        async with session.get(url, allow_redirects=False, timeout=timeout) as response:
            if response.status == 200:
                return (await response.read(), None, None)

            location = response.headers.get("Location", "")
            if location:
                match = info_hash_pattern.search(location)
                if match:
                    return (None, match.group(1), location)
            return (None, None, None)
    except Exception as e:
        logger.warning(f"Failed to download torrent from {url}: {e}")
        return (None, None, None)


demagnetizer = Demagnetizer()


async def get_torrent_from_magnet(magnet_uri: str):
    try:
        magnet = Magnet.from_string(magnet_uri)
        with anyio.fail_after(60):
            torrent_data = await demagnetizer.demagnetize(magnet)
            if torrent_data:
                return torrent_data.dump()
    except Exception as e:
        logger.warning(f"Failed to get torrent from magnet: {e}")
        return None


def extract_torrent_metadata(content: bytes, season: str, episode: str):
    try:
        torrent_data = bencodepy.decode(content)
        info = torrent_data[b"info"]
        m = hashlib.sha1()
        info_hash = m.hexdigest()

        torrent_name = info.get(b"name", b"").decode()
        if not torrent_name:
            return {}

        announce_list = [
            tracker[0].decode() for tracker in torrent_data.get(b"announce-list", [])
        ]

        metadata = {
            "info_hash": info_hash.lower(),
            "announce_list": announce_list,
        }

        if b"files" in info:
            files = info[b"files"]
            file_data = []
            best_index = None
            best_score = -1
            best_size = 0
            is_movie = True

            for idx, file in enumerate(files):
                if b"path" in file:
                    path_parts = [part.decode() for part in file[b"path"]]
                    path = "/".join(path_parts)
                else:
                    path = file[b"name"].decode() if b"name" in file else ""

                if not path or not is_video(path):
                    continue

                size = file[b"length"]
                score = size

                file_parsed = parse(path)

                season_exists = len(file_parsed.seasons) != 0
                episode_exists = len(file_parsed.episodes) != 0

                if season_exists or episode_exists:
                    is_movie = False

                if (
                    season_exists
                    and episode_exists
                    and file_parsed.seasons[0] == season
                    and file_parsed.episodes[0] == episode
                ):
                    score *= 3

                file_info = {
                    "index": idx,
                    "size": size,
                    "season": file_parsed.seasons[0] if season_exists else None,
                    "episode": file_parsed.episodes[0] if episode_exists else None,
                }

                if score > best_score:
                    best_score = score
                    best_size = size
                    best_index = idx
                    best_file_info = file_info

                if not is_movie:
                    file_data.append(file_info)

            if is_movie and best_index is not None:
                file_data = [best_file_info]

            metadata.update(
                {
                    "file_data": file_data,
                    "file_index": best_index,
                    "file_size": best_size,
                }
            )
        else:
            name = info[b"name"].decode()
            if not is_video(name):
                return {}

            size = info[b"length"]

            file_parsed = parse(name)

            metadata.update(
                {
                    "file_index": 0,
                    "file_size": size,
                    "file_data": [
                        {
                            "index": 0,
                            "size": size,
                            "season": file_parsed.seasons[0]
                            if len(file_parsed.seasons) != 0
                            else None,
                            "episode": file_parsed.episodes[0]
                            if len(file_parsed.episodes) != 0
                            else None,
                        }
                    ],
                }
            )

        return metadata

    except Exception as e:
        logger.warning(f"Failed to extract torrent metadata: {e}")
        return {}


async def update_torrent_file_index(
    info_hash: str, season: str, episode: str, index: int, size: int
):
    try:
        if season is None and episode is None:
            existing = await database.fetch_one(
                """
                SELECT file_index, file_size
                FROM torrent_file_indexes 
                WHERE info_hash = :info_hash 
                AND season IS NULL
                AND episode IS NULL
                """,
                {"info_hash": info_hash},
            )  # for movies, we keep best file (largest size)

            if existing and existing["file_size"] >= size:
                return

            await database.execute(
                """
                DELETE FROM torrent_file_indexes 
                WHERE info_hash = :info_hash 
                AND season IS NULL
                AND episode IS NULL
                """,
                {"info_hash": info_hash},
            )

        await database.execute(
            f"""
            INSERT {'OR IGNORE ' if settings.DATABASE_TYPE == 'sqlite' else ''}
            INTO torrent_file_indexes 
            VALUES (:info_hash, :season, :episode, :file_index, :file_size, :timestamp)
            {' ON CONFLICT DO NOTHING' if settings.DATABASE_TYPE == 'postgresql' else ''}
            """,
            {
                "info_hash": info_hash,
                "season": season,
                "episode": episode,
                "file_index": index,
                "file_size": size,
                "timestamp": time.time(),
            },
        )

        # additional = (
        #     f" S{season:02d}E{episode:02d}"
        #     if season is not None and episode is not None
        #     else ""
        # )
        # logger.log(
        #     "SCRAPER",
        #     f"Updated file index and size for {info_hash}{additional}",
        # )
    except Exception as e:
        logger.warning(f"Failed to update file index for {info_hash}: {e}")


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
                                    await update_torrent_file_index(
                                        info_hash,
                                        file_info["season"],
                                        file_info["episode"],
                                        file_info["index"],
                                        file_info["size"],
                                    )
                    finally:
                        self.queue.task_done()

            except Exception:
                await asyncio.sleep(1)

        self.is_running = False


file_index_queue = FileIndexQueue()


class FileIndexUpdateQueue:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.is_running = False

    async def add_update(
        self, info_hash: str, season: str, episode: str, index: int, size: int
    ):
        await self.queue.put((info_hash, season, episode, index, size))
        if not self.is_running:
            self.is_running = True
            asyncio.create_task(self._process_queue())

    async def _process_queue(self):
        while self.is_running:
            try:
                info_hash, season, episode, index, size = await self.queue.get()
                try:
                    await update_torrent_file_index(
                        info_hash, season, episode, index, size
                    )
                finally:
                    self.queue.task_done()
            except Exception:
                await asyncio.sleep(1)

        self.is_running = False


file_index_update_queue = FileIndexUpdateQueue()
