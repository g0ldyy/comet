import asyncio
import errno
import os
import time
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None

import comet.core.models as _models_mod
from comet.core.logger import logger
from comet.core.models import (IS_POSTGRES, IS_SQLITE, JSON_FUNC,
                               ON_CONFLICT_DO_NOTHING, OR_IGNORE, database,
                               settings)
from comet.core.schema_migrations import (NULL_SCOPE_SENTINEL,
                                          run_schema_migrations)

__all__ = [
    "DOWNLOAD_LINK_CACHE_TTL",
    "IS_POSTGRES",
    "IS_SQLITE",
    "JSON_FUNC",
    "NULL_SCOPE_SENTINEL",
    "ON_CONFLICT_DO_NOTHING",
    "OR_IGNORE",
    "backend_lock",
    "build_scope_lookup_params",
    "build_scope_params",
    "database",
    "normalize_scope_value",
    "settings",
]

STARTUP_CLEANUP_LOCK_ID = 0xC0FFEE
SCHEMA_MIGRATION_LOCK_ID = 0xC0DE7001
DOWNLOAD_LINK_CACHE_TTL = 3600
_BACKEND_LOCK_WAIT_LOG_DELAY_SECONDS = 0.5
_BACKEND_LOCK_RETRY_INTERVAL_SECONDS = 0.1
_SQLITE_INVALID_LOCKFILE_GRACE_SECONDS = 1.0
_SQLITE_DEFAULT_JOURNAL_MODE = "WAL"
_SQLITE_MIGRATION_JOURNAL_MODE = "DELETE"
_SQLITE_JOURNAL_SIZE_LIMIT_BYTES = 64 * 1024 * 1024
_SQLITE_CLEANUP_BATCH_SIZE = 50000
_SQLITE_CLEANUP_CHECKPOINT_INTERVAL = 8


def normalize_scope_value(value: int | None) -> int:
    return NULL_SCOPE_SENTINEL if value is None else value


def build_scope_params(
    season: int | None, episode: int | None
) -> dict[str, int | None]:
    return {
        "season": season,
        "episode": episode,
        "season_norm": normalize_scope_value(season),
        "episode_norm": normalize_scope_value(episode),
    }


def build_scope_lookup_params(
    season: int | None, episode: int | None
) -> dict[str, int]:
    return {
        "season_norm": normalize_scope_value(season),
        "episode_norm": normalize_scope_value(episode),
    }


def _debrid_account_snapshot_ttl() -> int:
    return max(
        settings.DEBRID_ACCOUNT_SCRAPE_CACHE_TTL,
        settings.DEBRID_ACCOUNT_SCRAPE_REFRESH_INTERVAL,
    )


def _media_demand_ttl() -> int:
    torrent_ttl = settings.TORRENT_CACHE_TTL
    demand_lookback = max(0, settings.BACKGROUND_SCRAPER_DEMAND_LOOKBACK)
    if torrent_ttl < 0:
        return 0
    return max(torrent_ttl, demand_lookback)


def _is_process_running(pid: int) -> bool:
    if pid <= 0:
        return False

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        winerror = getattr(exc, "winerror", None)
        if winerror == 87:
            return False
        if winerror == 5:
            return True
        if exc.errno == errno.ESRCH:
            return False
        if exc.errno == errno.EPERM:
            return True
        return True

    return True


def _read_sqlite_lockfile_pid(lock_path: str) -> int | None:
    try:
        with open(lock_path, "r", encoding="ascii") as lock_file:
            raw_pid = lock_file.readline().strip()
    except FileNotFoundError:
        return None
    except OSError:
        return None

    if not raw_pid:
        return None

    try:
        pid = int(raw_pid)
    except ValueError:
        return None

    return pid if pid > 0 else None


def _try_remove_stale_sqlite_lockfile(lock_path: str) -> bool:
    pid = _read_sqlite_lockfile_pid(lock_path)
    if pid is None:
        try:
            age_seconds = max(0.0, time.time() - os.path.getmtime(lock_path))
        except FileNotFoundError:
            return False
        except OSError:
            return False

        if age_seconds < _SQLITE_INVALID_LOCKFILE_GRACE_SECONDS:
            return False
    elif _is_process_running(pid):
        return False

    try:
        os.unlink(lock_path)
    except FileNotFoundError:
        return True
    except OSError:
        return False

    logger.log("DATABASE", f"Removed stale SQLite lock file: {lock_path}")
    return True


