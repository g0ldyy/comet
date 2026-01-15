import re

from comet.core.logger import log_scraper_error
from comet.scrapers.base import BaseScraper
from comet.scrapers.models import ScrapeRequest
from comet.utils.formatting import size_to_bytes

class PeerflixScraper(BaseScraper):
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
                title = title_full.split("\n")[0]

                seeders = None
                size = 0
                tracker = None

                if "👤" in title_full:
                    matchSeeders = re.search(r"👤\s*(\d+)", title_full)
                    if matchSeeders:
                        seeders = int(matchSeeders.group(1))

                if "💾" in title_full:
                    matchSize = re.search(r"💾\s*([\d.]+\s*[KMGT]B)", title_full)
                    if matchSize:
                        size = size_to_bytes(matchSize.group(1))

                if "🌐" in title_full:
                    matchTracker = re.search(r"🌐\s*([^\n\r]+)", title_full)
                    if matchTracker:
                        tracker = matchTracker.group(1).strip()
                        
                torrents.append(
                    {
                        "title": title,
                        "infoHash": torrent["infoHash"].lower(),
                        "fileIndex": torrent.get("fileIdx", None),
                        "seeders": seeders,
                        "size": size,
                        "tracker": f"Peerflix|{tracker}",
                        "sources": torrent.get("sources", []),
                    }
                )
        except Exception as e:
            log_scraper_error("Peerflix", self.url, request.media_id, e)

        return torrents
