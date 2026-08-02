import asyncio
import glob
import os
import re
import shutil
import stat
import time
import zipfile
from pathlib import Path

import aiofiles
import aiohttp
import orjson
import RTN

from comet.core.database import database, is_retryable_database_error
from comet.core.execution import get_executor
from comet.core.models import settings
from comet.core.provider_json import is_success_status
from comet.observability import log, run_context
from comet.services.lock import DistributedLock
from comet.utils.lzstring import decompressFromEncodedURIComponent

DMM_URL = "https://codeload.github.com/debridmediamanager/hashlists/zip/refs/heads/main"
TEMP_DIR = "data/dmm_temp"
LOCK_KEY = "dmm_ingest_lock"
LOCK_TTL = 60
_MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 100_000
_MAX_ARCHIVE_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
_MAX_ARCHIVE_MEMBER_BYTES = 64 * 1024 * 1024
_MAX_ARCHIVE_PATH_BYTES = 1_024
_MAX_HASHLIST_ITEMS = 100_000
_MAX_FILENAME_BYTES = 1_024
_MAX_SIGNED_64 = 2**63 - 1
_INFO_HASH = re.compile(r"[0-9a-fA-F]{40}")


class DMMIngester:
    def __init__(self):
        self.is_running = False
        self.semaphore = None
        self._configuration_changed = asyncio.Event()

    async def start(self):
        if not settings.DMM_INGEST_ENABLED:
            return

        if self.is_running:
            return
        self.is_running = True
        self.semaphore = asyncio.Semaphore(settings.DMM_INGEST_CONCURRENT_WORKERS)
        await self._run_continuous()

    async def stop(self):
        self.is_running = False

    def reconfigure(self, config) -> None:
        self.semaphore = asyncio.Semaphore(config.DMM_INGEST_CONCURRENT_WORKERS)
        self._configuration_changed.set()

    async def _run_continuous(self):
        while self.is_running:
            self._configuration_changed.clear()
            try:
                lock = DistributedLock(LOCK_KEY, timeout=LOCK_TTL)
                if await lock.acquire(wait_timeout=None):
                    try:
                        await lock.run(self._ingest_cycle())
                    finally:
                        await lock.release()
            except Exception as exc:
                log.error(
                    "dmm.loop.failed",
                    "DMM ingestion loop failed",
                    error_code="ingestion_loop_failure",
                    exc=exc,
                )

            try:
                await asyncio.wait_for(
                    self._configuration_changed.wait(),
                    timeout=settings.DMM_INGEST_INTERVAL,
                )
            except TimeoutError:
                pass

    async def _ingest_cycle(self):
        with run_context():
            started_at = time.monotonic_ns()
            log.info(
                "dmm.run.started",
                "DMM ingestion run started",
            )
            try:
                total_files, total_inserted = await self._ingest_cycle_body()
            except asyncio.CancelledError:
                log.terminal(
                    "dmm.run.completed",
                    "DMM ingestion run completed",
                    outcome="cancelled",
                    duration_ms=(time.monotonic_ns() - started_at) / 1_000_000,
                )
                raise
            except Exception as exc:
                log.terminal(
                    "dmm.run.completed",
                    "DMM ingestion run completed",
                    outcome="failed",
                    duration_ms=(time.monotonic_ns() - started_at) / 1_000_000,
                    error_code="ingestion_failure",
                    exc=exc,
                )
            else:
                log.terminal(
                    "dmm.run.completed",
                    "DMM ingestion run completed",
                    outcome="ok",
                    item_count=total_files,
                    success_count=total_inserted,
                    duration_ms=(time.monotonic_ns() - started_at) / 1_000_000,
                )

    async def _ingest_cycle_body(self) -> tuple[int, int]:
        os.makedirs(TEMP_DIR, exist_ok=True)
        zip_path = os.path.join(TEMP_DIR, "dmm.zip")
        total_files = 0
        total_inserted = 0

        try:
            timeout = aiohttp.ClientTimeout(total=600, connect=30, sock_read=300)
            async with aiohttp.ClientSession(
                timeout=timeout,
            ) as session:
                async with session.get(
                    DMM_URL,
                    allow_redirects=False,
                    headers={
                        "Accept": "application/zip",
                        "Accept-Encoding": "identity",
                    },
                ) as response:
                    if not is_success_status(response.status):
                        raise RuntimeError("DMM archive download failed")
                    downloaded = 0
                    async with aiofiles.open(zip_path, "wb") as f:
                        while True:
                            chunk = await response.content.read(1024 * 1024)
                            if not chunk:
                                break
                            if len(chunk) > _MAX_DOWNLOAD_BYTES - downloaded:
                                raise ValueError("DMM archive download is too large")
                            await f.write(chunk)
                            downloaded += len(chunk)
                    if not downloaded:
                        raise ValueError("DMM archive download is empty")
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

            batch_size = settings.DMM_INGEST_BATCH_SIZE

            for i in range(0, total_files, batch_size):
                if not self.is_running:
                    break

                batch_files = new_files[i : i + batch_size]

                async def process_file_with_sem(fp):
                    async with self.semaphore:
                        return await loop.run_in_executor(
                            get_executor(), process_file_sync, fp
                        )

                futures = [process_file_with_sem(fp) for fp in batch_files]
                results = await asyncio.gather(*futures)

                batch_entries = []
                processed_files_batch = []
                for file_path, entries in zip(batch_files, results):
                    if entries is None:
                        continue
                    if entries:
                        batch_entries.extend(entries)

                    processed_files_batch.append(
                        {
                            "filename": os.path.basename(file_path),
                        }
                    )

                for attempt in range(3):
                    try:
                        if batch_entries:
                            await self._batch_insert(batch_entries)
                            total_inserted += len(batch_entries)

                        if processed_files_batch:
                            query_files = """
                                INSERT INTO dmm_ingested_files (filename)
                                VALUES (:filename)
                                ON CONFLICT DO NOTHING
                            """
                            await database.execute_many(
                                query_files,
                                processed_files_batch,
                            )
                        break
                    except Exception as exc:
                        if is_retryable_database_error(exc) and attempt < 2:
                            await asyncio.sleep(0.1 * (attempt + 1))
                            continue
                        raise
            return total_files, total_inserted
        finally:
            if os.path.exists(TEMP_DIR):
                shutil.rmtree(TEMP_DIR)

    async def _filter_new_files(self, all_files):
        processed_rows = await database.fetch_all(
            "SELECT filename FROM dmm_ingested_files"
        )
        processed_set = {row["filename"] for row in processed_rows}

        return [f for f in all_files if os.path.basename(f) not in processed_set]

    async def _batch_insert(self, entries):
        chunk_size = 500
        for i in range(0, len(entries), chunk_size):
            chunk = entries[i : i + chunk_size]
            values = []
            for entry in chunk:
                values.append(
                    {
                        "info_hash": entry["hash"],
                        "filename": entry["filename"],
                        "size": entry["size"],
                        "parsed_title": entry["parsed_title"],
                        "parsed_year": entry["parsed_year"],
                    }
                )

            query = """
                INSERT INTO dmm_entries (info_hash, filename, size, parsed_title, parsed_year)
                VALUES (:info_hash, :filename, :size, :parsed_title, :parsed_year)
                ON CONFLICT DO NOTHING
            """

            await database.execute_many(query, values)


