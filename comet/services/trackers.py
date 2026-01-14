import aiohttp

from comet.core.logger import logger

trackers = []


async def download_best_trackers():
    try:
        async with aiohttp.ClientSession() as session:
            response = await session.get(
                "https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt"
            )
            response = await response.text()

            trackers.extend([tracker for tracker in response.split("\n") if tracker])
            logger.log(
                "COMET",
                f"Generic Trackers: downloaded {len(trackers)} trackers",
            )
    except Exception as e:
        logger.warning(f"Failed to download best trackers: {e}")
