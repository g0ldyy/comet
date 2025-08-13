import os
import time
import traceback

from comet.utils.logger import logger
from comet.utils.models import database, settings

DATABASE_VERSION = "1.3"


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
            # For >=1.1 perform non-destructive, additive migrations
            # Create new tables if they don't exist
            await database.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'user',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL
                )
                """
            )

            await database.execute(
                """
                CREATE TABLE IF NOT EXISTS api_tokens (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    name TEXT,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER,
                    last_used INTEGER,
                    usage_count INTEGER NOT NULL DEFAULT 0,
                    monthly_quota INTEGER,
                    scope TEXT,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                )
                """
            )

            # 1.2 additive migration items
            # Create configs table
            await database.execute(
                """
                CREATE TABLE IF NOT EXISTS configs (
                    id TEXT PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    config_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )
            # Add current_config_id column to users if missing
            try:
                await database.execute("ALTER TABLE users ADD COLUMN current_config_id TEXT")
            except Exception:
                pass

            # Sessions table for user login sessions (1.3)
            await database.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    token TEXT UNIQUE NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                )
                """
            )

            # Ensure db_version row exists/updated
            await database.execute(
                """
                INSERT INTO db_version (id, version) VALUES (1, :version)
                ON CONFLICT (id) DO UPDATE SET version = :version
                """,
                {"version": DATABASE_VERSION},
            )

            logger.log("COMET", f"Database: Migration to version {DATABASE_VERSION} completed (additive)")

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
    """Periodic cleanup task for expired locks."""
    import asyncio
    import time
    from comet.utils.logger import logger

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
