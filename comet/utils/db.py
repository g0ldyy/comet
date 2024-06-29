import json
import os

from comet.utils.general import lang_code_map
from comet.utils.logger import logger
from comet.utils.models import database, settings


async def setup_database():
    """Setup the database by ensuring the directory and file exist, and creating the necessary tables."""
    try:
        # Ensure the database directory exists
        os.makedirs(os.path.dirname(settings.DATABASE_PATH), exist_ok=True)
        
        # Ensure the database file exists
        if not os.path.exists(settings.DATABASE_PATH):
            open(settings.DATABASE_PATH, 'a').close()
        
        await database.connect()
        await database.execute("CREATE TABLE IF NOT EXISTS cache (cacheKey BLOB PRIMARY KEY, timestamp INTEGER, results TEXT)")
    except Exception as e:
        logger.error(f"Error setting up the database: {e}")

async def teardown_database():
    """Teardown the database by disconnecting."""
    try:
        await database.disconnect()
    except Exception as e:
        # Log the exception or handle it as needed
        print(f"Error tearing down the database: {e}")

def write_config():
    """Write the config file."""
    indexers = settings.INDEXER_MANAGER_INDEXERS
    if indexers:
        if isinstance(indexers, str):
            indexers = indexers.split(",")
        elif not isinstance(indexers, list):
            logger.warning("Invalid indexers")

    config_data = {
        "indexers": indexers,
        "languages": lang_code_map,
        "resolutions": ["480p", "720p", "1080p", "1440p", "2160p", "2880p", "4320p"]
    }

    with open("comet/templates/config.json", "w", encoding="utf-8") as config_file:
        json.dump(config_data, config_file, indent=4)