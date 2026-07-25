import re

from comet.core.logger import log_scraper_error
from comet.scrapers.base import BaseScraper
from comet.scrapers.models import ScrapeRequest
from comet.utils.formatting import size_to_bytes

METADATA_PATTERN = re.compile(
    r"(?:📅 S\d+E\d+ )?(?:👤 (\d+) )?💾 ([\d.]+ [KMGT]?B)(?: ⚙️ (.+))?", re.IGNORECASE
)


class TorrentsDBScraper(BaseScraper):
    BASE_URL = "https://torrentsdb.com"

    def __init__(self, manager, session):
        super().__init__(manager, session)

    @staticmethod
    def _parse_stream(torrent):
        if not isinstance(torrent, dict):
            return None

        description = torrent.get("title")
        info_hash = torrent.get("infoHash")
        sources = torrent.get("sources", [])
        if (
            not isinstance(description, str)
            or not description
            or not isinstance(info_hash, str)
            or not info_hash
            or not isinstance(sources, list)
        ):
            return None

        lines = description.split("\n")
        match = METADATA_PATTERN.search(lines[-1])
        seeders = int(match.group(1)) if match and match.group(1) else None
        size = size_to_bytes(match.group(2)) if match and match.group(2) else None
        tracker = match.group(3) if match and match.group(3) else None
        return {
            "title": lines[0],
            "infoHash": info_hash.lower(),
            "fileIndex": torrent.get("fileIdx"),
            "seeders": seeders,
            "size": size,
            "tracker": f"TorrentsDB|{tracker}" if tracker else "TorrentsDB",
            "sources": sources,
        }

    async def scrape(self, request: ScrapeRequest):
        torrents = []
        try:
            async with self.session.get(
                f"{self.BASE_URL}/stream/{request.media_type}/{request.media_id}.json",
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
            log_scraper_error("TorrentsDB", self.BASE_URL, request.media_id, e)

        return torrents
