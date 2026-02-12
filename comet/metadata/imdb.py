import aiohttp

from comet.core.logger import logger
from comet.utils.year import parse_year, parse_year_range

_IMDB_SUGGESTION_URL = "https://v3.sg.media-imdb.com/suggestion/a/{id}.json"
_CINEMETA_META_URL = "https://v3-cinemeta.strem.io/meta/{media_type}/{id}.json"
_CINEMETA_MEDIA_TYPES = ("movie", "series")


def _extract_imdb_metadata(payload: dict) -> tuple[str | None, int | None, int | None]:
    if not isinstance(payload, dict):
        return None, None, None

    for element in payload.get("d") or []:
        item_id = element.get("id", "")
        if "/" in item_id:
            continue

        title = element.get("l")
        if not title:
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

    meta = payload.get("meta") or {}
    title = meta.get("name")
    if not title:
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
