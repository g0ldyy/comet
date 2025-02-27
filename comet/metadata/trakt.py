import aiohttp

from comet.utils.logger import logger


async def get_trakt_aliases(
    session: aiohttp.ClientSession, media_type: str, media_id: str
):
    aliases = set()
    try:
        response = await session.get(
            f"https://api.trakt.tv/{'movies' if media_type == 'movie' else 'shows'}/{media_id}/aliases"
        )
        data = await response.json()

        for aliase in data:
            aliases.add(aliase["title"])

        total_aliases = len(aliases)
        if total_aliases > 0:
            logger.log(
                "SCRAPER",
                f"📜 Found {total_aliases} Trakt aliases for {media_id}",
            )
            return {"ez": list(aliases)}
    except Exception:
        pass

    logger.log("SCRAPER", f"📜 No Trakt aliases found for {media_id}")

    return {}
