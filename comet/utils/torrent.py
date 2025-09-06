import hashlib
import re
import bencodepy
import aiohttp
import anyio
import asyncio
import orjson
import time
import base64
import html

from urllib.parse import parse_qs, urlparse
from demagnetize.core import Demagnetizer
from torf import Magnet
from RTN import ParsedData, parse

from comet.utils.logger import logger
from comet.utils.models import settings, database
from comet.utils.general import is_video, default_dump

info_hash_pattern = re.compile(r"btih:([a-fA-F0-9]{40}|[a-zA-Z0-9]{32})")


def extract_trackers_from_magnet(magnet_uri: str):
    try:
        decoded_uri = html.unescape(magnet_uri)
        parsed = urlparse(decoded_uri)
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
                    info_hash = match.group(1)
                    if len(info_hash) == 32:
                        info_hash = base64.b16encode(
                            base64.b32decode(info_hash)
                        ).decode("utf-8")
                    return (None, info_hash, location)
            return (None, None, None)
    except Exception as e:
        logger.warning(
            f"Failed to download torrent from {url}: {e} (in most cases, you can ignore this error)"
        )
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
        announce = torrent_data.get(b"announce", b"").decode()
        if announce:
            announce_list.append(announce)

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


async def add_torrent(
    info_hash: str,
    seeders: int,
    tracker: str,
    media_id: str,
    search_season: int,
    sources: list,
    file_index: int,
    title: str,
    size: int,
    parsed: ParsedData,
):
    try:
        parsed_season = parsed.seasons[0] if parsed.seasons else search_season
        parsed_episode = parsed.episodes[0] if parsed.episodes else None

        if parsed_episode is not None:
            await database.execute(
                """
                DELETE FROM torrents
                WHERE info_hash = :info_hash
                AND season = :season 
                AND episode IS NULL
                """,
                {
                    "info_hash": info_hash,
                    "season": parsed_season,
                },
            )
            logger.log(
                "SCRAPER",
                f"Deleted season-only entry for S{parsed_season:02d} of {info_hash}",
            )

        await database.execute(
            f"""
                INSERT {"OR IGNORE " if settings.DATABASE_TYPE == "sqlite" else ""}
                INTO torrents
                VALUES (:media_id, :info_hash, :file_index, :season, :episode, :title, :seeders, :size, :tracker, :sources, :parsed, :timestamp)
                {" ON CONFLICT DO NOTHING" if settings.DATABASE_TYPE == "postgresql" else ""}
            """,
            {
                "media_id": media_id,
                "info_hash": info_hash,
                "file_index": file_index,
                "season": parsed_season,
                "episode": parsed_episode,
                "title": title,
                "seeders": seeders,
                "size": size,
                "tracker": tracker,
                "sources": orjson.dumps(sources).decode("utf-8"),
                "parsed": orjson.dumps(parsed, default_dump).decode("utf-8"),
                "timestamp": time.time(),
            },
        )

        additional = ""
        if parsed_season:
            additional += f" - S{parsed_season:02d}"
            additional += f"E{parsed_episode:02d}" if parsed_episode else ""

        logger.log("SCRAPER", f"Added torrent for {media_id} - {title}{additional}")
    except Exception as e:
        logger.warning(f"Failed to add torrent for {info_hash}: {e}")


class AddTorrentQueue:
    def __init__(self, max_concurrent: int = 10):
        self.queue = asyncio.Queue()
        self.max_concurrent = max_concurrent
        self.is_running = False
        self.semaphore = asyncio.Semaphore(max_concurrent)

    async def add_torrent(
        self,
        magnet_url: str,
        seeders: int,
        tracker: str,
        media_id: str,
        search_season: int,
    ):
        if not settings.DOWNLOAD_TORRENT_FILES:
            return

        await self.queue.put((magnet_url, seeders, tracker, media_id, search_season))
        if not self.is_running:
            self.is_running = True
            asyncio.create_task(self._process_queue())

    async def _process_queue(self):
        while self.is_running:
            try:
                (
                    magnet_url,
                    seeders,
                    tracker,
                    media_id,
                    search_season,
                ) = await self.queue.get()

                async with self.semaphore:
                    try:
                        content = await get_torrent_from_magnet(magnet_url)
                        if content:
                            metadata = extract_torrent_metadata(content)
                            for file in metadata["files"]:
                                parsed = parse(file["name"])

                                await add_torrent(
                                    metadata["info_hash"],
                                    seeders,
                                    tracker,
                                    media_id,
                                    search_season,
                                    metadata["announce_list"],
                                    file["index"],
                                    file["name"],
                                    file["size"],
                                    parsed,
                                )
                    finally:
                        self.queue.task_done()

            except Exception:
                await asyncio.sleep(1)

        self.is_running = False


