from comet.scrapers.base import (
    BaseScraper,
    deduplicate_torrents,
    gather_with_error_logging,
)
from comet.scrapers.models import ScrapeRequest


class ZileanScraper(BaseScraper):
    def __init__(self, manager, session, url: str):
        super().__init__(manager, session, url)

    @staticmethod
    def _parse_result(result):
        if not isinstance(result, dict):
            return None
        title = result.get("raw_title")
        info_hash = result.get("info_hash")
        if not isinstance(title, str) or not title:
            return None
        if not isinstance(info_hash, str) or not info_hash:
            return None
        try:
            size = int(result["size"])
        except (KeyError, TypeError, ValueError):
            return None
        return {
            "title": title,
            "infoHash": info_hash.lower(),
            "fileIndex": None,
            "seeders": None,
            "size": size,
            "tracker": "DMM",
            "sources": [],
        }

    async def scrape(self, request: ScrapeRequest):
        torrents = []
        series_filters = {}
        if request.media_type == "series":
            if request.season is not None:
                series_filters["season"] = request.season
            if request.episode is not None:
                series_filters["episode"] = request.episode

        async def fetch(title):
            async with self.session.get(
                f"{self.url}/dmm/filtered",
                params={"query": title, **series_filters},
            ) as response:
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status}")
                data = await response.json()
                if not isinstance(data, list):
                    raise ValueError("response payload is not a list")
                return data

        responses = await gather_with_error_logging(
            (f"Zilean query {title!r} ({self.url})", fetch(title))
            for title in request.query_titles
        )
        for data in responses:
            for result in data:
                parsed = self._parse_result(result)
                if parsed is not None:
                    torrents.append(parsed)

        return deduplicate_torrents(torrents)
