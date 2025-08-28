import aiohttp

from comet.utils.logger import logger


class CinemataClient:
    BASE_URL = "https://cinemeta-catalogs.strem.io"

    def __init__(self):
        self.session = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def _fetch_catalog_page(
        self, media_type: str, category: str, skip: int = 0, genre: str = None
    ):
        url_parts = [self.BASE_URL]
        url_parts.extend([category, "catalog", media_type, category])
        if genre:
            url = "/".join(url_parts) + f"/genre={genre}&skip={skip}.json"
        else:
            url = "/".join(url_parts) + f"/skip={skip}.json"

        try:
            async with self.session.get(
                url, timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
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
        """
        Fetch all items from a specific category and genre.

        Args:
            media_type: "movie" or "series"
            category: "top", "imdbRating", or "year"
            genre: Optional genre filter
        """
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
        categories = ["top", "imdbRating"]
        genres = [
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
        ]

        seen_ids = set()

        for category in categories:
            for genre in genres:
                async for item in self.fetch_all_from_category(
                    media_type, category, genre
                ):
                    if "imdb_id" not in item:
                        item["imdb_id"] = item["id"]

                    imdb_id = item["imdb_id"]
                    if imdb_id not in seen_ids:
                        if "year" not in item:
                            item["year"] = item["releaseInfo"]

                        seen_ids.add(imdb_id)
                        yield item
