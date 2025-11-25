import aiohttp

from comet.core.logger import logger
from comet.core.models import trackers


async def download_best_trackers():
    try:
        async with aiohttp.ClientSession() as session:
            response = await session.get(
                "https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt"
            )
            response = await response.text()

            other_trackers = [tracker for tracker in response.split("\n") if tracker]
            trackers.extend(other_trackers)
    except Exception as e:
        logger.warning(f"Failed to download best trackers: {e}")
