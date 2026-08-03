from comet.core.provider_json import is_success_status
from comet.discovery.torrent_base import (
    TorrentDiscoveryAdapter,
    deduplicate_torrents,
    gather_concurrently,
)
from comet.discovery.torrent_models import ScrapeRequest


class ZileanScraper(TorrentDiscoveryAdapter):
    url_setting = "ZILEAN_URL"

    def __init__(self, manager, session, url: str):
        super().__init__(manager, session, url)

    @staticmethod
    def _parse_result(result):
        title = result["raw_title"]
        info_hash = result["info_hash"]
        raw_size = result.get("size")
        return {
            "title": title,
            "infoHash": info_hash.lower(),
            "fileIndex": None,
            "seeders": None,
            "size": None if raw_size is None else int(raw_size),
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
                if not is_success_status(response.status):
                    raise RuntimeError(f"HTTP {response.status}")
                data = await response.json()
                return data

        responses = await gather_concurrently(
            fetch(title) for title in request.query_titles
        )
        for data in responses:
            torrents.extend(self._parse_result(result) for result in data)

        return deduplicate_torrents(torrents)
