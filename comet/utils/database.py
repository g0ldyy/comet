import os
import time
import traceback
import asyncio

from comet.utils.logger import logger
from comet.utils.models import database, settings

DATABASE_VERSION = "1.0"


async def setup_database():
    try:
        if settings.DATABASE_TYPE == "sqlite":
            os.makedirs(os.path.dirname(settings.DATABASE_PATH), exist_ok=True)

            if not os.path.exists(settings.DATABASE_PATH):
                open(settings.DATABASE_PATH, "a").close()

        await database.connect()

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
                CREATE TABLE IF NOT EXISTS ongoing_searches (
                    media_id TEXT PRIMARY KEY, 
                    timestamp INTEGER
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
            CREATE UNIQUE INDEX IF NOT EXISTS torrents_series_both_idx 
            ON torrents (media_id, info_hash, season, episode) 
            WHERE season IS NOT NULL AND episode IS NOT NULL
            """
        )

        await database.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS torrents_season_only_idx 
            ON torrents (media_id, info_hash, season) 
            WHERE season IS NOT NULL AND episode IS NULL
            """
        )

        await database.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS torrents_episode_only_idx 
            ON torrents (media_id, info_hash, episode) 
            WHERE season IS NULL AND episode IS NOT NULL
            """
        )

        await database.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS torrents_no_season_episode_idx 
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
            CREATE UNIQUE INDEX IF NOT EXISTS debrid_series_both_idx 
            ON debrid_availability (debrid_service, info_hash, season, episode) 
            WHERE season IS NOT NULL AND episode IS NOT NULL
            """
        )

        await database.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS debrid_season_only_idx 
            ON debrid_availability (debrid_service, info_hash, season) 
            WHERE season IS NOT NULL AND episode IS NULL
            """
        )

        await database.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS debrid_episode_only_idx 
            ON debrid_availability (debrid_service, info_hash, episode) 
            WHERE season IS NULL AND episode IS NOT NULL
            """
        )

        await database.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS debrid_no_season_episode_idx 
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
                    total_bytes INTEGER, 
                    last_updated INTEGER
                )
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

        await database.execute("DELETE FROM ongoing_searches")

        await database.execute(
            """
            DELETE FROM first_searches 
            WHERE timestamp + :cache_ttl < :current_time;
            """,
            {"cache_ttl": settings.TORRENT_CACHE_TTL, "current_time": time.time()},
        )

        await database.execute(
            """
            DELETE FROM metadata_cache 
            WHERE timestamp + :cache_ttl < :current_time;
            """,
            {"cache_ttl": settings.METADATA_CACHE_TTL, "current_time": time.time()},
        )

        await database.execute(
            """
            DELETE FROM torrents
            WHERE timestamp + :cache_ttl < :current_time;
            """,
            {"cache_ttl": settings.TORRENT_CACHE_TTL, "current_time": time.time()},
        )

        await database.execute(
            """
            DELETE FROM debrid_availability
            WHERE timestamp + :cache_ttl < :current_time;
            """,
            {"cache_ttl": settings.DEBRID_CACHE_TTL, "current_time": time.time()},
        )

        await database.execute("DELETE FROM download_links_cache")

        await database.execute("DELETE FROM active_connections")

    except Exception as e:
        logger.error(f"Error setting up the database: {e}")
        logger.exception(traceback.format_exc())


async def cleanup_expired_locks():
    while True:
        try:
            current_time = int(time.time())
            await database.execute(
                "DELETE FROM scrape_locks WHERE expires_at < :current_time",
                {"current_time": current_time},
            )
        except Exception as e:
            logger.log("LOCK", f"❌ Error during periodic lock cleanup: {e}")

        await asyncio.sleep(60)


async def teardown_database():
    try:
        await database.disconnect()
    except Exception as e:
        logger.error(f"Error tearing down the database: {e}")
        logger.exception(traceback.format_exc())
