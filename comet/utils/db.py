import os

from comet.utils.logger import logger
from comet.utils.models import database, settings


async def setup_database():
    """Setup the database by ensuring the directory and file exist, and creating the necessary tables."""
    try:
        # Ensure the database directory exists
        os.makedirs(os.path.dirname(settings.DATABASE_PATH), exist_ok=True)
        
        # Ensure the database file exists
        if not os.path.exists(settings.DATABASE_PATH):
            open(settings.DATABASE_PATH, "a").close()
        
        await database.connect()
        await database.execute("CREATE TABLE IF NOT EXISTS cache (cacheKey BLOB PRIMARY KEY, timestamp INTEGER, results TEXT)")
    except Exception as e:
        logger.error(f"Error setting up the database: {e}")


async def teardown_database():
    """Teardown the database by disconnecting."""
    try:
        await database.disconnect()
    except Exception as e:
        logger.error(f"Error tearing down the database: {e}")