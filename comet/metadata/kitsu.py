import aiohttp

from comet.core.logger import logger


async def get_kitsu_metadata(session: aiohttp.ClientSession, id: str):
    try:
        async with session.get(
            f"https://kitsu.io/api/edge/anime/{id}",
        ) as response:
            metadata = await response.json()

        attributes = metadata.get("data", {}).get("attributes")

        title = attributes.get("canonicalTitle")
        if not title:
            titles = attributes.get("titles") or {}
            title = titles.get("en") or titles.get("en_jp") or titles.get("ja_jp")

        year = None
        start_date = attributes.get("startDate")
        if start_date and len(start_date) >= 4:
            year = int(start_date[:4])

        year_end = None
        end_date = attributes.get("endDate")
        if end_date and len(end_date) >= 4:
            year_end = int(end_date[:4])

        if year is None:
            created_at = attributes.get("createdAt")
            if created_at and len(created_at) >= 4:
                year = int(created_at[:4])

        if year_end is None:
            updated_at = attributes.get("updatedAt")
            if updated_at and len(updated_at) >= 4:
                year_end = int(updated_at[:4])

        return title, year, year_end
    except Exception as e:
        logger.warning(f"Exception while getting Kitsu metadata for {id}: {e}")
        return None, None, None
