import aiohttp

from comet.utils.logger import logger


async def get_trakt_aliases(
    session: aiohttp.ClientSession, media_type: str, media_id: str
):
    aliases = {}
    try:
        response = await session.get(
            f"https://api.trakt.tv/{'movies' if media_type == 'movie' else 'shows'}/{media_id}/aliases"
        )
        for aliase in await response.json():
            country = aliase["country"]
            if country not in aliases:
                aliases[country] = []
            aliases[country].append(aliase["title"])
    except Exception as e:
        logger.warning(f"Exception while getting Trakt aliases for {media_id}: {e}")
        pass
    return aliases
