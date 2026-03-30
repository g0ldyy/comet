import aiohttp

from comet.core.logger import logger
from comet.core.models import settings

DEFAULT_TMDB_READ_ACCESS_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJlNTkxMmVmOWFhM2IxNzg2Zjk3ZTE1NWY1YmQ3ZjY1MSIsInN1YiI6IjY1M2NjNWUyZTg5NGE2MDBmZjE2N2FmYyIsInNjb3BlcyI6WyJhcGlfcmVhZCJdLCJ2ZXJzaW9uIjoxfQ.xrIXsMFJpI1o1j5g2QpQcFP1X3AfRjFA5FlBFO5Naw8"


class TMDBApi:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.base_url = "https://api.themoviedb.org/3"
        self.headers = {
            "Authorization": f"Bearer {settings.TMDB_READ_ACCESS_TOKEN if settings.TMDB_READ_ACCESS_TOKEN else DEFAULT_TMDB_READ_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        }

    async def get_upcoming_movie_release_date(self, tmdb_id: str):
        try:
            url = f"{self.base_url}/movie/{tmdb_id}/release_dates"
            async with self.session.get(url, headers=self.headers) as response:
                if response.status != 200:
                    return None

                data = await response.json()

            release_dates = []
            for result in data.get("results", []):
                for release in result.get("release_dates", []):
                    # Type 4 = Digital, Type 5 = Physical
                    if release.get("type") in (4, 5):
                        date_str = release.get("release_date", "").split("T")[0]
                        if date_str:
                            release_dates.append(date_str)

            return min(release_dates) if release_dates else None
        except Exception as e:
            logger.error(f"TMDB: Error getting movie release date for {tmdb_id}: {e}")
            return None

    async def get_episode_air_date(self, tmdb_id: str, season: int, episode: int):
        try:
            url = f"{self.base_url}/tv/{tmdb_id}/season/{season}/episode/{episode}"
            async with self.session.get(url, headers=self.headers) as response:
                if response.status != 200:
                    return None

                data = await response.json()
                return data.get("air_date")
        except Exception as e:
            logger.error(
                f"TMDB: Error getting episode air date for {tmdb_id} S{season}E{episode}: {e}"
            )
            return None

    async def get_tmdb_id_from_imdb(self, imdb_id: str):
        try:
            url = f"{self.base_url}/find/{imdb_id}?external_source=imdb_id"
            async with self.session.get(url, headers=self.headers) as response:
                if response.status != 200:
                    text = await response.text()
                    logger.error(
                        f"TMDB: Failed to get TMDB ID from IMDB ID {imdb_id}: {text}"
                    )
                    return None

                data = await response.json()

            movie_results = data.get("movie_results")
            if movie_results:
                return str(movie_results[0]["id"])

            tv_results = data.get("tv_results")
            if tv_results:
                return str(tv_results[0]["id"])

            return None
        except Exception as e:
            logger.error(f"TMDB: Error converting IMDB ID {imdb_id}: {e}")
            return None

    async def has_watch_providers(self, tmdb_id: str):
        try:
            url = f"{self.base_url}/movie/{tmdb_id}/watch/providers"
            async with self.session.get(url, headers=self.headers) as response:
                if response.status != 200:
                    return None

                data = await response.json()
                return bool(data.get("results"))
        except Exception as e:
            logger.error(f"TMDB: Error getting watch providers for {tmdb_id}: {e}")
            return None

    async def get_translations_by_tmdb_id(self, tmdb_id: str, media_type: str = "movie"):
        """
        Fetch translations for names/titles in all languages and regions from TMDB.
        :param tmdb_id: The TMDB ID of the movie or TV show.
        :param media_type: Either 'movie' or 'tv'.
        :return: Dict of language or language-region codes to titles.
        """
        try:
            tmdb_media_type = "tv" if media_type == "series" else media_type

            if tmdb_media_type not in ("movie", "tv"):
                raise ValueError("media_type must be 'movie' or 'tv'")
            translations_url = f"{self.base_url}/{tmdb_media_type}/{tmdb_id}/translations"
            async with self.session.get(translations_url, headers=self.headers) as response:
                if response.status != 200:
                    text = await response.text()
                    logger.error(
                        f"TMDB: Failed to get translations for {media_type} {tmdb_id}: {text}"
                    )
                    return {}

                translations_data = await response.json()
                translations = translations_data.get("translations", [])
                # Build a dict of language_code and language-region_code: title/name
                names_by_language = {}
                for t in translations:
                    lang = t.get("iso_639_1")
                    region = t.get("iso_3166_1")
                    data = t.get("data", {})
                    title = data.get("title") or data.get("name")
                    if lang and title:
                        # Store by language code (e.g., 'es')
                        names_by_language[lang] = title
                    if lang and region and title:
                        # Store by language-region code (e.g., 'es-MX')
                        names_by_language[f"{lang}-{region}"] = title
                return names_by_language
        except Exception as e:
            logger.error(f"TMDB: Error getting translations for {media_type} {tmdb_id}: {e}")
            return {}

    async def get_metadata_by_tmdb_id(self, tmdb_id: str, media_type: str = "movie"):
        """
        Fetch full metadata from TMDB by tmdb_id
        :param tmdb_id: The TMDB ID of the movie or TV show.
        :param media_type: Either 'movie' or 'tv'.
        :return: Metadata dict or None if not found.
        """
        try:
            tmdb_media_type = "tv" if media_type == "series" else media_type

            if tmdb_media_type not in ("movie", "tv"):
                raise ValueError("media_type must be 'movie' or 'tv'")
            url = f"{self.base_url}/{tmdb_media_type}/{tmdb_id}"
            async with self.session.get(url, headers=self.headers) as response:
                if response.status != 200:
                    text = await response.text()
                    logger.error(
                        f"TMDB: Failed to get metadata for {media_type} {tmdb_id}: {text}"
                    )
                    return None
                metadata = await response.json()
          
            return metadata
        except Exception as e:
            logger.error(f"TMDB: Error getting metadata for {media_type} {tmdb_id}: {e}")
            return None

    async def get_tmdb_aliases(self, imdb_id: str, media_type: str = "movie"):
        """
        Build an aliases dictionary.
        :param imdb_id: The IMDb ID of the movie or TV show.
        :param media_type: Either 'movie' or 'tv'.
        :return: Aliases dict or None if not found.
        """
        tmdb_id = await self.get_tmdb_id_from_imdb(imdb_id)
        if not tmdb_id:
            logger.error(f"TMDB: Could not resolve TMDB ID for IMDb ID {imdb_id}")
            return None

        titles_by_language = await self.get_translations_by_tmdb_id(tmdb_id, media_type)
        if not titles_by_language:
            logger.error(f"TMDB: Could not fetch translations for TMDB ID {tmdb_id}")
            return None

        aliases = {}

        # Add all language/country titles
        for lang, title in titles_by_language.items():
            if title:
                aliases.setdefault(lang, []).append(title)

        # Add an "ez" key for all titles
        aliases["ez"] = [
            t for titles in aliases.values() if isinstance(titles, list) for t in titles
        ]

        return aliases
