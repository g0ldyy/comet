import asyncio
import errno
import os
import sqlite3
import stat
import time
from contextlib import asynccontextmanager

import asyncpg

try:
    import fcntl
except ImportError:
    fcntl = None

import orjson

import comet.core.models as _models_mod
from comet.core.models import IS_POSTGRES, IS_SQLITE, JSON_FUNC, database, settings
from comet.core.schema_migrations import (
    NULL_SCOPE_SENTINEL,
    has_pending_schema_migrations,
    run_schema_migrations,
)
from comet.observability import log

__all__ = [
    "DOWNLOAD_LINK_CACHE_TTL",
    "IS_POSTGRES",
    "IS_SQLITE",
    "JSON_FUNC",
    "NULL_SCOPE_SENTINEL",
    "backend_lock",
    "build_distinct_from_predicate",
    "build_json_list_membership_predicate",
    "build_scope_lookup_params",
    "build_scope_params",
    "build_upsert_assignments",
    "database",
    "encode_json_param",
    "fetch_flag",
    "is_retryable_database_error",
    "normalize_scope_value",
    "run_retention_cleanup",
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
_USENET_LIFECYCLE_CLEANUP_INTERVAL_SECONDS = 5 * 60
_MAX_SQLITE_LOCKFILE_PID_BYTES = 32
_RETRYABLE_DB_SQLSTATES = frozenset({"40001", "40P01", "55P03"})
_RETRYABLE_SQLITE_ERROR_CODES = frozenset(
    {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    }
)


def normalize_scope_value(value: int | None) -> int:
    return NULL_SCOPE_SENTINEL if value is None else value


def is_retryable_database_error(exc: Exception) -> bool:
    seen = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        sqlstate = getattr(current, "sqlstate", None) or getattr(
            current, "pgcode", None
        )
        if (
            sqlstate in _RETRYABLE_DB_SQLSTATES
            or getattr(current, "sqlite_errorcode", None)
            in _RETRYABLE_SQLITE_ERROR_CODES
        ):
            return True
        current = getattr(current, "__cause__", None) or getattr(
            current, "__context__", None
        )
    return False


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


def build_distinct_from_predicate(
    table_name: str,
    compared_alias: str,
    columns: tuple[str, ...],
) -> str:
    return " OR ".join(
        f"{table_name}.{column} IS DISTINCT FROM {compared_alias}.{column}"
        for column in columns
    )


def build_upsert_assignments(
    columns: tuple[str, ...] | list[str],
    *,
    source_alias: str = "EXCLUDED",
    indent: str = "        ",
) -> str:
    return ",\n".join(
        f"{indent}{column} = {source_alias}.{column}" for column in columns
    )


def encode_json_param(value, *, default=None) -> str:
    return orjson.dumps(value, default=default).decode("utf-8")


def build_json_list_membership_predicate(column_name: str, param_name: str) -> str:
    return (
        f"{column_name} IN (SELECT CAST(value AS TEXT) FROM {JSON_FUNC}(:{param_name}))"
    )


async def fetch_flag(
    query: str,
    values: dict[str, object] | None = None,
    *,
    force_primary: bool = False,
) -> bool:
    return (
        await database.fetch_one(query, values, force_primary=force_primary)
    ) is not None


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
            raw_pid = lock_file.readline(_MAX_SQLITE_LOCKFILE_PID_BYTES + 1).strip()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError):
        return None

    if not raw_pid or len(raw_pid) > _MAX_SQLITE_LOCKFILE_PID_BYTES:
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
    return True


def _create_sqlite_lockfile(lock_path: str) -> int | None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC

    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    try:
        lock_fd = os.open(lock_path, flags, 0o600)
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
            log.info("database.lock.waiting", wait_message)
            wait_logged = True

        await asyncio.sleep(_BACKEND_LOCK_RETRY_INTERVAL_SECONDS)


async def _close_sqlite_lock_fd(lock_fd: int | None) -> None:
    if lock_fd is None:
        return
    await asyncio.to_thread(os.close, lock_fd)


async def _unlink_if_exists(path: str) -> None:
    def _unlink():
        try:
            os.unlink(path)
        except FileNotFoundError:
            return

    await asyncio.to_thread(_unlink)


async def _acquire_backend_lock(
    try_acquire,
    *,
    wait_message: str,
    wait: bool,
) -> bool:
    if wait:
        await _acquire_backend_lock_with_delayed_log(try_acquire, wait_message)
        return True

    return await try_acquire()


