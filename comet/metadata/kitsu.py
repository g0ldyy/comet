import aiohttp

from comet.utils.logger import logger


async def get_kitsu_metadata(session: aiohttp.ClientSession, id: str):
    try:
        response = await session.get(f"https://kitsu.io/api/edge/anime/{id}")
        metadata = await response.json()

        attributes = metadata["data"]["attributes"]
        year = int(attributes["createdAt"].split("-")[0])
        year_end = int(attributes["updatedAt"].split("-")[0])

        return attributes["canonicalTitle"], year, year_end
    except Exception as e:
        logger.warning(f"Exception while getting Kitsu metadata for {id}: {e}")
        return None, None, None