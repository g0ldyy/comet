import aiohttp

from comet.utils.logger import logger


async def get_imdb_metadata(session: aiohttp.ClientSession, id: str):
    try:
        response = await session.get(
            f"https://v3.sg.media-imdb.com/suggestion/a/{id}.json"
        )
        metadata = await response.json()
        for element in metadata["d"]:
            if "/" not in element["id"]:
                title = element["l"]
                year = element.get("y")
                year_end = int(element["yr"].split("-")[1]) if "yr" in element else None
                return title, year, year_end
    except Exception as e:
        logger.warning(f"Exception while getting IMDB metadata for {id}: {e}")
        return None, None, None
