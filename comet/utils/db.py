import os
import time

from comet.utils.logger import logger
from comet.utils.models import database, settings


async def setup_database():
    try:
        if settings.DATABASE_TYPE == "sqlite":
            os.makedirs(os.path.dirname(settings.DATABASE_PATH), exist_ok=True)

            if not os.path.exists(settings.DATABASE_PATH):
                open(settings.DATABASE_PATH, "a").close()

        await database.connect()

        if settings.DATABASE_TYPE == "postgresql":
            check_query = """SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name = 'cache' 
                AND column_name = 'cachekey'"""
        else:
            check_query = """SELECT name FROM pragma_table_info('cache') 
                WHERE name = 'cacheKey'"""

        old_structure = await database.fetch_one(check_query)

        if old_structure:
            await database.execute("DROP TABLE IF EXISTS cache")

        await database.execute(
            "CREATE TABLE IF NOT EXISTS cache (debridService TEXT, info_hash TEXT, name TEXT, season INTEGER, episode INTEGER, tracker TEXT, data TEXT, timestamp INTEGER)"
        )
        await database.execute(
            "CREATE TABLE IF NOT EXISTS download_links (debrid_key TEXT, hash TEXT, file_index TEXT, link TEXT, timestamp INTEGER, PRIMARY KEY (debrid_key, hash, file_index))"
        )
        await database.execute("DROP TABLE IF EXISTS active_connections")
        await database.execute(
            "CREATE TABLE IF NOT EXISTS active_connections (id TEXT PRIMARY KEY, ip TEXT, content TEXT, timestamp INTEGER)"
        )

        # clear expired entries
        await database.execute(
            """
            DELETE FROM cache 
            WHERE timestamp + :cache_ttl < :current_time
            """,
            {"cache_ttl": settings.CACHE_TTL, "current_time": time.time()},
        )
    except Exception as e:
        logger.error(f"Error setting up the database: {e}")


async def teardown_database():
    try:
        await database.disconnect()
    except Exception as e:
        logger.error(f"Error tearing down the database: {e}")