def _create_sqlite_lockfile(lock_path: str) -> int | None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC

    try:
        lock_fd = os.open(lock_path, flags, 0o644)
    except FileExistsError:
        return None
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            return None
        raise

    try:
        payload = f"{os.getpid()}\n".encode("ascii")
        written = 0
        while written < len(payload):
            chunk_written = os.write(lock_fd, payload[written:])
            if chunk_written <= 0:
                raise OSError(errno.EIO, "Failed to write SQLite lock file PID")
            written += chunk_written
        os.fsync(lock_fd)
    except Exception:
        try:
            os.close(lock_fd)
        finally:
            try:
                os.unlink(lock_path)
            except FileNotFoundError:
                pass
        raise

    return lock_fd


async def _acquire_backend_lock_with_delayed_log(
    try_acquire, wait_message: str
) -> None:
    wait_started = None
    wait_logged = False

    while True:
        if await try_acquire():
            return

        now = time.monotonic()
        if wait_started is None:
            wait_started = now
        elif (
            not wait_logged
            and (now - wait_started) >= _BACKEND_LOCK_WAIT_LOG_DELAY_SECONDS
        ):
            logger.log("DATABASE", wait_message)
            wait_logged = True

        await asyncio.sleep(_BACKEND_LOCK_RETRY_INTERVAL_SECONDS)


@asynccontextmanager
async def backend_lock(
    *,
    postgres_lock_id: int,
    sqlite_lock_path: str,
    wait_message: str,
):
    if IS_POSTGRES:
        async with database.connection() as connection:

            async def _try_acquire_postgres_lock() -> bool:
                row = await connection.fetch_one(
                    "SELECT pg_try_advisory_lock(:lock_id) AS acquired",
                    {"lock_id": postgres_lock_id},
                )
                return bool(row["acquired"])

            await _acquire_backend_lock_with_delayed_log(
                _try_acquire_postgres_lock, wait_message
            )
            try:
                yield
            finally:
                await connection.execute(
                    "SELECT pg_advisory_unlock(:lock_id)",
                    {"lock_id": postgres_lock_id},
                )
        return

    if IS_SQLITE:
        if fcntl is not None:
            lock_file = None
            lock_acquired = False
            try:
                lock_file = open(sqlite_lock_path, "a+")

                async def _try_acquire_sqlite_lock() -> bool:
                    nonlocal lock_acquired
                    assert lock_file is not None

                    try:
                        await asyncio.to_thread(
                            fcntl.flock,
                            lock_file.fileno(),
                            fcntl.LOCK_EX | fcntl.LOCK_NB,
                        )
                        lock_acquired = True
                        return True
                    except OSError as exc:
                        if exc.errno not in (errno.EACCES, errno.EAGAIN):
                            raise
                        return False

                await _acquire_backend_lock_with_delayed_log(
                    _try_acquire_sqlite_lock, wait_message
                )
                yield
            finally:
                if lock_file is not None:
                    try:
                        if lock_acquired:
                            await asyncio.to_thread(
                                fcntl.flock, lock_file.fileno(), fcntl.LOCK_UN
                            )
                    finally:
                        lock_file.close()
            return

        fallback_lock_path = f"{sqlite_lock_path}.lock"
        lock_fd = None
        try:

            async def _try_acquire_sqlite_lockfile() -> bool:
                nonlocal lock_fd

                acquired_fd = await asyncio.to_thread(
                    _create_sqlite_lockfile, fallback_lock_path
                )
                if acquired_fd is None:
                    removed_stale = await asyncio.to_thread(
                        _try_remove_stale_sqlite_lockfile, fallback_lock_path
                    )
                    if not removed_stale:
                        return False

                    acquired_fd = await asyncio.to_thread(
                        _create_sqlite_lockfile, fallback_lock_path
                    )
                    if acquired_fd is None:
                        return False

                lock_fd = acquired_fd
                return True

            await _acquire_backend_lock_with_delayed_log(
                _try_acquire_sqlite_lockfile, wait_message
            )
            yield
        finally:
            if lock_fd is not None:
                await asyncio.to_thread(os.close, lock_fd)

                def _remove_lock_file():
                    try:
                        os.unlink(fallback_lock_path)
                    except FileNotFoundError:
                        return

                await asyncio.to_thread(_remove_lock_file)
        return

    yield


@asynccontextmanager
async def _schema_migration_lock():
    async with backend_lock(
        postgres_lock_id=SCHEMA_MIGRATION_LOCK_ID,
        sqlite_lock_path=f"{settings.DATABASE_PATH}.migrate.lock",
        wait_message=(
            "Waiting for schema migration lock"
            if IS_POSTGRES
            else "Waiting for SQLite schema migration lock"
        ),
    ):
        yield