@asynccontextmanager
async def _held_backend_lock(
    try_acquire,
    release,
    *,
    wait_message: str,
    wait: bool,
):
    acquired = await _acquire_backend_lock(
        try_acquire,
        wait_message=wait_message,
        wait=wait,
    )
    if not acquired:
        yield False
        return

    try:
        yield True
    finally:
        await release()


@asynccontextmanager
async def backend_lock(
    *,
    postgres_lock_id: int,
    sqlite_lock_path: str,
    wait_message: str,
    wait: bool = True,
):
    if IS_POSTGRES:
        async with database.connection() as connection:

            async def _try_acquire_postgres_lock() -> bool:
                row = await connection.fetch_one(
                    "SELECT pg_try_advisory_lock(:lock_id) AS acquired",
                    {"lock_id": postgres_lock_id},
                )
                return bool(row["acquired"])

            async def _release_postgres_lock():
                await connection.execute(
                    "SELECT pg_advisory_unlock(:lock_id)",
                    {"lock_id": postgres_lock_id},
                )

            async with _held_backend_lock(
                _try_acquire_postgres_lock,
                _release_postgres_lock,
                wait_message=wait_message,
                wait=wait,
            ) as acquired:
                yield acquired
        return

    if IS_SQLITE:
        if fcntl is not None:
            lock_file = None
            lock_acquired = False
            try:
                lock_file = await asyncio.to_thread(open, sqlite_lock_path, "a+")

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

                async def _release_sqlite_lock():
                    if lock_file is not None and lock_acquired:
                        await asyncio.to_thread(
                            fcntl.flock, lock_file.fileno(), fcntl.LOCK_UN
                        )

                async with _held_backend_lock(
                    _try_acquire_sqlite_lock,
                    _release_sqlite_lock,
                    wait_message=wait_message,
                    wait=wait,
                ) as acquired:
                    yield acquired
            finally:
                if lock_file is not None:
                    await asyncio.to_thread(lock_file.close)
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

            async def _release_sqlite_lockfile():
                nonlocal lock_fd
                if lock_fd is None:
                    return

                await _close_sqlite_lock_fd(lock_fd)
                lock_fd = None
                await _unlink_if_exists(fallback_lock_path)

            async with _held_backend_lock(
                _try_acquire_sqlite_lockfile,
                _release_sqlite_lockfile,
                wait_message=wait_message,
                wait=wait,
            ) as acquired:
                yield acquired
        finally:
            if lock_fd is not None:
                await _close_sqlite_lock_fd(lock_fd)
                lock_fd = None
                await _unlink_if_exists(fallback_lock_path)
        return

    raise RuntimeError("database backend is unsupported")


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


@asynccontextmanager
async def _startup_cleanup_lock():
    if IS_POSTGRES:
        async with database.transaction():
            acquired = await database.fetch_val(
                "SELECT pg_try_advisory_xact_lock(:lock_id)",
                {"lock_id": STARTUP_CLEANUP_LOCK_ID},
                force_primary=True,
            )
            yield bool(acquired)
        return

    async with backend_lock(
        postgres_lock_id=STARTUP_CLEANUP_LOCK_ID,
        sqlite_lock_path=f"{settings.DATABASE_PATH}.startup_cleanup.lock",
        wait_message="Waiting for SQLite startup cleanup lock",
        wait=False,
    ) as acquired:
        if not acquired:
            yield False
            return

        async with database.transaction():
            yield True


async def _apply_sqlite_pragmas(
    *,
    foreign_keys: bool,
    journal_mode: str = _SQLITE_DEFAULT_JOURNAL_MODE,
):
    await _models_mod.apply_sqlite_connection_pragmas(
        database.execute,
        foreign_keys_enabled=foreign_keys,
    )
    await database.execute(f"PRAGMA journal_mode={journal_mode}")
    await database.execute("PRAGMA synchronous=NORMAL")
    await database.execute("PRAGMA temp_store=MEMORY")
    await database.execute("PRAGMA mmap_size=30000000000")
    await database.execute("PRAGMA page_size=4096")
    await database.execute("PRAGMA cache_size=-2000")
    await database.execute(
        f"PRAGMA journal_size_limit={_SQLITE_JOURNAL_SIZE_LIMIT_BYTES}"
    )
    await database.execute("PRAGMA count_changes=OFF")
    await database.execute("PRAGMA secure_delete=FAST")
    await database.execute("PRAGMA auto_vacuum=OFF")


