import re
from dataclasses import dataclass
from urllib.parse import quote

import aiohttp

from comet.core.logger import logger
from comet.utils.year import parse_year, parse_year_range

_IMDB_SUGGESTION_URL = "https://v3.sg.media-imdb.com/suggestion/a/{id}.json"
_CINEMETA_META_URL = "https://v3-cinemeta.strem.io/meta/{media_type}/{id}.json"
_CINEMETA_MEDIA_TYPES = ("movie", "series")
_IMDB_ID = re.compile(r"tt[0-9]{7,10}")
_MOVIE_TYPES = frozenset(
    {
        "feature",
        "movie",
        "short",
        "tvmovie",
        "tvshort",
        "video",
    }
)
_SERIES_TYPES = frozenset(
    {
        "miniseries",
        "series",
        "tvminiseries",
        "tvseries",
    }
)


@dataclass(frozen=True, slots=True)
class ImdbTitleMatch:
    imdb_id: str
    media_type: str
    year: int | None


def _suggestion_media_type(element: dict) -> str | None:
    for key in ("qid", "q"):
        raw_value = element.get(key)
        if not isinstance(raw_value, str):
            continue
        normalized = "".join(
            character for character in raw_value.lower() if character.isalnum()
        )
        if normalized in _MOVIE_TYPES:
            return "movie"
        if normalized in _SERIES_TYPES:
            return "series"
    return None


def _extract_title_match(
    payload: object,
    media_type: str | None = None,
    year: int | None = None,
) -> ImdbTitleMatch | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("d"), list):
        return None

    for element in payload["d"]:
        if not isinstance(element, dict):
            continue
        imdb_id = element.get("id")
        if not isinstance(imdb_id, str) or _IMDB_ID.fullmatch(imdb_id) is None:
            continue
        candidate_type = _suggestion_media_type(element)
        if candidate_type is None or (
            media_type is not None and candidate_type != media_type
        ):
            continue
        candidate_year = parse_year(element.get("y"))
        if year is not None and candidate_year != year:
            continue
        return ImdbTitleMatch(imdb_id, candidate_type, candidate_year)

    return None


async def resolve_imdb_title(
    session: aiohttp.ClientSession,
    query: str,
    media_type: str | None = None,
    year: int | None = None,
) -> ImdbTitleMatch | None:
    normalized_query = query.strip()
    if not normalized_query:
        return None

    url = _IMDB_SUGGESTION_URL.format(id=quote(normalized_query, safe=""))
    try:
        async with session.get(url) as response:
            if response.status != 200:
                logger.warning(
                    f"IMDb title search failed for {normalized_query!r}: "
                    f"HTTP {response.status}"
                )
                return None
            payload = await response.json()
    except Exception as exc:
        logger.warning(
            f"IMDb title search failed for {normalized_query!r}: {exc}"
        )
        return None

    return _extract_title_match(payload, media_type, year)


def _extract_imdb_metadata(payload: dict) -> tuple[str | None, int | None, int | None]:
    if not isinstance(payload, dict):
        return None, None, None

    elements = payload.get("d")
    if not isinstance(elements, list):
        return None, None, None

    for element in elements:
        if not isinstance(element, dict):
            continue
        item_id = element.get("id")
        if not isinstance(item_id, str):
            continue
        if "/" in item_id:
            continue

        title = element.get("l")
        if not isinstance(title, str) or not title:
            continue

        year = parse_year(element.get("y"))
        _, year_end = parse_year_range(element.get("yr"))
        return title, year, year_end

    return None, None, None


def _extract_cinemeta_metadata(
    payload: dict,
) -> tuple[str | None, int | None, int | None]:
    if not isinstance(payload, dict):
        return None, None, None

    meta = payload.get("meta")
    if not isinstance(meta, dict):
        return None, None, None
    title = meta.get("name")
    if not isinstance(title, str) or not title:
        return None, None, None

    year, year_end = parse_year_range(meta.get("year"))
    if year is None:
        year, year_end = parse_year_range(meta.get("releaseInfo"))

    if year is None:
        year = parse_year(meta.get("released"))

    return title, year, year_end


def _iter_cinemeta_media_types(media_type: str | None):
    if media_type in _CINEMETA_MEDIA_TYPES:
        return (media_type,)
    return _CINEMETA_MEDIA_TYPES


async def _get_cinemeta_metadata(
    session: aiohttp.ClientSession, id: str, media_type: str | None
) -> tuple[str | None, int | None, int | None]:
    for candidate_type in _iter_cinemeta_media_types(media_type):
        url = _CINEMETA_META_URL.format(media_type=candidate_type, id=id)

        try:
            async with session.get(url) as response:
                if response.status == 404:
                    continue
                if response.status != 200:
                    logger.warning(
                        f"Cinemeta metadata request failed for {id} ({candidate_type}): HTTP {response.status}"
                    )
                    continue

                payload = await response.json()
        except Exception as exc:
            logger.warning(
                f"Exception while getting Cinemeta metadata for {id} ({candidate_type}): {exc}"
            )
            continue

        parsed = _extract_cinemeta_metadata(payload)
        if parsed[0] is not None:
            return parsed

    return None, None, None


async def get_imdb_metadata(
    session: aiohttp.ClientSession, id: str, media_type: str | None = None
):
    metadata = None

    try:
        async with session.get(_IMDB_SUGGESTION_URL.format(id=id)) as response:
            if response.status == 429:
                logger.warning(f"IMDB metadata rate-limited for {id}, using Cinemeta")
                return await _get_cinemeta_metadata(session, id, media_type)

            if response.status != 200:
                logger.warning(
                    f"IMDB metadata request failed for {id}: HTTP {response.status}, using Cinemeta"
                )
                return await _get_cinemeta_metadata(session, id, media_type)

            metadata = await response.json()
    except Exception as exc:
        logger.warning(
            f"Exception while getting IMDB metadata for {id}: {exc}. Using Cinemeta fallback."
        )
        return await _get_cinemeta_metadata(session, id, media_type)

    parsed = _extract_imdb_metadata(metadata)
    if parsed[0] is not None:
        return parsed

    logger.warning(f"IMDB metadata empty for {id}, using Cinemeta fallback")
    fallback = await _get_cinemeta_metadata(session, id, media_type)
    if fallback[0] is not None:
        return fallback

    if metadata:
        logger.warning(f"No metadata found for {id}. IMDB response: {metadata}")

    return None, None, None
