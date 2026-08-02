import re
import time
from dataclasses import dataclass
from urllib.parse import quote

import aiohttp

from comet.core.models import database, settings
from comet.metadata.http import MetadataHttpError, get_metadata_json
from comet.metadata.validation import metadata_text, metadata_year
from comet.utils.year import parse_year, parse_year_range

_IMDB_SUGGESTION_URL = "https://v3.sg.media-imdb.com/suggestion/a/{id}.json"
_CINEMETA_META_URL = "https://v3-cinemeta.strem.io/meta/{media_type}/{id}.json"
_CINEMETA_MEDIA_TYPES = ("movie", "series")
_IMDB_ID = re.compile(r"tt[0-9]{7,10}")
_MAX_TITLE_QUERY_BYTES = 512
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

_TITLE_LOOKUP_QUERY = """
    SELECT imdb_id, media_type, year
    FROM imdb_title_lookup
    WHERE query_key = :query_key
      AND updated_at >= CAST(:min_timestamp AS DOUBLE PRECISION)
"""

_UPSERT_TITLE_LOOKUP_QUERY = """
    INSERT INTO imdb_title_lookup (
        query_key,
        imdb_id,
        media_type,
        year,
        updated_at
    )
    VALUES (
        :query_key,
        :imdb_id,
        :media_type,
        :year,
        :updated_at
    )
    ON CONFLICT (query_key) DO UPDATE SET
        imdb_id = EXCLUDED.imdb_id,
        media_type = EXCLUDED.media_type,
        year = EXCLUDED.year,
        updated_at = EXCLUDED.updated_at
"""


@dataclass(frozen=True, slots=True)
class ImdbTitleMatch:
    imdb_id: str
    media_type: str
    year: int | None


def _title_lookup_key(query: str, media_type: str | None, year: int | None) -> str:
    """Key a resolution by its query and the filters that shaped the answer.

    The two prefixes are colon-free, so they always delimit the free-form query.
    """
    return f"{media_type or '*'}:{'*' if year is None else year}:{query.casefold()}"


async def _cached_title_match(query_key: str) -> ImdbTitleMatch | None:
    row = await database.fetch_one(
        _TITLE_LOOKUP_QUERY,
        {
            "query_key": query_key,
            "min_timestamp": time.time() - settings.METADATA_CACHE_TTL,
        },
    )
    if row is None:
        return None

    imdb_id = row["imdb_id"]
    media_type = row["media_type"]
    year = row["year"]
    if (
        not isinstance(imdb_id, str)
        or _IMDB_ID.fullmatch(imdb_id) is None
        or media_type not in _CINEMETA_MEDIA_TYPES
        or (year is not None and metadata_year(year) is None)
    ):
        raise ValueError("cached IMDb title match is invalid")
    return ImdbTitleMatch(imdb_id, media_type, year)


async def _store_title_match(query_key: str, match: ImdbTitleMatch) -> None:
    await database.execute(
        _UPSERT_TITLE_LOOKUP_QUERY,
        {
            "query_key": query_key,
            "imdb_id": match.imdb_id,
            "media_type": match.media_type,
            "year": match.year,
            "updated_at": time.time(),
        },
    )


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
    payload: dict,
    media_type: str | None = None,
    year: int | None = None,
) -> ImdbTitleMatch | None:
    if not isinstance(payload.get("d"), list):
        return None

    nearest_match = None
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
        candidate_year = metadata_year(parse_year(element.get("y")))
        match = ImdbTitleMatch(imdb_id, candidate_type, candidate_year)
        if year is None or candidate_year == year:
            return match
        if candidate_year is None or abs(candidate_year - year) > 1:
            continue
        if nearest_match is None:
            nearest_match = match

    return nearest_match