def _fsync_directory(path: str) -> None:
    directory = os.path.dirname(path) or "."
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    directory_fd = os.open(directory, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _prepare_sqlite_database_file(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)

    flags = os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    created = False
    try:
        file_fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        file_fd = os.open(path, flags)

    try:
        file_stat = os.fstat(file_fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError("DATABASE_PATH must reference a regular file")
        os.fchmod(file_fd, 0o600)
        if created:
            os.fsync(file_fd)
    finally:
        os.close(file_fd)

    if created:
        _fsync_directory(path)


async def setup_database():
    connected = False
    started_at = time.monotonic()
    backend = "sqlite" if IS_SQLITE else "postgresql"
    try:
        if IS_SQLITE:
            _prepare_sqlite_database_file(settings.DATABASE_PATH)
            _models_mod.set_comet_foreign_keys_enabled(False)

        await database.connect()
        connected = True

        async with _schema_migration_lock():
            migration_started = False

            def _report_migration_started():
                nonlocal migration_started
                migration_started = True
                log.info(
                    "database.migration.started",
                    "Database migration started",
                    database_backend=backend,
                )

            if IS_SQLITE:
                migrations_pending = await has_pending_schema_migrations(
                    database,
                    is_sqlite=True,
                    is_postgres=False,
                )
                if migrations_pending:
                    _report_migration_started()
                    await _apply_sqlite_pragmas(
                        foreign_keys=False,
                        journal_mode=_SQLITE_MIGRATION_JOURNAL_MODE,
                    )
                    migration_count = await run_schema_migrations(
                        database,
                        is_sqlite=True,
                        is_postgres=False,
                    )
                    await _apply_sqlite_pragmas(
                        foreign_keys=True,
                        journal_mode=_SQLITE_DEFAULT_JOURNAL_MODE,
                    )
                _models_mod.set_comet_foreign_keys_enabled(True)
            else:
                migration_count = await run_schema_migrations(
                    database,
                    is_sqlite=False,
                    is_postgres=IS_POSTGRES,
                    on_pending=_report_migration_started,
                )
            if migration_started:
                log.info(
                    "database.migration.completed",
                    "Database migration completed",
                    migration_count=migration_count,
                    duration_ms=(time.monotonic() - started_at) * 1000,
                )

        await database.execute(
            """
            DELETE FROM active_connections
            WHERE instance_id = ''
               OR updated_at < :cutoff
            """,
            {
                "cutoff": time.time()
                - max(settings.PROXY_DEBRID_STREAM_INACTIVITY_THRESHOLD, 15)
            },
        )
        await database.execute("DELETE FROM metrics_cache")

        await _run_startup_cleanup()
        log.verbose(
            "database.ready",
            "Database is ready",
            database_backend=backend,
            duration_ms=(time.monotonic() - started_at) * 1000,
        )
    except Exception as exc:
        log.error(
            "database.setup.failed",
            "Database setup failed",
            database_backend=backend,
            error_code="database_setup_failed",
            exc=exc,
        )
        if connected:
            try:
                await database.disconnect()
            except Exception as disconnect_exc:
                log.error(
                    "database.disconnect.failed",
                    "Database disconnect after setup failure failed",
                    error_code="database_setup_failed",
                    exc=disconnect_exc,
                )
        raise


async def _run_startup_cleanup():
    interval = settings.DATABASE_STARTUP_CLEANUP_INTERVAL
    if interval < 0:
        return

    current_time = time.time()
    cleanup_performed = False
    should_run = (
        True
        if interval == 0
        else await _should_run_startup_cleanup(current_time, interval)
    )
    if not should_run:
        return

    try:
        async with _startup_cleanup_lock() as acquired:
            if not acquired:
                return
            await _perform_startup_cleanup(current_time)
            await _record_startup_cleanup(current_time)
            cleanup_performed = True
    except (OSError, sqlite3.Error, asyncpg.PostgresError) as exc:
        log.error(
            "database.cleanup.failed",
            "Database startup cleanup failed",
            error_code="cleanup_failure",
            exc=exc,
        )

    if IS_SQLITE and cleanup_performed:
        await database.execute("PRAGMA wal_checkpoint(TRUNCATE)")


async def run_retention_cleanup() -> bool:
    current_time = time.time()
    async with _startup_cleanup_lock() as acquired:
        if not acquired:
            return False
        await _perform_startup_cleanup(current_time)
        await _record_startup_cleanup(current_time)
    if IS_SQLITE:
        await database.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return True


async def _sqlite_batched_delete(
    table_name: str,
    where_sql: str,
    params: dict[str, float | int | str],
):
    last_rowid = 0

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

        if batch_row is None:
            raise RuntimeError("SQLite cleanup batch query returned no row")
        if not batch_row["row_count"]:
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
    await _perform_usenet_lifecycle_cleanup(current_time)

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
            metadata_updated_at = NULL
        WHERE metadata_updated_at IS NOT NULL
          AND metadata_updated_at < :metadata_cutoff
        """,
        {"metadata_cutoff": metadata_cutoff},
    )
    await database.execute(
        """
        UPDATE media_metadata_cache
        SET aliases_updated_at = NULL
        WHERE aliases_updated_at IS NOT NULL
          AND aliases_updated_at < :metadata_cutoff
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
        "metadata_updated_at IS NULL "
        "AND aliases_updated_at IS NULL "
        "AND release_updated_at IS NULL",
        {},
    )
    await _delete_where(
        "series_episode_index",
        "updated_at < :min_timestamp",
        {"min_timestamp": metadata_cutoff},
    )
    await _delete_where(
        "series_episode_index_refresh",
        "refreshed_at < :min_timestamp",
        {"min_timestamp": metadata_cutoff},
    )
    await _delete_where(
        "imdb_title_lookup",
        "updated_at < :min_timestamp",
        {"min_timestamp": metadata_cutoff},
    )

    if settings.TORRENT_CACHE_TTL >= 0:
        from comet.discovery.torrent_repository import TorrentReleaseRepository

        await TorrentReleaseRepository(database).delete_stale_public(
            before_ms=max(
                0,
                int((current_time - settings.TORRENT_CACHE_TTL) * 1_000),
            ),
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
        "expires_at < :current_time",
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


async def _perform_usenet_lifecycle_cleanup(current_time: float):
    from comet.core.capability_states import CapabilityStateRepository
    from comet.core.provider_governor import ProviderGovernor
    from comet.playback.preparations import PlaybackPreparationRepository
    from comet.playback.provider_preparations import ProviderPreparationRepository
    from comet.playback.repository import RenderedReleaseRepository
    from comet.playback.resolution_cache import ProviderResolutionCacheRepository
    from comet.usenet.artifact_gc import SharedArtifactGarbageCollector
    from comet.usenet.provider_exports import NzbProviderExportRepository

    await CapabilityStateRepository(database).cleanup_expired(now=current_time)
    await ProviderGovernor(database).collect_expired(
        now=current_time,
        batch_size=10_000,
    )
    await PlaybackPreparationRepository(database).garbage_collect(
        now=current_time,
    )
    await ProviderPreparationRepository(database).garbage_collect(
        now=current_time,
    )
    await RenderedReleaseRepository(database).garbage_collect(
        now=current_time,
    )
    await NzbProviderExportRepository(database).garbage_collect(
        now=current_time,
    )
    await ProviderResolutionCacheRepository(database).cleanup_expired(
        now=current_time,
    )
    await SharedArtifactGarbageCollector(
        settings.USENET_ARTIFACT_DIR,
        database,
    ).collect(now=current_time)


async def _run_usenet_lifecycle_cleanup(current_time: float) -> bool:
    async with _startup_cleanup_lock() as acquired:
        if not acquired:
            return False
        await _perform_usenet_lifecycle_cleanup(current_time)
        return True


async def cleanup_expired_usenet_state():
    while True:
        await asyncio.sleep(_USENET_LIFECYCLE_CLEANUP_INTERVAL_SECONDS)
        try:
            await _run_usenet_lifecycle_cleanup(time.time())
        except (OSError, sqlite3.Error, asyncpg.PostgresError) as exc:
            log.error(
                "database.lifecycle.failed",
                "Usenet lifecycle cleanup failed",
                error_code="cleanup_failure",
                exc=exc,
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
        except (OSError, sqlite3.Error, asyncpg.PostgresError) as exc:
            log.error(
                "database.lock_cleanup.failed",
                "Expired lock cleanup failed",
                error_code="cleanup_failure",
                exc=exc,
            )

        await asyncio.sleep(60)


async def cleanup_expired_kodi_setup_codes():
    while True:
        try:
            current_time = time.time()
            await database.execute(
                """
                DELETE FROM kodi_setup_codes
                WHERE expires_at < :current_time
                """,
                {"current_time": current_time},
            )
        except (OSError, sqlite3.Error, asyncpg.PostgresError) as exc:
            log.error(
                "database.kodi_cleanup.failed",
                "Kodi setup cleanup failed",
                error_code="cleanup_failure",
                exc=exc,
            )

        await asyncio.sleep(30)


async def teardown_database():
    try:
        await database.disconnect()
    except Exception as exc:
        log.error(
            "database.teardown.failed",
            "Database teardown failed",
            error_code="cleanup_failure",
            exc=exc,
        )
        raise
