import asyncio
import errno
import os
import time
import traceback
from contextlib import asynccontextmanager

try:
    import fcntl
except ImportError:
    fcntl = None

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
    if torrent_ttl is None or torrent_ttl < 0:
        return demand_lookback
    return max(torrent_ttl, demand_lookback)


@asynccontextmanager
async def backend_lock(
    *,
    postgres_lock_id: int,
    sqlite_lock_path: str,
    wait_message: str,
):
    if IS_POSTGRES:
        async with database.connection() as connection:
            row = await connection.fetch_one(
                "SELECT pg_try_advisory_lock(:lock_id) AS acquired",
                {"lock_id": postgres_lock_id},
            )
            acquired = bool(row["acquired"])
            if not acquired:
                logger.log("DATABASE", wait_message)
                await connection.execute(
                    "SELECT pg_advisory_lock(:lock_id)",
                    {"lock_id": postgres_lock_id},
                )
            try:
                yield
            finally:
                await connection.execute(
                    "SELECT pg_advisory_unlock(:lock_id)",
                    {"lock_id": postgres_lock_id},
                )
        return

    if IS_SQLITE and fcntl is not None:
        lock_file = open(sqlite_lock_path, "a+")
        try:
            try:
                await asyncio.to_thread(
                    fcntl.flock,
                    lock_file.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                logger.log("DATABASE", wait_message)
                await asyncio.to_thread(fcntl.flock, lock_file.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                await asyncio.to_thread(fcntl.flock, lock_file.fileno(), fcntl.LOCK_UN)
            finally:
                lock_file.close()
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


async def _apply_sqlite_pragmas(*, foreign_keys: bool):
    await database.execute("PRAGMA busy_timeout=30000")
    await database.execute("PRAGMA journal_mode=WAL")
    await database.execute("PRAGMA synchronous=OFF")
    await database.execute("PRAGMA temp_store=MEMORY")
    await database.execute("PRAGMA mmap_size=30000000000")
    await database.execute("PRAGMA page_size=4096")
    await database.execute("PRAGMA cache_size=-2000")
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
                open(settings.DATABASE_PATH, "a").close()

        await database.connect()

        if IS_SQLITE:
            await _apply_sqlite_pragmas(foreign_keys=False)

        async with _schema_migration_lock():
            await run_schema_migrations(
                database,
                is_sqlite=IS_SQLITE,
                is_postgres=IS_POSTGRES,
            )
            await _migrate_indexes()

        if IS_SQLITE:
            await _apply_sqlite_pragmas(foreign_keys=True)

        await database.execute("DELETE FROM active_connections")
        await database.execute("DELETE FROM metrics_cache")

        await _run_startup_cleanup()
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
        async with database.transaction():
            if IS_POSTGRES:
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

            demand_ttl = _media_demand_ttl()
            if demand_ttl > 0:
                await database.execute(
                    """
                    DELETE FROM media_demand
                    WHERE last_seen_at < :min_timestamp
                    """,
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
            await database.execute(
                """
                DELETE FROM media_metadata_cache
                WHERE metadata_updated_at IS NULL
                  AND release_updated_at IS NULL
                """
            )

            if settings.TORRENT_CACHE_TTL >= 0:
                await database.execute(
                    """
                    DELETE FROM torrents
                    WHERE updated_at < :min_timestamp
                    """,
                    {"min_timestamp": current_time - settings.TORRENT_CACHE_TTL},
                )

            await database.execute(
                """
                DELETE FROM debrid_availability
                WHERE updated_at < :min_timestamp
                """,
                {"min_timestamp": current_time - settings.DEBRID_CACHE_TTL},
            )

            await database.execute(
                """
                DELETE FROM debrid_account_magnets
                WHERE synced_at < :min_timestamp
                """,
                {"min_timestamp": current_time - _debrid_account_snapshot_ttl()},
            )

            await database.execute(
                """
                DELETE FROM debrid_account_sync_state
                WHERE last_sync_at < :min_timestamp
                """,
                {
                    "min_timestamp": current_time
                    - (_debrid_account_snapshot_ttl() * 2),
                },
            )

            await database.execute(
                """
                DELETE FROM download_links_cache
                WHERE updated_at < :min_timestamp
                """,
                {"min_timestamp": current_time - DOWNLOAD_LINK_CACHE_TTL},
            )

            await database.execute(
                """
                DELETE FROM kodi_setup_codes
                WHERE expires_at < :current_time
                   OR consumed_at IS NOT NULL
                """,
                {"current_time": current_time},
            )

            run_retention_days = settings.BACKGROUND_SCRAPER_RUN_RETENTION_DAYS
            if run_retention_days > 0:
                await database.execute(
                    """
                    DELETE FROM background_scraper_runs
                    WHERE started_at < :min_timestamp
                    """,
                    {
                        "min_timestamp": current_time - (run_retention_days * 86400),
                    },
                )

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
    except Exception as e:
        logger.error(f"Error executing startup cleanup: {e}")


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


async def _migrate_indexes():
    legacy_indexes = [
        "torrents_series_both_idx",
        "torrents_season_only_idx",
        "torrents_episode_only_idx",
        "torrents_no_season_episode_idx",
        "idx_torrents_media_cache_lookup",
        "idx_torrents_tracker_analytics",
        "idx_torrents_size_filter",
        "idx_torrents_seeders_desc",
        "idx_torrents_quality_cache",
        "idx_torrents_media_season_episode",
        "torrents_cache_lookup_idx",
        "idx_torrents_timestamp",
        "torrents_seeders_idx",
        "unq_torrents_series",
        "unq_torrents_season",
        "unq_torrents_episode",
        "unq_torrents_movie",
        "idx_torrents_lookup",
        "idx_torrents_info_hash",
        "debrid_series_both_idx",
        "debrid_season_only_idx",
        "debrid_episode_only_idx",
        "debrid_no_season_episode_idx",
        "idx_debrid_service_hash_cache",
        "idx_debrid_season_episode_filter",
        "idx_debrid_service_timestamp",
        "idx_debrid_title_filter",
        "idx_debrid_comprehensive",
        "idx_debrid_info_hash_season_episode",
        "idx_debrid_timestamp",
        "unq_debrid_series",
        "unq_debrid_season",
        "unq_debrid_episode",
        "unq_debrid_movie",
        "idx_debrid_lookup",
        "idx_debrid_info_hash",
        "idx_debrid_hash_season_episode",
        "download_links_series_both_idx",
        "download_links_season_only_idx",
        "download_links_episode_only_idx",
        "download_links_no_season_episode_idx",
        "download_links_series_both_v2_idx",
        "download_links_season_only_v2_idx",
        "download_links_episode_only_v2_idx",
        "download_links_no_season_episode_v2_idx",
        "idx_download_links_playback",
        "idx_download_links_playback_v2",
        "idx_download_links_cleanup",
        "idx_first_searches_cleanup",
        "idx_metadata_title_search",
        "idx_metadata_cache_lookup",
        "idx_digital_release_timestamp",
        "idx_anime_ids_entry_id",
        "idx_scrape_locks_expires_at",
        "idx_scrape_locks_lock_key",
        "idx_scrape_locks_instance",
        "idx_connections_timestamp_desc",
        "idx_connections_ip_filter",
        "idx_connections_content_monitoring",
        "idx_kodi_setup_codes_expires",
        "idx_debrid_account_lookup",
        "idx_debrid_account_cleanup",
        "idx_bg_items_media_retry_priority",
        "idx_bg_items_status",
        "idx_bg_items_plan_window",
        "idx_bg_episodes_series_retry",
        "idx_bg_episodes_plan_window",
        "idx_bg_runs_started",
        "idx_bg_runs_status",
        "idx_anime_ids_entry_provider",
        "idx_dmm_parsed_title",
        "idx_dmm_parsed_year",
    ]

    dropped_count = 0
    for index_name in legacy_indexes:
        try:
            if IS_SQLITE:
                exists = await database.fetch_val(
                    """
                    SELECT 1
                    FROM sqlite_master
                    WHERE type = 'index' AND name = :index_name
                    """,
                    {"index_name": index_name},
                    force_primary=True,
                )
            else:
                exists = await database.fetch_val(
                    """
                    SELECT 1
                    FROM pg_indexes
                    WHERE schemaname = current_schema()
                      AND indexname = :index_name
                    """,
                    {"index_name": index_name},
                    force_primary=True,
                )
            if not exists:
                continue

            await database.execute(f"DROP INDEX IF EXISTS {index_name}")
            dropped_count += 1
        except Exception:
            continue

    if dropped_count > 0:
        logger.log(
            "DATABASE",
            f"Legacy indexes cleanup completed. Dropped {dropped_count} indexes.",
        )


async def teardown_database():
    try:
        await database.disconnect()
    except Exception as e:
        logger.error(f"Error tearing down the database: {e}")
        logger.exception(traceback.format_exc())