async def resolve_imdb_title(
    session: aiohttp.ClientSession,
    query: str,
    media_type: str | None = None,
    year: int | None = None,
) -> ImdbTitleMatch | None:
    normalized_query = metadata_text(query, maximum=_MAX_TITLE_QUERY_BYTES)
    if (
        normalized_query is None
        or media_type not in (None, *_CINEMETA_MEDIA_TYPES)
        or (year is not None and metadata_year(year) is None)
    ):
        return None

    query_key = _title_lookup_key(normalized_query, media_type, year)
    cached_match = await _cached_title_match(query_key)
    if cached_match is not None:
        return cached_match

    url = _IMDB_SUGGESTION_URL.format(id=quote(normalized_query, safe=""))
    try:
        response = await get_metadata_json(session, url)
    except MetadataHttpError:
        return None
    if not response.successful:
        return None

    match = _extract_title_match(response.payload, media_type, year)
    if match is not None:
        await _store_title_match(query_key, match)
    return match


def _extract_imdb_metadata(
    payload: dict,
    expected_id: str | None = None,
) -> tuple[str | None, int | None, int | None]:
    elements = payload.get("d")
    if not isinstance(elements, list):
        return None, None, None

    for element in elements:
        if not isinstance(element, dict):
            continue
        item_id = element.get("id")
        if (
            not isinstance(item_id, str)
            or _IMDB_ID.fullmatch(item_id) is None
            or (expected_id is not None and item_id != expected_id)
        ):
            continue

        title = metadata_text(element.get("l"))
        if title is None:
            continue

        year = metadata_year(parse_year(element.get("y")))
        _, raw_year_end = parse_year_range(element.get("yr"))
        year_end = metadata_year(raw_year_end)
        if year is not None and year_end is not None and year_end < year:
            year_end = None
        return title, year, year_end

    return None, None, None


def _extract_cinemeta_metadata(
    payload: dict,
) -> tuple[str | None, int | None, int | None]:
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        return None, None, None
    title = metadata_text(meta.get("name"))
    if title is None:
        return None, None, None

    year, year_end = parse_year_range(meta.get("year"))
    if year is None:
        year, year_end = parse_year_range(meta.get("releaseInfo"))

    if year is None:
        year = parse_year(meta.get("released"))
    year = metadata_year(year)
    year_end = metadata_year(year_end)
    if year is not None and year_end is not None and year_end < year:
        year_end = None

    return title, year, year_end


def _iter_cinemeta_media_types(media_type: str | None):
    if media_type in _CINEMETA_MEDIA_TYPES:
        return (media_type,)
    return _CINEMETA_MEDIA_TYPES


async def _get_cinemeta_metadata(
    session: aiohttp.ClientSession, id: str, media_type: str | None
) -> tuple[str | None, int | None, int | None]:
    if not isinstance(id, str) or _IMDB_ID.fullmatch(id) is None:
        return None, None, None
    last_error = None
    for candidate_type in _iter_cinemeta_media_types(media_type):
        url = _CINEMETA_META_URL.format(media_type=candidate_type, id=id)

        try:
            response = await get_metadata_json(session, url)
        except MetadataHttpError as exc:
            last_error = exc
            continue
        if response.status == 404:
            continue
        if not response.successful:
            last_error = MetadataHttpError("metadata service is unavailable")
            continue

        parsed = _extract_cinemeta_metadata(response.payload)
        if parsed[0] is not None:
            return parsed

    if last_error is not None:
        raise last_error
    return None, None, None


async def get_imdb_metadata(
    session: aiohttp.ClientSession, id: str, media_type: str | None = None
):
    if not isinstance(id, str) or _IMDB_ID.fullmatch(id) is None:
        return None, None, None
    try:
        response = await get_metadata_json(
            session,
            _IMDB_SUGGESTION_URL.format(id=id),
        )
    except MetadataHttpError:
        return await _get_cinemeta_metadata(session, id, media_type)
    if response.status == 429:
        return await _get_cinemeta_metadata(session, id, media_type)
    if not response.successful:
        return await _get_cinemeta_metadata(session, id, media_type)

    parsed = _extract_imdb_metadata(response.payload, id)
    if parsed[0] is not None:
        return parsed
    fallback = await _get_cinemeta_metadata(session, id, media_type)
    if fallback[0] is not None:
        return fallback

    return None, None, None