HASHLIST_REGEX = re.compile(r'hashlist#(.*?)"')


def process_file_sync(file_path):
    try:
        with open(file_path, "rb") as f:
            raw_content = f.read(_MAX_ARCHIVE_MEMBER_BYTES + 1)
    except OSError:
        return None
    if len(raw_content) > _MAX_ARCHIVE_MEMBER_BYTES:
        return None
    try:
        content = raw_content.decode("utf-8")
    except UnicodeDecodeError:
        return None

    match = HASHLIST_REGEX.search(content)
    if not match:
        return []

    encoded_data = match.group(1)
    json_str = decompressFromEncodedURIComponent(
        encoded_data,
        maximum=_MAX_ARCHIVE_MEMBER_BYTES,
    )

    if not json_str:
        return None

    try:
        data = orjson.loads(json_str)
    except orjson.JSONDecodeError:
        return None

    results = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict) and "torrents" in data:
        items = data["torrents"]
    else:
        return None

    if not isinstance(items, list):
        return None
    if len(items) > _MAX_HASHLIST_ITEMS:
        return None

    for item in items:
        if not isinstance(item, dict):
            continue
        filename = item.get("filename")
        info_hash = item.get("hash")
        size = item.get("bytes", 0)

        if (
            not isinstance(filename, str)
            or not filename
            or not isinstance(info_hash, str)
            or _INFO_HASH.fullmatch(info_hash) is None
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not 1 <= size <= _MAX_SIGNED_64
        ):
            continue

        try:
            filename_bytes = filename.encode("utf-8")
        except UnicodeEncodeError:
            continue
        if len(filename_bytes) > _MAX_FILENAME_BYTES or any(
            ord(character) < 32 or ord(character) == 127 for character in filename
        ):
            continue

        parsed = RTN.parse(filename)

        results.append(
            {
                "hash": info_hash.lower(),
                "filename": filename,
                "size": size,
                "parsed_title": parsed.parsed_title,
                "parsed_year": parsed.year,
            }
        )

    return results


