import aiohttp

from comet.core.logger import logger


async def get_imdb_metadata(session: aiohttp.ClientSession, id: str):
    try:
        async with session.get(
            f"https://v3.sg.media-imdb.com/suggestion/a/{id}.json",
        ) as response:
            metadata = await response.json()

        for element in metadata["d"]:
            if "/" not in element["id"]:
                title = element["l"]
                year = element.get("y")

                year_end = None
                yr = element.get("yr")
                if yr:
                    _, _, end_part = yr.partition("-")
                    if end_part:
                        year_end = int(end_part)

                return title, year, year_end
    except Exception as e:
        additional_info = ""
        if metadata:
            additional_info = f"- API Response: {metadata}"
        logger.warning(
            f"Exception while getting IMDB metadata for {id}: {e}{additional_info}"
        )
        return None, None, None
