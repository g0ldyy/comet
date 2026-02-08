import aiohttp

from comet.core.constants import CATALOG_TIMEOUT
from comet.core.logger import logger


class CinemataClient:
    CATALOG_BASE_URL = "https://cinemeta-catalogs.strem.io"
    META_BASE_URL = "https://v3-cinemeta.strem.io"
    CATALOG_CATEGORIES = ("top", "imdbRating")
    CATALOG_GENRES = (
        None,
        "Action",
        "Adventure",
        "Animation",
        "Biography",
        "Comedy",
        "Crime",
        "Documentary",
        "Drama",
        "Family",
        "Fantasy",
        "History",
        "Horror",
        "Mystery",
        "Romance",
        "Sci-Fi",
        "Sport",
        "Thriller",
        "War",
        "Western",
    )

    def __init__(self, session: aiohttp.ClientSession | None = None):
        self.session = session
        self._owns_session = session is None

    async def __aenter__(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
            self._owns_session = True
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._owns_session and self.session and not self.session.closed:
            await self.session.close()

    async def _fetch_catalog_page(
        self, media_type: str, category: str, skip: int = 0, genre: str = None
    ):
        url_parts = [self.CATALOG_BASE_URL]
        url_parts.extend([category, "catalog", media_type, category])
        if genre:
            url = "/".join(url_parts) + f"/genre={genre}&skip={skip}.json"
        else:
            url = "/".join(url_parts) + f"/skip={skip}.json"

        try:
            async with self.session.get(url, timeout=CATALOG_TIMEOUT) as response:
                response.raise_for_status()
                data = await response.json()
                return data
        except Exception as e:
            logger.error(f"Error fetching Cinemata catalog page: {url} - {e}")
            raise

    async def fetch_all_from_category(
        self,
        media_type: str,
        category: str,
        genre: str = None,
    ):
        skip = 0

        while True:
            try:
                data = await self._fetch_catalog_page(media_type, category, skip, genre)
                metas = data.get("metas", [])

                if not metas:
                    break

                for meta in metas:
                    yield meta

                if not data["hasMore"]:
                    break

                skip += len(metas)

            except Exception as e:
                logger.error(f"Error in fetch_all_from_category: {e}")
                break

    async def fetch_all_of_type(self, media_type: str):
        seen_ids = set()

        for category in self.CATALOG_CATEGORIES:
            for genre in self.CATALOG_GENRES:
                async for item in self.fetch_all_from_category(
                    media_type, category, genre
                ):
                    imdb_id = item.get("imdb_id") or item.get("id")
                    if not imdb_id or imdb_id in seen_ids:
                        continue

                    if not (item.get("year") or item.get("releaseInfo")):
                        continue

                    seen_ids.add(imdb_id)
                    yield item

    async def fetch_series_episodes(self, series_id: str) -> list[dict]:
        episodes = []
        url = f"{self.META_BASE_URL}/meta/series/{series_id}.json"
        try:
            async with self.session.get(url, timeout=CATALOG_TIMEOUT) as response:
                if response.status == 404:
                    logger.warning(f"No Cinemata metadata found for series {series_id}")
                    return episodes
                response.raise_for_status()
                data = await response.json()
        except Exception as e:
            logger.error(
                f"Error fetching Cinemata series metadata for {series_id}: {e}"
            )
            return episodes

        videos = data.get("meta", {}).get("videos", [])
        seen = set()
        for video in videos:
            season = video.get("season")
            episode = video.get("episode", video.get("number"))
            if season is None or episode is None:
                continue

            try:
                season_int = int(season)
                episode_int = int(episode)
            except (TypeError, ValueError):
                continue

            key = (season_int, episode_int)
            if key in seen:
                continue
            seen.add(key)
            episodes.append({"season": season_int, "episode": episode_int})

        episodes.sort(key=lambda entry: (entry["season"], entry["episode"]))
        return episodes
