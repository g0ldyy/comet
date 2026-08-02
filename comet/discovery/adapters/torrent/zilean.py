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
        if not isinstance(result, dict):
            raise ValueError("Zilean result is invalid")
        title = result.get("raw_title")
        info_hash = result.get("info_hash")
        if not isinstance(title, str) or not title:
            raise ValueError("Zilean result is invalid")
        if not isinstance(info_hash, str) or not info_hash:
            raise ValueError("Zilean result is invalid")
        raw_size = result.get("size")
        try:
            size = None if raw_size is None else int(raw_size)
        except (TypeError, ValueError) as exc:
            raise ValueError("Zilean result is invalid") from exc
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
                if not is_success_status(response.status):
                    raise RuntimeError(f"HTTP {response.status}")
                data = await response.json()
                if not isinstance(data, list):
                    raise ValueError("response payload is not a list")
                return data

        responses = await gather_concurrently(
            fetch(title) for title in request.query_titles
        )
        for data in responses:
            torrents.extend(self._parse_result(result) for result in data)

        return deduplicate_torrents(torrents)
