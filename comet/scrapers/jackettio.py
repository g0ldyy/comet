import re

from comet.core.logger import log_scraper_error
from comet.scrapers.base import BaseScraper
from comet.scrapers.models import ScrapeRequest
from comet.utils.formatting import size_to_bytes

data_pattern = re.compile(
    r"💾 ([\d.]+ [KMGT]B)\s+👥 (\d+)\s+⚙️ (\w+)",
)


class JackettioScraper(BaseScraper):
    def __init__(self, manager, session, url: str):
        super().__init__(manager, session, url)

    @staticmethod
    def _parse_stream(torrent):
        if not isinstance(torrent, dict):
            return None

        title_full = torrent.get("title")
        info_hash = torrent.get("infoHash")
        if (
            not isinstance(title_full, str)
            or not title_full
            or not isinstance(info_hash, str)
            or not info_hash
        ):
            return None

        match = data_pattern.search(title_full)
        size = size_to_bytes(match.group(1)) if match else None
        seeders = int(match.group(2)) if match else None
        tracker = match.group(3) if match else "Jackettio"
        return {
            "title": title_full.split("\n")[0],
            "infoHash": info_hash,
            "fileIndex": None,
            "seeders": seeders,
            "size": size,
            "tracker": f"Jackettio|{tracker}",
            "sources": [],
        }

    async def scrape(self, request: ScrapeRequest):
        torrents = []
        try:
            async with self.session.get(
                f"{self.url}/stream/{request.media_type}/{request.media_id}.json",
            ) as response:
                results = await response.json()

            if not isinstance(results, dict) or not isinstance(
                results.get("streams"), list
            ):
                return []

            for torrent in results["streams"]:
                parsed = self._parse_stream(torrent)
                if parsed is not None:
                    torrents.append(parsed)
        except Exception as e:
            log_scraper_error("Jackettio", self.url, request.media_id, e)

        return torrents
