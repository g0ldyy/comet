import re

import aiohttp

from comet.metadata.http import MetadataHttpError, get_metadata_json
from comet.metadata.validation import metadata_text, metadata_year
from comet.utils.year import parse_year

_KITSU_ID = re.compile(r"[1-9][0-9]{0,18}")


def _extract_kitsu_metadata(payload: dict) -> tuple[str | None, int | None, int | None]:
    data = payload.get("data")
    if not isinstance(data, dict):
        return None, None, None
    attributes = data.get("attributes")
    if not isinstance(attributes, dict):
        return None, None, None

    title = metadata_text(attributes.get("canonicalTitle"))
    if title is None:
        titles = attributes.get("titles")
        if isinstance(titles, dict):
            for raw_title in titles.values():
                candidate = metadata_text(raw_title)
                if candidate is not None:
                    title = candidate
                    break

    year = metadata_year(parse_year(attributes.get("startDate")))
    year_end = metadata_year(parse_year(attributes.get("endDate")))
    if year is not None and year_end is not None and year_end < year:
        year_end = None

    return title, year, year_end


async def get_kitsu_metadata(session: aiohttp.ClientSession, id: str):
    if not isinstance(id, str) or _KITSU_ID.fullmatch(id) is None:
        return None, None, None
    response = await get_metadata_json(
        session,
        f"https://kitsu.io/api/edge/anime/{id}",
    )
    if response.status == 404:
        return None, None, None
    if not response.successful:
        raise MetadataHttpError("metadata service is unavailable")
    return _extract_kitsu_metadata(response.payload)
