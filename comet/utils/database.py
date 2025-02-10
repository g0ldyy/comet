import os
import time
import traceback

from comet.utils.logger import logger
from comet.utils.models import database, settings


async def setup_database():
    try:
        if settings.DATABASE_TYPE == "sqlite":
            os.makedirs(os.path.dirname(settings.DATABASE_PATH), exist_ok=True)

            if not os.path.exists(settings.DATABASE_PATH):
                open(settings.DATABASE_PATH, "a").close()

        await database.connect()

        await database.execute(
            "CREATE TABLE IF NOT EXISTS ongoing_searches (media_id TEXT PRIMARY KEY, timestamp INTEGER)"
        )
        await database.execute(
            "CREATE INDEX IF NOT EXISTS idx_ongoing_searches_timestamp ON ongoing_searches(timestamp)"
        )

        await database.execute(
            "CREATE TABLE IF NOT EXISTS metadata_cache (media_id TEXT PRIMARY KEY, title TEXT, year INTEGER, year_end INTEGER, aliases TEXT, timestamp INTEGER)"
        )
        await database.execute(
            "CREATE INDEX IF NOT EXISTS idx_metadata_timestamp ON metadata_cache(timestamp)"
        )
        await database.execute(
            "CREATE INDEX IF NOT EXISTS idx_metadata_title ON metadata_cache(title)"
        )

        await database.execute(
            "CREATE TABLE IF NOT EXISTS torrents_cache (info_hash TEXT PRIMARY KEY, media_id TEXT, season INTEGER, episode INTEGER, data TEXT, timestamp INTEGER)"
        )
        await database.execute(
            "CREATE INDEX IF NOT EXISTS idx_torrents_media ON torrents_cache(media_id, season, episode)"
        )
        await database.execute(
            "CREATE INDEX IF NOT EXISTS idx_torrents_timestamp ON torrents_cache(timestamp)"
        )

        await database.execute(
            "CREATE TABLE IF NOT EXISTS availability_cache (id SERIAL PRIMARY KEY, debrid_service TEXT, info_hash TEXT, season INTEGER, episode INTEGER, file_index TEXT, title TEXT, size BIGINT, file_data TEXT, timestamp INTEGER, UNIQUE(debrid_service, info_hash, season, episode))"
        )
        await database.execute(
            "CREATE INDEX IF NOT EXISTS idx_availability_lookup ON availability_cache(info_hash, debrid_service, season, episode)"
        )
        await database.execute(
            "CREATE INDEX IF NOT EXISTS idx_availability_timestamp ON availability_cache(timestamp)"
        )

        await database.execute(
            "CREATE TABLE IF NOT EXISTS download_links_cache (debrid_key TEXT, info_hash TEXT, file_index TEXT, download_url TEXT, timestamp INTEGER, PRIMARY KEY (debrid_key, info_hash, file_index))"
        )
        await database.execute(
            "CREATE INDEX IF NOT EXISTS idx_downloads_timestamp ON download_links_cache(timestamp)"
        )

        await database.execute(
            "CREATE TABLE IF NOT EXISTS active_connections (id TEXT PRIMARY KEY, ip TEXT, content TEXT, timestamp INTEGER)"
        )
        await database.execute(
            "CREATE INDEX IF NOT EXISTS idx_connections_timestamp ON active_connections(timestamp)"
        )

        if settings.DATABASE_TYPE == "sqlite":
            await database.execute("PRAGMA journal_mode=WAL")
            await database.execute("PRAGMA synchronous=NORMAL")
            await database.execute("PRAGMA temp_store=MEMORY")
            await database.execute("PRAGMA mmap_size=30000000000")
            await database.execute("PRAGMA page_size=4096")
            await database.execute("PRAGMA cache_size=-2000")

        await database.execute("DELETE FROM ongoing_searches")

        await database.execute(
            """
            DELETE FROM metadata_cache 
            WHERE timestamp + :cache_ttl < :current_time;
            """,
            {"cache_ttl": settings.CACHE_TTL, "current_time": time.time()},
        )

        await database.execute(
            """
            DELETE FROM torrents_cache 
            WHERE timestamp + :cache_ttl < :current_time;
            """,
            {"cache_ttl": settings.CACHE_TTL, "current_time": time.time()},
        )

        await database.execute(
            """
            DELETE FROM availability_cache 
            WHERE timestamp + :cache_ttl < :current_time;
            """,
            {"cache_ttl": settings.CACHE_TTL, "current_time": time.time()},
        )

        await database.execute("DELETE FROM download_links_cache")

        await database.execute("DELETE FROM active_connections")

    except Exception as e:
        logger.error(f"Error setting up the database: {e}")
        logger.exception(traceback.format_exc())


async def teardown_database():
    try:
        await database.disconnect()
    except Exception as e:
        logger.error(f"Error tearing down the database: {e}")
        logger.exception(traceback.format_exc())
