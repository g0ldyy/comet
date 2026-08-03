from datetime import date

import aiohttp

from comet.core.models import settings
from comet.metadata.http import MetadataHttpError, get_metadata_json
from comet.metadata.validation import metadata_text, normalize_aliases
from comet.utils.languages import merge_aliases

DEFAULT_TMDB_READ_ACCESS_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJlNTkxMmVmOWFhM2IxNzg2Zjk3ZTE1NWY1YmQ3ZjY1MSIsInN1YiI6IjY1M2NjNWUyZTg5NGE2MDBmZjE2N2FmYyIsInNjb3BlcyI6WyJhcGlfcmVhZCJdLCJ2ZXJzaW9uIjoxfQ.xrIXsMFJpI1o1j5g2QpQcFP1X3AfRjFA5FlBFO5Naw8"
_MEDIA_CONFIG = {
    "movie": {
        "path": "movie",
        "find_results": "movie_results",
        "alias_results": "titles",
        "title": "title",
        "original_title": "original_title",
    },
    "series": {
        "path": "tv",
        "find_results": "tv_results",
        "alias_results": "results",
        "title": "name",
        "original_title": "original_name",
    },
}


def _extract_upcoming_release_date(payload) -> str | None:
    release_dates = []
    for result in payload.get("results") or []:
        if result is None:
            continue
        for release in result.get("release_dates") or []:
            if release is None:
                continue
            if release.get("type") not in (4, 5):
                continue
            raw_date = release.get("release_date")
            if not raw_date:
                continue
            date_text = raw_date.split("T", 1)[0]
            try:
                date.fromisoformat(date_text)
            except ValueError:
                continue
            release_dates.append(date_text)

    return min(release_dates) if release_dates else None


def _extract_tmdb_id(payload, media_type: str | None = None) -> str | None:
    if payload is None:
        return None
    if media_type is None:
        result_keys = ("movie_results", "tv_results")
    else:
        config = _MEDIA_CONFIG.get(media_type)
        if config is None:
            return None
        result_keys = (config["find_results"],)

    for result_key in result_keys:
        for result in payload.get(result_key) or []:
            if result is not None and result.get("id") is not None:
                return str(result["id"])
    return None


def _extract_title_aliases(payload, result_key: str) -> dict[str, list[str]]:
    aliases: dict[str, list[str]] = {}
    for entry in payload.get(result_key) or []:
        if entry is None:
            continue
        title = metadata_text(entry.get("title"))
        if title is None:
            continue

        raw_country = entry.get("iso_3166_1")
        country = (
            raw_country.lower()
            if raw_country
            and len(raw_country) == 2
            and raw_country.isascii()
            and raw_country.isalpha()
            else "ez"
        )
        aliases.setdefault(country, []).append(title)

    return aliases


def _extract_original_title(payload: dict, title_key: str) -> dict[str, list[str]]:
    title = metadata_text(payload.get(title_key))
    if title is None:
        return {}

    language = payload.get("original_language")
    if (
        language
        and len(normalized_language := language.lower()) == 2
        and normalized_language.isascii()
        and normalized_language.isalpha()
    ):
        return {f"original:{normalized_language}": [title]}
    return {"original": [title]}


def _extract_translated_titles(payload: object, title_key: str) -> dict[str, list[str]]:
    aliases: dict[str, list[str]] = {}
    for entry in payload.get("translations") or []:
        if entry is None or entry.get("data") is None:
            continue
        language = entry.get("iso_639_1")
        title = metadata_text(entry["data"].get(title_key))
        if title is None:
            continue
        scope = (
            f"lang:{normalized_language}"
            if language
            and len(normalized_language := language.lower()) == 2
            and normalized_language.isascii()
            and normalized_language.isalpha()
            else "ez"
        )
        aliases.setdefault(scope, []).append(title)
    return aliases


def _extract_all_title_aliases(payload: dict, config: dict) -> dict[str, list[str]]:
    return normalize_aliases(
        merge_aliases(
            _extract_original_title(payload, config["original_title"]),
            _extract_translated_titles(payload.get("translations"), config["title"]),
            _extract_title_aliases(
                payload.get("alternative_titles"), config["alias_results"]
            ),
        )
    )


class TMDBApi:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.base_url = "https://api.themoviedb.org/3"
        self.headers = {
            "Authorization": f"Bearer {settings.TMDB_READ_ACCESS_TOKEN if settings.TMDB_READ_ACCESS_TOKEN else DEFAULT_TMDB_READ_ACCESS_TOKEN}",
            "Accept": "application/json",
        }

    async def _get_json(self, path: str):
        try:
            response = await get_metadata_json(
                self.session,
                f"{self.base_url}/{path}",
                headers=self.headers,
            )
        except MetadataHttpError:
            return None
        if not response.successful:
            return None
        return response.payload

    async def get_upcoming_movie_release_date(self, tmdb_id: str):
        data = await self._get_json(f"movie/{tmdb_id}/release_dates")
        return None if data is None else _extract_upcoming_release_date(data)

    async def get_episode_air_date(self, tmdb_id: str, season: int, episode: int):
        data = await self._get_json(f"tv/{tmdb_id}/season/{season}/episode/{episode}")
        if data is None:
            return None
        return data.get("air_date")

    async def _find_from_imdb(self, imdb_id: str):
        return await self._get_json(f"find/{imdb_id}?external_source=imdb_id")

    async def get_tmdb_id_from_imdb(self, imdb_id: str, media_type: str | None = None):
        data = await self._find_from_imdb(imdb_id)
        return _extract_tmdb_id(data, media_type)

    async def get_media_type_from_imdb(self, imdb_id: str) -> str | None:
        data = await self._find_from_imdb(imdb_id)
        if data is None:
            return None
        if _extract_tmdb_id(data, "movie") is not None:
            return "movie"
        if _extract_tmdb_id(data, "series") is not None:
            return "series"
        return None

    async def get_title_aliases(self, media_type: str, imdb_id: str):
        config = _MEDIA_CONFIG.get(media_type)
        if config is None:
            return None

        tmdb_id = await self.get_tmdb_id_from_imdb(imdb_id, media_type)
        if tmdb_id is None:
            return None

        data = await self._get_json(
            f"{config['path']}/{tmdb_id}"
            "?append_to_response=alternative_titles,translations"
        )
        if data is None:
            return None
        return _extract_all_title_aliases(data, config)

    async def has_watch_providers(self, tmdb_id: str):
        data = await self._get_json(f"movie/{tmdb_id}/watch/providers")
        if data is None:
            return None
        return bool(data.get("results"))
