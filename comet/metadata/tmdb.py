from datetime import date

import aiohttp

from comet.core.logger import logger
from comet.core.models import settings
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
    if not isinstance(payload, dict):
        return None

    release_dates = []
    results = payload.get("results")
    if not isinstance(results, list):
        return None
    for result in results:
        if not isinstance(result, dict):
            continue
        releases = result.get("release_dates")
        if not isinstance(releases, list):
            continue
        for release in releases:
            if not isinstance(release, dict) or release.get("type") not in (4, 5):
                continue
            raw_date = release.get("release_date")
            if not isinstance(raw_date, str):
                continue
            date_text = raw_date.split("T", 1)[0]
            try:
                date.fromisoformat(date_text)
            except ValueError:
                continue
            release_dates.append(date_text)

    return min(release_dates) if release_dates else None


def _extract_tmdb_id(payload, media_type: str | None = None) -> str | None:
    if not isinstance(payload, dict):
        return None

    if media_type is None:
        result_keys = ("movie_results", "tv_results")
    else:
        config = _MEDIA_CONFIG.get(media_type)
        if config is None:
            return None
        result_keys = (config["find_results"],)

    for result_key in result_keys:
        results = payload.get(result_key)
        if not isinstance(results, list):
            continue
        for result in results:
            if not isinstance(result, dict):
                continue
            result_id = result.get("id")
            if isinstance(result_id, bool) or not isinstance(result_id, int):
                continue
            if result_id > 0:
                return str(result_id)
    return None


def _extract_title_aliases(payload, result_key: str) -> dict[str, list[str]]:
    if not isinstance(payload, dict):
        return {}

    entries = payload.get(result_key)
    if not isinstance(entries, list):
        return {}

    aliases: dict[str, list[str]] = {}
    seen: dict[str, set[str]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue

        raw_title = entry.get("title")
        if not isinstance(raw_title, str) or not (title := raw_title.strip()):
            continue

        raw_country = entry.get("iso_3166_1")
        country = (
            raw_country.lower()
            if isinstance(raw_country, str)
            and len(raw_country) == 2
            and raw_country.isascii()
            and raw_country.isalpha()
            else "ez"
        )
        country_seen = seen.setdefault(country, set())
        if title in country_seen:
            continue
        country_seen.add(title)
        aliases.setdefault(country, []).append(title)

    return aliases


def _extract_original_title(payload: object, title_key: str) -> dict[str, list[str]]:
    if not isinstance(payload, dict):
        return {}

    raw_title = payload.get(title_key)
    if not isinstance(raw_title, str) or not (title := raw_title.strip()):
        return {}

    language = payload.get("original_language")
    if (
        isinstance(language, str)
        and len(normalized_language := language.lower()) == 2
        and normalized_language.isascii()
        and normalized_language.isalpha()
    ):
        return {f"original:{normalized_language}": [title]}
    return {"original": [title]}


def _extract_translated_titles(payload: object, title_key: str) -> dict[str, list[str]]:
    if not isinstance(payload, dict):
        return {}
    entries = payload.get("translations")
    if not isinstance(entries, list):
        return {}

    aliases: dict[str, list[str]] = {}
    seen: dict[str, set[str]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("data"), dict):
            continue
        language = entry.get("iso_639_1")
        raw_title = entry["data"].get(title_key)
        if (
            not isinstance(language, str)
            or len(language := language.lower()) != 2
            or not language.isascii()
            or not language.isalpha()
            or not isinstance(raw_title, str)
            or not (title := raw_title.strip())
        ):
            continue
        language_seen = seen.setdefault(language, set())
        if title not in language_seen:
            language_seen.add(title)
            aliases.setdefault(f"lang:{language}", []).append(title)
    return aliases


def _extract_all_title_aliases(payload: object, config: dict) -> dict[str, list[str]]:
    if not isinstance(payload, dict):
        return {}

    return merge_aliases(
        _extract_original_title(payload, config["original_title"]),
        _extract_translated_titles(payload.get("translations"), config["title"]),
        _extract_title_aliases(
            payload.get("alternative_titles"), config["alias_results"]
        ),
    )


class TMDBApi:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.base_url = "https://api.themoviedb.org/3"
        self.headers = {
            "Authorization": f"Bearer {settings.TMDB_READ_ACCESS_TOKEN if settings.TMDB_READ_ACCESS_TOKEN else DEFAULT_TMDB_READ_ACCESS_TOKEN}",
            "Accept": "application/json",
        }

    async def _get_json(self, path: str, context: str):
        try:
            async with self.session.get(
                f"{self.base_url}/{path}", headers=self.headers
            ) as response:
                if response.status != 200:
                    logger.warning(
                        f"TMDB: {context} failed with HTTP {response.status}"
                    )
                    return None
                return await response.json()
        except Exception as exc:
            logger.error(f"TMDB: {context} failed: {exc}")
            return None

    async def get_upcoming_movie_release_date(self, tmdb_id: str):
        data = await self._get_json(
            f"movie/{tmdb_id}/release_dates",
            f"movie release dates lookup for {tmdb_id}",
        )
        return _extract_upcoming_release_date(data)

    async def get_episode_air_date(self, tmdb_id: str, season: int, episode: int):
        data = await self._get_json(
            f"tv/{tmdb_id}/season/{season}/episode/{episode}",
            f"episode lookup for {tmdb_id} S{season}E{episode}",
        )
        if not isinstance(data, dict):
            return None
        air_date = data.get("air_date")
        return air_date if isinstance(air_date, str) else None

    async def _find_from_imdb(self, imdb_id: str):
        return await self._get_json(
            f"find/{imdb_id}?external_source=imdb_id",
            f"IMDb ID lookup for {imdb_id}",
        )

    async def get_tmdb_id_from_imdb(self, imdb_id: str, media_type: str | None = None):
        data = await self._find_from_imdb(imdb_id)
        return _extract_tmdb_id(data, media_type)

    async def get_media_type_from_imdb(self, imdb_id: str) -> str | None:
        data = await self._find_from_imdb(imdb_id)
        if not isinstance(data, dict):
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
            "?append_to_response=alternative_titles,translations",
            f"title aliases lookup for {imdb_id}",
        )
        if data is None:
            return None
        if not isinstance(data, dict):
            logger.warning(f"TMDB: invalid title aliases response for {imdb_id}")
            return None
        return _extract_all_title_aliases(data, config)

    async def has_watch_providers(self, tmdb_id: str):
        data = await self._get_json(
            f"movie/{tmdb_id}/watch/providers",
            f"watch providers lookup for {tmdb_id}",
        )
        if not isinstance(data, dict):
            return None
        return bool(data.get("results"))
