import hashlib
import re
import bencodepy
import aiohttp
import anyio
import asyncio
import orjson

from urllib.parse import parse_qs, urlparse
from demagnetize.core import Demagnetizer
from torf import Magnet
from RTN import ParsedData

from comet.utils.logger import logger
from comet.utils.models import settings, database
from comet.utils.general import is_video, default_dump

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


def extract_torrent_metadata(content: bytes):
    try:
        torrent_data = bencodepy.decode(content)
        info = torrent_data[b"info"]
        info_encoded = bencodepy.encode(info)
        m = hashlib.sha1()
        m.update(info_encoded)
        info_hash = m.hexdigest()

        announce_list = [
            tracker[0].decode() for tracker in torrent_data.get(b"announce-list", [])
        ]

        metadata = {"info_hash": info_hash, "announce_list": announce_list, "files": []}

        files = info[b"files"] if b"files" in info else [info]
        for idx, file in enumerate(files):
            name = (
                file[b"path"][-1].decode()
                if b"path" in file
                else file[b"name"].decode()
            )

            if not is_video(name) or "sample" in name.lower():
                continue

            size = file[b"length"]

            metadata["files"].append({"index": idx, "name": name, "size": size})

        return metadata

    except Exception as e:
        logger.warning(f"Failed to extract torrent metadata: {e}")
        return {}


async def update_torrent_file_index(
    info_hash: str,
    season: int,
    episode: int,
    index: int,
    title: str,
    size: int,
    parsed: ParsedData,
):
    try:
        season = season if season != "n" else None
        episode = episode if episode != "n" else None

        await database.execute(
            """
            UPDATE torrents
            SET file_index = :index,
                title = :title,
                size = :size,
                parsed = :parsed
            WHERE info_hash = :info_hash
            AND ((cast(:season as INTEGER) IS NULL AND season IS NULL) OR season = cast(:season as INTEGER))
            AND ((cast(:episode as INTEGER) IS NULL AND episode IS NULL) OR episode = cast(:episode as INTEGER))
            """,
            {
                "index": index,
                "title": title,
                "size": size,
                "parsed": orjson.dumps(parsed, default=default_dump).decode("utf-8"),
                "info_hash": info_hash,
                "season": season,
                "episode": episode,
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
            FROM torrents 
            WHERE info_hash = :info_hash 
            AND ((cast(:season as INTEGER) IS NULL AND season IS NULL) OR season = cast(:season as INTEGER))
            AND ((cast(:episode as INTEGER) IS NULL AND episode IS NULL) OR episode = cast(:episode as INTEGER))
            AND file_index IS NOT NULL
            """,
            {
                "info_hash": info_hash,
                "season": season,
                "episode": episode,
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
                info_hash, magnet_url = await self.queue.get()

                async with self.semaphore:
                    try:
                        content = await get_torrent_from_magnet(magnet_url)
                        if content:
                            metadata = extract_torrent_metadata(content)
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
        self,
        info_hash: str,
        season: str,
        episode: str,
        index: int,
        title: str,
        size: int,
        parsed: ParsedData,
    ):
        await self.queue.put((info_hash, season, episode, index, title, size, parsed))
        if not self.is_running:
            self.is_running = True
            asyncio.create_task(self._process_queue())

    async def _process_queue(self):
        while self.is_running:
            try:
                (
                    info_hash,
                    season,
                    episode,
                    index,
                    title,
                    size,
                    parsed,
                ) = await self.queue.get()
                try:
                    await update_torrent_file_index(
                        info_hash, season, episode, index, title, size, parsed
                    )
                finally:
                    self.queue.task_done()
            except Exception:
                await asyncio.sleep(1)

        self.is_running = False


file_index_update_queue = FileIndexUpdateQueue()