async def _apply_sqlite_pragmas(
    *,
    foreign_keys: bool,
    journal_mode: str = _SQLITE_DEFAULT_JOURNAL_MODE,
):
    await database.execute("PRAGMA busy_timeout=30000")
    await database.execute(f"PRAGMA journal_mode={journal_mode}")
    await database.execute("PRAGMA synchronous=OFF")
    await database.execute("PRAGMA temp_store=MEMORY")
    await database.execute("PRAGMA mmap_size=30000000000")
    await database.execute("PRAGMA page_size=4096")
    await database.execute("PRAGMA cache_size=-2000")
    await database.execute(
        f"PRAGMA journal_size_limit={_SQLITE_JOURNAL_SIZE_LIMIT_BYTES}"
    )
    await database.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}")
    await database.execute("PRAGMA count_changes=OFF")
    await database.execute("PRAGMA secure_delete=OFF")
    await database.execute("PRAGMA auto_vacuum=OFF")


async def setup_database():
    try:
        if IS_SQLITE:
            db_dir = os.path.dirname(settings.DATABASE_PATH)
            if db_dir:
                os.makedirs(db_dir, exist_ok=True)
            if not os.path.exists(settings.DATABASE_PATH):
                Path(settings.DATABASE_PATH).touch(exist_ok=True)
            _models_mod.set_comet_foreign_keys_enabled(False)

        await database.connect()

        if IS_SQLITE:
            await _apply_sqlite_pragmas(
                foreign_keys=False,
                journal_mode=_SQLITE_MIGRATION_JOURNAL_MODE,
            )

        async with _schema_migration_lock():
            await run_schema_migrations(
                database,
                is_sqlite=IS_SQLITE,
                is_postgres=IS_POSTGRES,
            )

        if IS_SQLITE:
            _models_mod.set_comet_foreign_keys_enabled(True)
            await _apply_sqlite_pragmas(
                foreign_keys=True,
                journal_mode=_SQLITE_DEFAULT_JOURNAL_MODE,
            )

        await database.execute("DELETE FROM active_connections")
        await database.execute("DELETE FROM metrics_cache")

        await _run_startup_cleanup()
        if IS_SQLITE:
            await database.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception as e:
        logger.error(f"Error setting up the database: {e}")
        logger.exception(traceback.format_exc())
        raise


async def _run_startup_cleanup():
    interval = settings.DATABASE_STARTUP_CLEANUP_INTERVAL
    if interval is None or interval < 0:
        return

    current_time = time.time()
    should_run = (
        True
        if interval == 0
        else await _should_run_startup_cleanup(current_time, interval)
    )
    if not should_run:
        logger.log("DATABASE", "Startup cleanup skipped (recent run)")
        return

    try:
        if IS_POSTGRES:
            async with database.transaction():
                acquired = await database.fetch_val(
                    "SELECT pg_try_advisory_xact_lock(:lock_id)",
                    {"lock_id": STARTUP_CLEANUP_LOCK_ID},
                    force_primary=True,
                )
                if not acquired:
                    logger.log(
                        "DATABASE",
                        "Startup cleanup already running elsewhere; skipping",
                    )
                    return

                logger.log("DATABASE", "Running startup cleanup sweep")
                await _perform_startup_cleanup(current_time)
                await _record_startup_cleanup(current_time)
            return

        logger.log("DATABASE", "Running startup cleanup sweep")
        await _perform_startup_cleanup(current_time)
        await _record_startup_cleanup(current_time)
    except Exception as e:
        logger.error(f"Error executing startup cleanup: {e}")


async def _sqlite_batched_delete(
    table_name: str,
    where_sql: str,
    params: dict[str, float | int | str],
):
    last_rowid = 0
    batches = 0

    while True:
        batch_params = {
            **params,
            "last_rowid": last_rowid,
            "batch_size": _SQLITE_CLEANUP_BATCH_SIZE,
        }
        batch_row = await database.fetch_one(
            f"""
            SELECT COUNT(*) AS row_count, MAX(rowid) AS max_rowid
            FROM (
                SELECT rowid
                FROM {table_name}
                WHERE rowid > :last_rowid
                  AND ({where_sql})
                ORDER BY rowid
                LIMIT :batch_size
            ) AS cleanup_batch
            """,
            batch_params,
            force_primary=True,
        )

        if not batch_row or not batch_row["row_count"]:
            break

        await database.execute(
            f"""
            DELETE FROM {table_name}
            WHERE rowid IN (
                SELECT rowid
                FROM {table_name}
                WHERE rowid > :last_rowid
                  AND ({where_sql})
                ORDER BY rowid
                LIMIT :batch_size
            )
            """,
            batch_params,
        )

        last_rowid = int(batch_row["max_rowid"])
        batches += 1
        if batches % _SQLITE_CLEANUP_CHECKPOINT_INTERVAL == 0:
            await database.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    if batches:
        await database.execute("PRAGMA wal_checkpoint(TRUNCATE)")


