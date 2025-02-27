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


async def get_kitsu_aliases(session: aiohttp.ClientSession, id: str):
    aliases = {}
    try:
        response = await session.get(f"https://find-my-anime.dtimur.de/api?id={id}&provider=Kitsu")
        data = await response.json()
        
        aliases["ez"] = []
        aliases["ez"].append(data[0]["title"])
        for synonym in data[0]["synonyms"]:
            aliases["ez"].append(synonym)

        total_aliases = len(aliases["ez"])
        if total_aliases > 0:
            logger.log(
                "SCRAPER",
                f"📜 Found {total_aliases} Kitsu aliases for {id}",
            )
            return aliases
    except Exception:
        pass

    logger.log("SCRAPER", f"📜 No Kitsu aliases found for {id}")

    return {}
