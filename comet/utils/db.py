import os

from comet.utils.logger import logger
from comet.utils.models import database, settings


async def setup_database():
    try:
        os.makedirs(os.path.dirname(settings.DATABASE_PATH), exist_ok=True)

        if not os.path.exists(settings.DATABASE_PATH):
            open(settings.DATABASE_PATH, "a").close()

        await database.connect()
        await database.execute(
            "CREATE TABLE IF NOT EXISTS cache (cacheKey BLOB PRIMARY KEY, timestamp INTEGER, results TEXT)"
        )
        await database.execute(
            "CREATE TABLE IF NOT EXISTS download_links (debrid_key TEXT, hash TEXT, `index` TEXT, link TEXT, timestamp INTEGER, PRIMARY KEY (debrid_key, hash, `index`))"
        )
        await database.execute("DROP TABLE IF EXISTS active_connections")
        await database.execute(
            "CREATE TABLE IF NOT EXISTS active_connections (id TEXT PRIMARY KEY, ip TEXT, content TEXT, timestamp INTEGER)"
        )
    except Exception as e:
        logger.error(f"Error setting up the database: {e}")


async def teardown_database():
    try:
        await database.disconnect()
    except Exception as e:
        logger.error(f"Error tearing down the database: {e}")
