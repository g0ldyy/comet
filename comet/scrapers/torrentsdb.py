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

    async def scrape(self, request: ScrapeRequest):
        torrents = []
        try:
            async with self.session.get(
                f"{self.BASE_URL}/stream/{request.media_type}/{request.media_id}.json",
            ) as response:
                results = await response.json()

            for torrent in results["streams"]:
                description = torrent["title"]

                lines = description.split("\n")
                title = lines[0]
                metadata_line = lines[-1]

                match = METADATA_PATTERN.search(metadata_line)

                seeders = int(match.group(1)) if match and match.group(1) else None
                size = (
                    size_to_bytes(match.group(2)) if match and match.group(2) else None
                )
                tracker = match.group(3) if match and match.group(3) else None

                torrents.append(
                    {
                        "title": title,
                        "infoHash": torrent["infoHash"].lower(),
                        "fileIndex": torrent.get("fileIdx", None),
                        "seeders": seeders,
                        "size": size,
                        "tracker": f"TorrentsDB|{tracker}" if tracker else "TorrentsDB",
                        "sources": torrent.get("sources", []),
                    }
                )
        except Exception as e:
            log_scraper_error("TorrentsDB", self.BASE_URL, request.media_id, e)

        return torrents
