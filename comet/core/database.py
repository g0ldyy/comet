import asyncio
import os
import time
import traceback

from comet.core.logger import logger
from comet.core.models import database, settings

STARTUP_CLEANUP_LOCK_ID = 0xC0FFEE

DATABASE_VERSION = "1.0"


async def setup_database():
    try:
        if settings.DATABASE_TYPE == "sqlite":
            os.makedirs(os.path.dirname(settings.DATABASE_PATH), exist_ok=True)

            if not os.path.exists(settings.DATABASE_PATH):
                open(settings.DATABASE_PATH, "a").close()

        await database.connect()

        await _migrate_indexes()

        await database.execute(
            """
                CREATE TABLE IF NOT EXISTS db_version (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    version TEXT
                )
            """
        )

        current_version = await database.fetch_val(
            """
                SELECT version FROM db_version WHERE id = 1
            """
        )

        if current_version != DATABASE_VERSION:
            logger.log(
                "COMET",
                f"Database: Migration from {current_version} to {DATABASE_VERSION} version",
            )

            if settings.DATABASE_TYPE == "sqlite":
                tables = await database.fetch_all(
                    """
                    SELECT name FROM sqlite_master 
                    WHERE type='table' AND name != 'db_version' AND name != 'sqlite_sequence'
                    """
                )

                for table in tables:
                    await database.execute(f"DROP TABLE IF EXISTS {table['name']}")
            else:
                await database.execute(
                    """
                    DO $$ DECLARE
                        r RECORD;
                    BEGIN
                        FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname = current_schema() AND tablename != 'db_version') LOOP
                            EXECUTE 'DROP TABLE IF EXISTS ' || quote_ident(r.tablename) || ' CASCADE';
                        END LOOP;
                    END $$;
                    """
                )

            await database.execute(
                """
                    INSERT INTO db_version VALUES (1, :version)
                    ON CONFLICT (id) DO UPDATE SET version = :version
                """,
                {"version": DATABASE_VERSION},
            )

            logger.log(
                "COMET", f"Database: Migration to version {DATABASE_VERSION} completed"
            )

        await database.execute(
            """
                CREATE TABLE IF NOT EXISTS db_maintenance (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    last_startup_cleanup REAL
                )
            """
        )

        await database.execute(
            """
                CREATE TABLE IF NOT EXISTS scrape_locks (
                    lock_key TEXT PRIMARY KEY,
                    instance_id TEXT,
                    timestamp INTEGER,
                    expires_at INTEGER
                )
            """
        )

        await database.execute(
            """
                CREATE TABLE IF NOT EXISTS first_searches (
                    media_id TEXT PRIMARY KEY, 
                    timestamp INTEGER
                )
            """
        )

        await database.execute(
            """
                CREATE TABLE IF NOT EXISTS admin_sessions (
                    session_id TEXT PRIMARY KEY,
                    created_at INTEGER,
                    expires_at INTEGER
                )
            """
        )

        await database.execute(
            """
                CREATE TABLE IF NOT EXISTS metadata_cache (
                    media_id TEXT PRIMARY KEY, 
                    title TEXT, 
                    year INTEGER, 
                    year_end INTEGER, 
                    aliases TEXT, 
                    timestamp INTEGER
                )
            """
        )

        await database.execute(
            """
                CREATE TABLE IF NOT EXISTS torrents (
                    media_id TEXT,
                    info_hash TEXT,
                    file_index INTEGER,
                    season INTEGER,
                    episode INTEGER,
                    title TEXT,
                    seeders INTEGER,
                    size BIGINT,
                    tracker TEXT,
                    sources TEXT,
                    parsed TEXT,
                    timestamp INTEGER
                )
            """
        )

        await database.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS unq_torrents_series 
            ON torrents (media_id, info_hash, season, episode) 
            WHERE season IS NOT NULL AND episode IS NOT NULL
            """
        )

        await database.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS unq_torrents_season 
            ON torrents (media_id, info_hash, season) 
            WHERE season IS NOT NULL AND episode IS NULL
            """
        )

        await database.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS unq_torrents_episode 
            ON torrents (media_id, info_hash, episode) 
            WHERE season IS NULL AND episode IS NOT NULL
            """
        )

        await database.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS unq_torrents_movie 
            ON torrents (media_id, info_hash) 
            WHERE season IS NULL AND episode IS NULL
            """
        )

        await database.execute(
            """
                CREATE TABLE IF NOT EXISTS debrid_availability (
                    debrid_service TEXT,
                    info_hash TEXT,
                    file_index TEXT,
                    title TEXT,
                    season INTEGER,
                    episode INTEGER,
                    size BIGINT,
                    parsed TEXT,
                    timestamp INTEGER
                )
            """
        )

        await database.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS unq_debrid_series 
            ON debrid_availability (debrid_service, info_hash, season, episode) 
            WHERE season IS NOT NULL AND episode IS NOT NULL
            """
        )

        await database.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS unq_debrid_season 
            ON debrid_availability (debrid_service, info_hash, season) 
            WHERE season IS NOT NULL AND episode IS NULL
            """
        )

        await database.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS unq_debrid_episode 
            ON debrid_availability (debrid_service, info_hash, episode) 
            WHERE season IS NULL AND episode IS NOT NULL
            """
        )

        await database.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS unq_debrid_movie 
            ON debrid_availability (debrid_service, info_hash) 
            WHERE season IS NULL AND episode IS NULL
            """
        )

        await database.execute(
            """
                CREATE TABLE IF NOT EXISTS download_links_cache (
                    debrid_key TEXT, 
                    info_hash TEXT, 
                    season INTEGER,
                    episode INTEGER,
                    download_url TEXT, 
                    timestamp INTEGER
                )
            """
        )

        await database.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS download_links_series_both_idx 
            ON download_links_cache (debrid_key, info_hash, season, episode) 
            WHERE season IS NOT NULL AND episode IS NOT NULL
            """
        )

        await database.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS download_links_season_only_idx 
            ON download_links_cache (debrid_key, info_hash, season) 
            WHERE season IS NOT NULL AND episode IS NULL
            """
        )

        await database.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS download_links_episode_only_idx 
            ON download_links_cache (debrid_key, info_hash, episode) 
            WHERE season IS NULL AND episode IS NOT NULL
            """
        )

        await database.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS download_links_no_season_episode_idx 
            ON download_links_cache (debrid_key, info_hash) 
            WHERE season IS NULL AND episode IS NULL
            """
        )

        await database.execute(
            """
                CREATE TABLE IF NOT EXISTS active_connections (
                    id TEXT PRIMARY KEY, 
                    ip TEXT, 
                    content TEXT, 
                    timestamp INTEGER
                )
            """
        )

        await database.execute(
            """
                CREATE TABLE IF NOT EXISTS bandwidth_stats (
                    id INTEGER PRIMARY KEY, 
                    total_bytes BIGINT, 
                    last_updated INTEGER
                )
            """
        )

        await database.execute(
            """
                CREATE TABLE IF NOT EXISTS background_scraper_progress (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    current_run_started_at INTEGER,
                    last_completed_run_at INTEGER,
                    is_running BOOLEAN DEFAULT FALSE,
                    current_phase TEXT,
                    total_movies_processed INTEGER,
                    total_series_processed INTEGER
                )
            """
        )

        await database.execute(
            """
                CREATE TABLE IF NOT EXISTS background_scraper_state (
                    media_id TEXT PRIMARY KEY,
                    media_type TEXT,
                    title TEXT,
                    year INTEGER,
                    scraped_at INTEGER,
                    total_torrents_found INTEGER,
                    scrape_failed_attempts INTEGER
                )
            """
        )

        await database.execute(
            """
                CREATE TABLE IF NOT EXISTS metrics_cache (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    data TEXT,
                    timestamp INTEGER
                )
            """
        )

        await database.execute(
            """
                CREATE TABLE IF NOT EXISTS anime_entries (
                    id INTEGER PRIMARY KEY,
                    data TEXT
                )
            """
        )

        await database.execute(
            """
                CREATE TABLE IF NOT EXISTS anime_ids (
                    provider TEXT,
                    provider_id TEXT,
                    entry_id INTEGER,
                    PRIMARY KEY (provider, provider_id)
                )
            """
        )

        await database.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_anime_ids_entry_provider
            ON anime_ids (entry_id, provider, provider_id)
            """
        )

        await database.execute(
            """
                CREATE TABLE IF NOT EXISTS anime_mapping_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    refreshed_at INTEGER
                )
            """
        )

        await database.execute(
            """
                CREATE TABLE IF NOT EXISTS digital_release_cache (
                    media_id TEXT PRIMARY KEY,
                    release_date BIGINT,
                    timestamp INTEGER
                )
            """
        )

        # =============================================================================
        # TORRENTS TABLE INDEXES
        # =============================================================================

        # Primary lookup index: media_id + season + episode (nullable) + timestamp
        # Covers: get_cached_torrents, check_torrents_cache
        await database.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_torrents_lookup 
            ON torrents (media_id, season, episode, timestamp)
            """
        )

        # Optimization for concurrent DELETEs: info_hash + season
        # Covers: DELETE FROM torrents WHERE (info_hash, season) IN (...)
        await database.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_torrents_info_hash_season 
            ON torrents (info_hash, season)
            """
        )

        # Optimization for lookups by info_hash only
        # Covers: SELECT sources, media_id FROM torrents WHERE info_hash = $1
        await database.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_torrents_info_hash 
            ON torrents (info_hash)
            """
        )

        # =============================================================================
        # DEBRID_AVAILABILITY TABLE INDEXES
        # =============================================================================

        # Primary lookup index: service + info_hash + timestamp
        # Covers: get_cached_availability (info_hash IN ...)
        await database.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_debrid_lookup 
            ON debrid_availability (debrid_service, info_hash, timestamp)
            """
        )

        # Index for torrent mode: queries without debrid_service filter
        # Covers: get_cached_availability when debrid_service == "torrent"
        await database.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_debrid_info_hash 
            ON debrid_availability (info_hash)
            """
        )

        # Composite index for full filter path with season/episode
        # Covers: get_cached_availability with season/episode filters
        await database.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_debrid_hash_season_episode 
            ON debrid_availability (info_hash, season, episode, timestamp)
            """
        )

        # =============================================================================
        # DOWNLOAD_LINKS_CACHE TABLE INDEXES
        # =============================================================================

        # Primary playback lookup: debrid_key + info_hash + season + episode
        await database.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_download_links_playback 
            ON download_links_cache (debrid_key, info_hash, season, episode, timestamp)
            """
        )

        # Timestamp-based cleanup
        await database.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_download_links_cleanup 
            ON download_links_cache (timestamp)
            """
        )

        # =============================================================================
        # METADATA_CACHE TABLE INDEXES
        # =============================================================================

        # Primary cache lookup: media_id + timestamp
        await database.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_metadata_cache_lookup 
            ON metadata_cache (media_id, timestamp)
            """
        )

        # =============================================================================
        # ACTIVE_CONNECTIONS TABLE INDEXES
        # =============================================================================

        # Admin dashboard ordering: timestamp DESC (most recent first)
        await database.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_connections_timestamp_desc 
            ON active_connections (timestamp DESC)
            """
        )

        # IP-based filtering and monitoring
        await database.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_connections_ip_filter 
            ON active_connections (ip, timestamp)
            """
        )

        # Connection monitoring by content type
        await database.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_connections_content_monitoring 
            ON active_connections (content, timestamp)
            """
        )

        # Instance-based lock monitoring
        await database.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_scrape_locks_instance 
            ON scrape_locks (instance_id, timestamp)
            """
        )

        # =============================================================================
        # ADMIN_SESSIONS TABLE INDEXES
        # =============================================================================

        # Session cleanup: expires_at < current_time
        await database.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_admin_sessions_expires 
            ON admin_sessions (expires_at)
            """
        )

        # =============================================================================
        # BACKGROUND_SCRAPER_STATE TABLE INDEXES
        # =============================================================================

        # Media type filtering for scraper analytics
        await database.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_scraper_state_media_type 
            ON background_scraper_state (media_type, scraped_at)
            """
        )

        # Scraping timestamp for progress tracking
        await database.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_scraper_state_scraped_at 
            ON background_scraper_state (scraped_at)
            """
        )

        # Failed attempts monitoring
        await database.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_scraper_state_failures 
            ON background_scraper_state (scrape_failed_attempts, scraped_at)
            """
        )

        await database.execute(
            """

            CREATE INDEX IF NOT EXISTS idx_digital_release_timestamp
            ON digital_release_cache (timestamp)
            """
        )

        if settings.DATABASE_TYPE == "sqlite":
            await database.execute("PRAGMA busy_timeout=30000")  # 30 seconds timeout
            await database.execute("PRAGMA journal_mode=WAL")
            await database.execute("PRAGMA synchronous=OFF")
            await database.execute("PRAGMA temp_store=MEMORY")
            await database.execute("PRAGMA mmap_size=30000000000")
            await database.execute("PRAGMA page_size=4096")
            await database.execute("PRAGMA cache_size=-2000")
            await database.execute("PRAGMA foreign_keys=OFF")
            await database.execute("PRAGMA count_changes=OFF")
            await database.execute("PRAGMA secure_delete=OFF")
            await database.execute("PRAGMA auto_vacuum=OFF")

        await database.execute("DELETE FROM active_connections")
        await database.execute("DELETE FROM metrics_cache")

        await _run_startup_cleanup()

    except Exception as e:
        logger.error(f"Error setting up the database: {e}")
        logger.exception(traceback.format_exc())


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
            if settings.DATABASE_TYPE == "postgresql":
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

            await database.execute(
                """
                DELETE FROM first_searches 
                WHERE timestamp < CAST(:current_time AS BIGINT) - CAST(:cache_ttl AS BIGINT);
                """,
                {"cache_ttl": settings.TORRENT_CACHE_TTL, "current_time": current_time},
            )

            await database.execute(
                """
                DELETE FROM metadata_cache 
                WHERE timestamp < CAST(:current_time AS BIGINT) - CAST(:cache_ttl AS BIGINT);
                """,
                {
                    "cache_ttl": settings.METADATA_CACHE_TTL,
                    "current_time": current_time,
                },
            )

            if settings.TORRENT_CACHE_TTL >= 0:
                await database.execute(
                    """
                    DELETE FROM torrents
                    WHERE timestamp < CAST(:current_time AS BIGINT) - CAST(:cache_ttl AS BIGINT);
                    """,
                    {
                        "cache_ttl": settings.TORRENT_CACHE_TTL,
                        "current_time": current_time,
                    },
                )

            await database.execute(
                """
                DELETE FROM debrid_availability
                WHERE timestamp < CAST(:current_time AS BIGINT) - CAST(:cache_ttl AS BIGINT);
                """,
                {"cache_ttl": settings.DEBRID_CACHE_TTL, "current_time": current_time},
            )

            await database.execute(
                """
                DELETE FROM digital_release_cache
                WHERE timestamp < CAST(:current_time AS BIGINT) - CAST(:cache_ttl AS BIGINT);
                """,
                {
                    "cache_ttl": settings.METADATA_CACHE_TTL,
                    "current_time": current_time,
                },
            )

            await database.execute("DELETE FROM download_links_cache")

            await database.execute(
                """
                INSERT INTO db_maintenance (id, last_startup_cleanup)
                VALUES (1, :timestamp)
                ON CONFLICT (id) DO UPDATE SET last_startup_cleanup = :timestamp
                """,
                {"timestamp": current_time},
                force_primary=True,
            )
    except Exception as e:
        logger.error(f"Error executing startup cleanup: {e}")


