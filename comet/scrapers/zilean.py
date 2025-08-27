import aiohttp

from comet.utils.logger import logger


async def get_zilean(manager, session: aiohttp.ClientSession, url: str):
    torrents = []
    try:
        show = (
            f"&season={manager.season}&episode={manager.episode}"
            if manager.media_type == "series"
            else ""
        )
        get_dmm = await session.get(f"{url}/dmm/filtered?query={manager.title}{show}")
        get_dmm = await get_dmm.json()

        for result in get_dmm:
            object = {
                "title": result["raw_title"],
                "infoHash": result["info_hash"].lower(),
                "fileIndex": None,
                "seeders": None,
                "size": int(result["size"]),
                "tracker": "DMM",
                "sources": [],
            }

            torrents.append(object)
    except Exception as e:
        logger.warning(
            f"Exception while getting torrents for {manager.title} with Zilean ({url}): {e}"
        )

    await manager.filter_manager(torrents)
