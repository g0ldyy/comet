import aiohttp

from comet.core.constants import catalog_timeout
from comet.core.provider_json import read_provider_json
from comet.metadata.validation import episode_coordinate


def _extract_catalog_page(payload: dict) -> tuple[list[dict], bool, int]:
    metas = payload.get("metas")
    if type(metas) is not list:
        raise ValueError("Cinemeta catalog response must contain a metadata list")

    return (
        [meta for meta in metas if type(meta) is dict],
        payload.get("hasMore") is not False,
        len(metas),
    )


def _extract_series_episodes(payload: dict) -> list[dict]:
    if type(payload.get("meta")) is not dict:
        raise ValueError("Cinemeta series response must contain a metadata object")

    videos = payload["meta"].get("videos")
    if type(videos) is not list:
        raise ValueError("Cinemeta series videos must be a list")

    episodes = []
    seen = set()
    for video in videos:
        if type(video) is not dict:
            continue
        season = episode_coordinate(video.get("season"))
        episode = episode_coordinate(video.get("episode", video.get("number")))
        if season is None or season < 1 or episode is None or episode < 1:
            continue

        key = (season, episode)
        if key in seen:
            continue
        seen.add(key)
        episodes.append({"season": season, "episode": episode})

    episodes.sort(key=lambda entry: (entry["season"], entry["episode"]))
    return episodes


class CinemetaClient:
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
        self, media_type: str, category: str, skip: int = 0, genre: str | None = None
    ):
        url_parts = [self.CATALOG_BASE_URL]
        url_parts.extend([category, "catalog", media_type, category])
        if genre:
            url = "/".join(url_parts) + f"/genre={genre}&skip={skip}.json"
        else:
            url = "/".join(url_parts) + f"/skip={skip}.json"

        async with self.session.get(
            url,
            timeout=catalog_timeout(),
            allow_redirects=False,
        ) as response:
            response.raise_for_status()
            return await read_provider_json(response)

    async def fetch_all_from_category(
        self,
        media_type: str,
        category: str,
        genre: str | None = None,
    ):
        skip = 0

        while True:
            data = await self._fetch_catalog_page(media_type, category, skip, genre)
            metas, has_more, page_size = _extract_catalog_page(data)

            if page_size == 0:
                break

            for meta in metas:
                yield meta

            if not has_more:
                break

            skip += page_size

    async def fetch_all_of_type(self, media_type: str):
        for category in self.CATALOG_CATEGORIES:
            for genre in self.CATALOG_GENRES:
                async for item in self.fetch_all_from_category(
                    media_type, category, genre
                ):
                    yield item

    async def fetch_series_episodes(self, series_id: str) -> list[dict]:
        url = f"{self.META_BASE_URL}/meta/series/{series_id}.json"
        async with self.session.get(
            url,
            timeout=catalog_timeout(),
            allow_redirects=False,
        ) as response:
            if response.status == 404:
                return []
            response.raise_for_status()
            data = await read_provider_json(response)
        return _extract_series_episodes(data)
