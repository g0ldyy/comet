import re

from comet.core.logger import log_scraper_error
from comet.scrapers.base import BaseScraper
from comet.scrapers.models import ScrapeRequest
from comet.utils.formatting import size_to_bytes

DATA_PATTERN = re.compile(
    r"(?:👤 (\d+) )?💾 ([\d.]+ [KMGT]B)(?: ⚙️ (\w+))?", re.IGNORECASE
)


class TorrentioScraper(BaseScraper):
    impersonate = "chrome"

    def __init__(self, manager, session, url: str):
        super().__init__(manager, session, url)

    async def scrape(self, request: ScrapeRequest):
        torrents = []
        try:
            async with self.session.get(
                f"{self.url}/stream/{request.media_type}/{request.media_id}.json",
            ) as response:
                results = await response.json()

            if not results or "streams" not in results:
                return []

            for torrent in results["streams"]:
                title_full = torrent["title"]

                if "\n💾" in title_full:
                    title = title_full.split("\n💾")[0].split("\n")[-1]
                else:
                    title = title_full.split("\n")[0]

                match = DATA_PATTERN.search(title_full)

                seeders = int(match.group(1)) if match and match.group(1) else None
                size = size_to_bytes(match.group(2)) if match and match.group(2) else 0
                tracker = (
                    match.group(3) if match and match.group(3) else "KnightCrawler"
                )

                torrents.append(
                    {
                        "title": title,
                        "infoHash": torrent["infoHash"].lower(),
                        "fileIndex": torrent.get("fileIdx", None),
                        "seeders": seeders,
                        "size": size,
                        "tracker": f"Torrentio|{tracker}",
                        "sources": torrent.get("sources", []),
                    }
                )
        except Exception as e:
            log_scraper_error("Torrentio", self.url, request.media_id, e)

        return torrents