def extract_zip_sync(zip_path, extract_dir):
    with zipfile.ZipFile(zip_path, "r") as zip_ref:
        target = Path(extract_dir).resolve()
        members = zip_ref.infolist()
        if len(members) > _MAX_ARCHIVE_MEMBERS:
            raise ValueError("DMM archive has too many members")
        planned = []
        seen = set()
        total_size = 0
        for member in members:
            member_path = Path(member.filename)
            mode = member.external_attr >> 16
            file_type = stat.S_IFMT(mode)
            try:
                path_bytes = member.filename.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError("Unsafe DMM archive member") from exc
            destination = (target / member_path).resolve()
            if (
                not member.filename
                or len(path_bytes) > _MAX_ARCHIVE_PATH_BYTES
                or "\\" in member.filename
                or "\x00" in member.filename
                or any(
                    ord(character) < 32 or ord(character) == 127
                    for character in member.filename
                )
                or member_path == Path(".")
                or member_path.is_absolute()
                or ".." in member_path.parts
                or stat.S_ISLNK(mode)
                or (file_type and file_type not in {stat.S_IFREG, stat.S_IFDIR})
                or member.flag_bits & 0x1
                or not destination.is_relative_to(target)
                or destination in seen
                or not 0 <= member.file_size <= _MAX_ARCHIVE_MEMBER_BYTES
                or not 0 <= member.compress_size <= _MAX_DOWNLOAD_BYTES
            ):
                raise ValueError("Unsafe DMM archive member")
            seen.add(destination)
            total_size += member.file_size
            if total_size > _MAX_ARCHIVE_TOTAL_BYTES:
                raise ValueError("DMM archive is too large")
            planned.append((member, destination))

        try:
            target.mkdir(parents=True, mode=0o700)
            for member, destination in planned:
                if member.is_dir():
                    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                written = 0
                with (
                    zip_ref.open(member, "r") as source,
                    destination.open("xb") as output,
                ):
                    os.chmod(destination, 0o600)
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        if len(chunk) > member.file_size - written:
                            raise ValueError("DMM archive member size mismatch")
                        output.write(chunk)
                        written += len(chunk)
                if written != member.file_size:
                    raise ValueError("DMM archive member size mismatch")
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise


dmm_ingester = DMMIngester()
