import aiohttp

from comet.utils.models import settings
from comet.utils.logger import logger


async def get_zilean(
    manager, session: aiohttp.ClientSession, title: str, season: int, episode: int
):
    torrents = []
    try:
        show = f"&season={season}&episode={episode}"
        get_dmm = await session.get(
            f"{settings.ZILEAN_URL}/dmm/filtered?query={title}{show if season else ''}"
        )
        get_dmm = await get_dmm.json()

        for result in get_dmm:
            object = {
                "title": result["raw_title"],
                "infoHash": result["info_hash"],
                "fileIndex": 0,
                "seeders": None,
                "size": int(result["size"]),
                "tracker": "DMM",
                "sources": [],
            }

            torrents.append(object)
    except Exception as e:
        logger.warning(f"Exception while getting torrents for {title} with Zilean: {e}")

    await manager.filter_manager(torrents)
