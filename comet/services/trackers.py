import aiohttp

from comet.core.logger import logger

trackers = []


async def download_best_trackers():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt"
            ) as response:
                text = await response.text()

        trackers.clear()
        trackers.extend(line for line in text.split("\n") if line)
        logger.log(
            "COMET",
            f"Generic Trackers: downloaded {len(trackers)} trackers",
        )
    except Exception as e:
        logger.warning(f"Failed to download best trackers: {e}")