add_torrent_queue = AddTorrentQueue()


class TorrentUpdateQueue:
    def __init__(self, batch_size: int = 100, flush_interval: float = 5.0):
        self.queue = asyncio.Queue()
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.is_running = False
        self.batches = {"to_check": [], "to_delete": [], "inserts": [], "updates": []}

    async def add_torrent_info(self, file_info: dict, media_id: str = None):
        await self.queue.put((file_info, media_id))
        if not self.is_running:
            self.is_running = True
            asyncio.create_task(self._process_queue())

    async def _process_queue(self):
        last_flush_time = time.time()

        while self.is_running:
            try:
                while not self.queue.empty():
                    try:
                        file_info, media_id = self.queue.get_nowait()
                        await self._process_file_info(file_info, media_id)
                    except asyncio.QueueEmpty:
                        break

                current_time = time.time()

                if (
                    current_time - last_flush_time >= self.flush_interval
                    or self.queue.empty()
                ) and any(len(batch) > 0 for batch in self.batches.values()):
                    await self._flush_batch()
                    last_flush_time = current_time

                if self.queue.empty() and not any(
                    len(batch) > 0 for batch in self.batches.values()
                ):
                    self.is_running = False
                    break

                await asyncio.sleep(0.1)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Error in _process_queue: {e}")
                self._reset_batches()

        if any(len(batch) > 0 for batch in self.batches.values()):
            await self._flush_batch()

        self.is_running = False

    def _reset_batches(self):
        for key in self.batches:
            if len(self.batches[key]) > 0:
                logger.warning(
                    f"Ignoring {len(self.batches[key])} items in problematic '{key}' batch"
                )
                self.batches[key] = []

    async def _flush_batch(self):
        try:
            if self.batches["to_check"]:
                sub_batch_size = 100
                for i in range(0, len(self.batches["to_check"]), sub_batch_size):
                    try:
                        sub_batch = self.batches["to_check"][i : i + sub_batch_size]
                        placeholders = []
                        params = {}

                        for idx, item in enumerate(sub_batch):
                            key = str(idx)
                            placeholders.append(
                                f"(CAST(:info_hash_{key} AS TEXT) = info_hash AND "
                                f"(CAST(:season_{key} AS INTEGER) IS NULL AND season IS NULL OR season = CAST(:season_{key} AS INTEGER)) AND "
                                f"(CAST(:episode_{key} AS INTEGER) IS NULL AND episode IS NULL OR episode = CAST(:episode_{key} AS INTEGER)))"
                            )
                            params[f"info_hash_{key}"] = item["info_hash"]
                            params[f"season_{key}"] = item["season"]
                            params[f"episode_{key}"] = item["episode"]

                        query = f"""
                            SELECT info_hash, season, episode
                            FROM torrents 
                            WHERE {" OR ".join(placeholders)}
                        """

                        async with database.transaction():
                            existing_rows = await database.fetch_all(query, params)

                            existing_set = {
                                (
                                    row["info_hash"],
                                    row["season"]
                                    if row["season"] is not None
                                    else None,
                                    row["episode"]
                                    if row["episode"] is not None
                                    else None,
                                )
                                for row in existing_rows
                            }

                            for item in sub_batch:
                                key = (
                                    item["info_hash"],
                                    item["season"],
                                    item["episode"],
                                )
                                if key in existing_set:
                                    self.batches["updates"].append(item["params"])
                                else:
                                    self.batches["inserts"].append(item["params"])
                    except Exception as e:
                        logger.warning(f"Error processing check batch: {e}")

                self.batches["to_check"] = []

            if self.batches["to_delete"]:
                sub_batch_size = 100
                for i in range(0, len(self.batches["to_delete"]), sub_batch_size):
                    try:
                        sub_batch = self.batches["to_delete"][i : i + sub_batch_size]

                        placeholders = []
                        params = {}
                        for idx, item in enumerate(sub_batch):
                            key_suffix = f"_{idx}"
                            placeholders.append(
                                f"(CAST(:info_hash{key_suffix} AS TEXT), CAST(:season{key_suffix} AS INTEGER))"
                            )
                            params[f"info_hash{key_suffix}"] = item["info_hash"]
                            params[f"season{key_suffix}"] = item["season"]

                        async with database.transaction():
                            delete_query = f"""
                                DELETE FROM torrents
                                WHERE (info_hash, season) IN (
                                    {",".join(placeholders)}
                                )
                                AND episode IS NULL
                            """
                            await database.execute(delete_query, params)
                    except Exception as e:
                        logger.warning(f"Error processing delete batch: {e}")

                self.batches["to_delete"] = []

            if self.batches["inserts"]:
                sub_batch_size = 100
                for i in range(0, len(self.batches["inserts"]), sub_batch_size):
                    try:
                        sub_batch = self.batches["inserts"][i : i + sub_batch_size]
                        async with database.transaction():
                            insert_query = f"""
                                INSERT {"OR IGNORE " if settings.DATABASE_TYPE == "sqlite" else ""}
                                INTO torrents
                                VALUES (
                                    :media_id,
                                    :info_hash,
                                    :file_index,
                                    :season,
                                    :episode,
                                    :title,
                                    :seeders,
                                    :size,
                                    :tracker,
                                    :sources,
                                    :parsed,
                                    :timestamp
                                )
                                {" ON CONFLICT DO NOTHING" if settings.DATABASE_TYPE == "postgresql" else ""}
                            """
                            await database.execute_many(insert_query, sub_batch)
                    except Exception as e:
                        logger.warning(f"Error processing insert batch: {e}")

                if len(self.batches["inserts"]) > 0:
                    logger.log(
                        "SCRAPER",
                        f"Inserted {len(self.batches['inserts'])} new torrents in batch",
                    )
                self.batches["inserts"] = []

            if self.batches["updates"]:
                sub_batch_size = 100
                for i in range(0, len(self.batches["updates"]), sub_batch_size):
                    try:
                        sub_batch = self.batches["updates"][i : i + sub_batch_size]
                        async with database.transaction():
                            update_query = """
                                UPDATE torrents 
                                SET title = CAST(:title AS TEXT),
                                    file_index = CAST(:file_index AS INTEGER),
                                    size = CAST(:size AS BIGINT),
                                    seeders = CAST(:seeders AS INTEGER),
                                    tracker = CAST(:tracker AS TEXT),
                                    sources = CAST(:sources AS TEXT),
                                    parsed = CAST(:parsed AS TEXT),
                                    timestamp = CAST(:timestamp AS FLOAT),
                                    media_id = CAST(:media_id AS TEXT)
                                WHERE info_hash = CAST(:info_hash AS TEXT)
                                AND (CAST(:season AS INTEGER) IS NULL AND season IS NULL OR season = CAST(:season AS INTEGER))
                                AND (CAST(:episode AS INTEGER) IS NULL AND episode IS NULL OR episode = CAST(:episode AS INTEGER))
                            """
                            await database.execute_many(update_query, sub_batch)
                    except Exception as e:
                        logger.warning(f"Error processing update batch: {e}")

                if len(self.batches["updates"]) > 0:
                    logger.log(
                        "SCRAPER",
                        f"Updated {len(self.batches['updates'])} existing torrents in batch",
                    )
                self.batches["updates"] = []

        except Exception as e:
            logger.warning(f"Error in flush_batch: {e}")
            self._reset_batches()

    async def _process_file_info(self, file_info: dict, media_id: str = None):
        try:
            params = {
                "info_hash": file_info["info_hash"],
                "file_index": file_info["index"],
                "season": file_info["season"],
                "episode": file_info["episode"],
                "title": file_info["title"],
                "seeders": file_info["seeders"],
                "size": file_info["size"],
                "tracker": file_info["tracker"],
                "sources": orjson.dumps(file_info["sources"]).decode("utf-8"),
                "parsed": orjson.dumps(
                    file_info["parsed"], default=default_dump
                ).decode("utf-8"),
                "timestamp": time.time(),
                "media_id": media_id,
            }

            self.batches["to_check"].append(
                {
                    "info_hash": file_info["info_hash"],
                    "season": file_info["season"],
                    "episode": file_info["episode"],
                    "params": params,
                }
            )

            if file_info["episode"] is not None:
                self.batches["to_delete"].append(
                    {"info_hash": file_info["info_hash"], "season": file_info["season"]}
                )

            await self._check_batch_size()

        except Exception as e:
            logger.warning(f"Error processing file info: {e}")
        finally:
            self.queue.task_done()

    async def _check_batch_size(self):
        if any(len(batch) >= self.batch_size for batch in self.batches.values()):
            await self._flush_batch()


torrent_update_queue = TorrentUpdateQueue()
