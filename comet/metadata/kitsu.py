import aiohttp

from comet.core.logger import logger
from comet.utils.year import parse_year


def _extract_kitsu_metadata(payload) -> tuple[str | None, int | None, int | None]:
    if not isinstance(payload, dict):
        return None, None, None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None, None, None
    attributes = data.get("attributes")
    if not isinstance(attributes, dict):
        return None, None, None

    title = attributes.get("canonicalTitle")
    if not isinstance(title, str) or not title:
        titles = attributes.get("titles")
        title = None
        if isinstance(titles, dict):
            for key in ("en", "en_jp", "ja_jp"):
                candidate = titles.get(key)
                if isinstance(candidate, str) and candidate:
                    title = candidate
                    break

    year = parse_year(attributes.get("startDate"))
    year_end = parse_year(attributes.get("endDate"))
    if year is not None and year_end is not None and year_end < year:
        year_end = None

    return title, year, year_end


async def get_kitsu_metadata(session: aiohttp.ClientSession, id: str):
    try:
        async with session.get(
            f"https://kitsu.io/api/edge/anime/{id}",
        ) as response:
            metadata = await response.json()

        return _extract_kitsu_metadata(metadata)
    except Exception as e:
        logger.warning(f"Exception while getting Kitsu metadata for {id}: {e}")
        return None, None, None
