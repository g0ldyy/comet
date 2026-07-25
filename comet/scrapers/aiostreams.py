from comet.core.logger import log_scraper_error
from comet.scrapers.base import BaseScraper
from comet.scrapers.helpers.aiostreams import aiostreams_config
from comet.scrapers.models import ScrapeRequest


class AiostreamsScraper(BaseScraper):
    def __init__(
        self,
        manager,
        session,
        url: str,
        credentials: str | None = None,
    ):
        super().__init__(manager, session, url)
        self.credentials = credentials

    @staticmethod
    def _parse_stream(torrent):
        if not isinstance(torrent, dict):
            return None

        title = torrent.get("filename")
        info_hash = torrent.get("infoHash")
        sources = torrent.get("sources", [])
        if (
            not isinstance(title, str)
            or not title
            or not isinstance(info_hash, str)
            or not info_hash
            or "size" not in torrent
            or not isinstance(sources, list)
        ):
            return None

        tracker = "AIOStreams"
        indexer = torrent.get("indexer")
        if isinstance(indexer, str) and indexer:
            tracker += f"|{indexer}"

        return {
            "title": title,
            "infoHash": info_hash,
            "fileIndex": torrent.get("fileIdx"),
            "seeders": torrent.get("seeders"),
            "size": torrent["size"],
            "tracker": tracker,
            "sources": sources,
        }

    async def scrape(self, request: ScrapeRequest):
        torrents = []
        try:
            headers = aiostreams_config.get_headers_for_credential(self.credentials)

            params = {
                "type": request.media_type,
                "id": request.media_id,
            }

            async with self.session.get(
                f"{self.url}/api/v1/search",
                params=params,
                headers=headers,
            ) as response:
                results = await response.json()

            if not isinstance(results, dict) or not isinstance(
                results.get("data"), dict
            ):
                return []
            streams = results["data"].get("results")
            if not isinstance(streams, list):
                return []

            for torrent in streams:
                parsed = self._parse_stream(torrent)
                if parsed is not None:
                    torrents.append(parsed)
        except Exception as e:
            log_scraper_error("AIOStreams", self.url, request.media_id, e)

        return torrents