async def _should_run_startup_cleanup(current_time: float, interval: int):
    row = await database.fetch_one(
        "SELECT last_startup_cleanup FROM db_maintenance WHERE id = 1",
        force_primary=True,
    )
    if not row or row["last_startup_cleanup"] is None:
        return True

    last_run = float(row["last_startup_cleanup"])
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
            logger.log("LOCK", f"❌ Error during periodic lock cleanup: {e}")

        await asyncio.sleep(60)


async def cleanup_expired_sessions():
    while True:
        try:
            current_time = time.time()
            await database.execute(
                "DELETE FROM admin_sessions WHERE expires_at < :current_time",
                {"current_time": current_time},
            )
        except Exception as e:
            logger.log("SESSION", f"❌ Error during periodic session cleanup: {e}")

        await asyncio.sleep(5)  # Clean up every 5 seconds


async def _migrate_indexes():
    try:
        old_indexes = [
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
            "torrents_cache_lookup_idx",
            "idx_scrape_locks_expires_at",
            "idx_scrape_locks_lock_key",
            "idx_torrents_timestamp",
            "torrents_seeders_idx",
            "idx_first_searches_cleanup",
            "idx_metadata_title_search",
            "idx_anime_ids_entry_id",
        ]

        dropped_count = 0
        for index_name in old_indexes:
            if settings.DATABASE_TYPE == "sqlite":
                exists = await database.fetch_val(
                    f"SELECT 1 FROM sqlite_master WHERE type='index' AND name='{index_name}'"
                )
            else:
                exists = await database.fetch_val(
                    f"SELECT 1 FROM pg_indexes WHERE indexname='{index_name}'"
                )

            if exists:
                await database.execute(f"DROP INDEX IF EXISTS {index_name}")
                dropped_count += 1
                logger.log("COMET", f"Database: Dropped legacy index '{index_name}'")

        if dropped_count > 0:
            logger.log(
                "COMET",
                f"Database: Legacy indexes cleanup completed. Dropped {dropped_count} indexes.",
            )
    except Exception as e:
        logger.warning(f"Error during index migration: {e}")


async def teardown_database():
    try:
        await database.disconnect()
    except Exception as e:
        logger.error(f"Error tearing down the database: {e}")
        logger.exception(traceback.format_exc())
