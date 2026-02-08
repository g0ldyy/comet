import asyncio
import glob
import json
import os
import random
import re
import shutil
import time
import zipfile

import aiohttp
import RTN

from comet.core.database import ON_CONFLICT_DO_NOTHING, OR_IGNORE, database
from comet.core.execution import get_executor
from comet.core.logger import logger
from comet.core.models import settings
from comet.services.lock import DistributedLock
from comet.utils.lzstring import decompressFromEncodedURIComponent

DMM_URL = "https://github.com/debridmediamanager/hashlists/zipball/main/"
TEMP_DIR = "data/dmm_temp"
LOCK_KEY = "dmm_ingest_lock"
LOCK_TTL = 60


class DMMIngester:
    def __init__(self):
        self.is_running = False
        self.semaphore = None

    async def start(self):
        if not settings.DMM_INGEST_ENABLED:
            return

        if self.is_running:
            return

        logger.log("DMM_INGEST", "Starting DMM Ingester service")
        self.is_running = True
        self.semaphore = asyncio.Semaphore(settings.DMM_INGEST_CONCURRENT_WORKERS)
        await self._run_continuous()

    async def stop(self):
        self.is_running = False

    async def _run_continuous(self):
        while self.is_running:
            try:
                lock = DistributedLock(LOCK_KEY, timeout=LOCK_TTL)
                if await lock.acquire(wait_timeout=None):
                    try:
                        await self._ingest_cycle(lock)
                    finally:
                        await lock.release()
                else:
                    logger.log(
                        "DMM_INGEST",
                        "Another instance is performing ingestion. Skipping.",
                    )
            except Exception as e:
                logger.error(f"Error in DMM ingestion cycle: {e}")

            await asyncio.sleep(settings.DMM_INGEST_INTERVAL)

    async def _ingest_cycle(self, lock: DistributedLock):
        logger.log("DMM_INGEST", "Checking for DMM updates...")

        os.makedirs(TEMP_DIR, exist_ok=True)
        zip_path = os.path.join(TEMP_DIR, "dmm.zip")

        try:
            logger.log("DMM_INGEST", "Downloading DMM hashlists...")
            timeout = aiohttp.ClientTimeout(total=None, sock_read=300)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(DMM_URL) as response:
                    if response.status != 200:
                        logger.warning(
                            f"Failed to download DMM hashlists: {response.status}"
                        )
                        return

                    with open(zip_path, "wb") as f:
                        while True:
                            await lock.acquire()

                            chunk = await response.content.read(1024 * 1024 * 64)
                            if not chunk:
                                break
                            f.write(chunk)

            logger.log("DMM_INGEST", "Extracting DMM hashlists...")
            extract_dir = os.path.join(TEMP_DIR, "extracted")
            if os.path.exists(extract_dir):
                shutil.rmtree(extract_dir)

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                get_executor(), extract_zip_sync, zip_path, extract_dir
            )

            os.remove(zip_path)

            all_files = glob.glob(
                os.path.join(extract_dir, "**", "*.html"), recursive=True
            )
            new_files = await self._filter_new_files(all_files)

            total_files = len(new_files)
            logger.log(
                "DMM_INGEST",
                f"Found {len(all_files)} total files. {total_files} are new.",
            )

            total_inserted = 0
            batch_size = settings.DMM_INGEST_BATCH_SIZE

            for i in range(0, total_files, batch_size):
                if not self.is_running:
                    break

                await lock.acquire()

                batch_files = new_files[i : i + batch_size]

                logger.log(
                    "DMM_INGEST",
                    f"Processing batch {i // batch_size + 1}/{(total_files + batch_size - 1) // batch_size} ({len(batch_files)} files)",
                )

                try:

                    async def process_file_with_sem(fp):
                        async with self.semaphore:
                            return await loop.run_in_executor(
                                get_executor(), process_file_sync, fp
                            )

                    futures = [process_file_with_sem(fp) for fp in batch_files]
                    results = await asyncio.gather(*futures)

                    batch_entries = []
                    processed_files_batch = []
                    current_timestamp = int(time.time())

                    for file_path, entries in zip(batch_files, results):
                        if entries:
                            batch_entries.extend(entries)

                        processed_files_batch.append(
                            {
                                "filename": os.path.basename(file_path),
                                "timestamp": current_timestamp,
                            }
                        )

                    for attempt in range(3):
                        try:
                            if batch_entries:
                                await self._batch_insert(batch_entries)
                                total_inserted += len(batch_entries)

                            if processed_files_batch:
                                query_files = f"""
                                    INSERT {OR_IGNORE}
                                    INTO dmm_ingested_files (filename, timestamp) 
                                    VALUES (:filename, :timestamp)
                                    {ON_CONFLICT_DO_NOTHING}
                                """
                                await database.execute_many(
                                    query_files,
                                    processed_files_batch,
                                )
                            break
                        except Exception as e:
                            if "database is locked" in str(e).lower() and attempt < 2:
                                await asyncio.sleep(random.uniform(0.1, 0.5))
                                continue
                            raise e

                except Exception as e:
                    logger.error(f"Error processing batch starting at {i}: {e}")

            logger.log(
                "DMM_INGEST",
                f"Ingestion completed. Inserted/Updated {total_inserted} entries.",
            )
        finally:
            if os.path.exists(TEMP_DIR):
                shutil.rmtree(TEMP_DIR)

    async def _filter_new_files(self, all_files):
        processed_rows = await database.fetch_all(
            "SELECT filename FROM dmm_ingested_files"
        )
        processed_set = set(row["filename"] for row in processed_rows)

        return [f for f in all_files if os.path.basename(f) not in processed_set]

    async def _batch_insert(self, entries):
        chunk_size = 500
        for i in range(0, len(entries), chunk_size):
            chunk = entries[i : i + chunk_size]
            chunk_timestamp = int(time.time())

            values = []
            for entry in chunk:
                values.append(
                    {
                        "info_hash": entry["hash"],
                        "filename": entry["filename"],
                        "size": entry["size"],
                        "parsed_title": entry["parsed_title"],
                        "parsed_year": entry["parsed_year"],
                        "created_at": chunk_timestamp,
                    }
                )

            query = f"""
                INSERT {OR_IGNORE} INTO dmm_entries (info_hash, filename, size, parsed_title, parsed_year, created_at)
                VALUES (:info_hash, :filename, :size, :parsed_title, :parsed_year, :created_at)
                {ON_CONFLICT_DO_NOTHING}
            """

            await database.execute_many(query, values)


HASHLIST_REGEX = re.compile(r'hashlist#(.*?)"')


def process_file_sync(file_path):
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        match = HASHLIST_REGEX.search(content)
        if not match:
            return []

        encoded_data = match.group(1)
        json_str = decompressFromEncodedURIComponent(encoded_data)

        if not json_str:
            return []

        data = json.loads(json_str)

        results = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict) and "torrents" in data:
            items = data["torrents"]
        else:
            return []

        for item in items:
            filename = item.get("filename")
            info_hash = item.get("hash")
            size = item.get("bytes", 0)

            if not filename or not info_hash:
                continue

            try:
                filename.encode("utf-8")
            except UnicodeEncodeError:
                filename = filename.encode("utf-8", "ignore").decode("utf-8")

            parsed = RTN.parse(filename)

            results.append(
                {
                    "hash": info_hash,
                    "filename": filename,
                    "size": size,
                    "parsed_title": parsed.parsed_title,
                    "parsed_year": parsed.year,
                }
            )

        return results
    except Exception:
        return []


def extract_zip_sync(zip_path, extract_dir):
    with zipfile.ZipFile(zip_path, "r") as zip_ref:
        zip_ref.extractall(extract_dir)


dmm_ingester = DMMIngester()