async def _delete_where(
    table_name: str,
    where_sql: str,
    params: dict[str, float | int | str],
):
    if IS_SQLITE:
        await _sqlite_batched_delete(table_name, where_sql, params)
        return

    await database.execute(
        f"""
        DELETE FROM {table_name}
        WHERE {where_sql}
        """,
        params,
    )


async def _record_startup_cleanup(current_time: float):
    await database.execute(
        """
        INSERT INTO db_maintenance (id, last_startup_cleanup_at)
        VALUES (1, :timestamp)
        ON CONFLICT (id) DO UPDATE
        SET last_startup_cleanup_at = :timestamp
        """,
        {"timestamp": current_time},
        force_primary=True,
    )


async def _perform_startup_cleanup(current_time: float):
    demand_ttl = _media_demand_ttl()
    if demand_ttl > 0:
        await _delete_where(
            "media_demand",
            "last_seen_at < :min_timestamp",
            {"min_timestamp": current_time - demand_ttl},
        )

    metadata_cutoff = current_time - settings.METADATA_CACHE_TTL
    await database.execute(
        """
        UPDATE media_metadata_cache
        SET title = NULL,
            year = NULL,
            year_end = NULL,
            aliases_json = NULL,
            metadata_updated_at = NULL
        WHERE metadata_updated_at IS NOT NULL
          AND metadata_updated_at < :metadata_cutoff
        """,
        {"metadata_cutoff": metadata_cutoff},
    )
    await database.execute(
        """
        UPDATE media_metadata_cache
        SET release_date = NULL,
            release_updated_at = NULL
        WHERE release_updated_at IS NOT NULL
          AND release_updated_at < :release_cutoff
        """,
        {"release_cutoff": metadata_cutoff},
    )
    await _delete_where(
        "media_metadata_cache",
        "metadata_updated_at IS NULL AND release_updated_at IS NULL",
        {},
    )

    if settings.TORRENT_CACHE_TTL >= 0:
        await _delete_where(
            "torrents",
            "updated_at < :min_timestamp",
            {"min_timestamp": current_time - settings.TORRENT_CACHE_TTL},
        )

    await _delete_where(
        "debrid_availability",
        "updated_at < :min_timestamp",
        {"min_timestamp": current_time - settings.DEBRID_CACHE_TTL},
    )

    await _delete_where(
        "debrid_account_magnets",
        "synced_at < :min_timestamp",
        {"min_timestamp": current_time - _debrid_account_snapshot_ttl()},
    )

    await _delete_where(
        "debrid_account_sync_state",
        "last_sync_at < :min_timestamp",
        {
            "min_timestamp": current_time - (_debrid_account_snapshot_ttl() * 2),
        },
    )

    await _delete_where(
        "download_links_cache",
        "updated_at < :min_timestamp",
        {"min_timestamp": current_time - DOWNLOAD_LINK_CACHE_TTL},
    )

    await _delete_where(
        "kodi_setup_codes",
        "expires_at < :current_time OR consumed_at IS NOT NULL",
        {"current_time": current_time},
    )

    run_retention_days = settings.BACKGROUND_SCRAPER_RUN_RETENTION_DAYS
    if run_retention_days > 0:
        await _delete_where(
            "background_scraper_runs",
            "started_at < :min_timestamp",
            {
                "min_timestamp": current_time - (run_retention_days * 86400),
            },
        )


async def _should_run_startup_cleanup(current_time: float, interval: int):
    row = await database.fetch_one(
        "SELECT last_startup_cleanup_at FROM db_maintenance WHERE id = 1",
        force_primary=True,
    )
    if not row or row["last_startup_cleanup_at"] is None:
        return True

    last_run = float(row["last_startup_cleanup_at"])
    return (current_time - last_run) >= interval


async def cleanup_expired_locks():
    while True:
        try:
            current_time = time.time()
            await database.execute(
                "DELETE FROM scrape_locks WHERE expires_at < :current_time",
                {"current_time": current_time},
            )
        except Exception as e:
            logger.log("LOCK", f"Error during periodic lock cleanup: {e}")

        await asyncio.sleep(60)


async def cleanup_expired_kodi_setup_codes():
    while True:
        try:
            current_time = time.time()
            await database.execute(
                """
                DELETE FROM kodi_setup_codes
                WHERE expires_at < :current_time
                   OR consumed_at IS NOT NULL
                """,
                {"current_time": current_time},
            )
        except Exception as e:
            logger.log("KODI", f"Error during Kodi setup cleanup: {e}")

        await asyncio.sleep(30)


async def teardown_database():
    try:
        await database.disconnect()
    except Exception as e:
        logger.error(f"Error tearing down the database: {e}")
        logger.exception(traceback.format_exc())
