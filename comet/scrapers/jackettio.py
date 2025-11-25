import re

import aiohttp

from comet.core.logger import log_scraper_error
from comet.scrapers.base import BaseScraper
from comet.scrapers.models import ScrapeRequest
from comet.utils.formatting import size_to_bytes
from comet.utils.network import fetch_with_proxy_fallback

data_pattern = re.compile(
    r"💾 ([\d.]+ [KMGT]B)\s+👥 (\d+)\s+⚙️ (\w+)",
)


class JackettioScraper(BaseScraper):
    def __init__(self, manager, session: aiohttp.ClientSession, url: str):
        super().__init__(manager, session, url)

    async def scrape(self, request: ScrapeRequest):
        torrents = []
        try:
            results = await fetch_with_proxy_fallback(
                self.session,
                f"{self.url}/stream/{request.media_type}/{request.media_id}.json",
            )

            for torrent in results["streams"]:
                title_full = torrent["title"]

                title = title_full.split("\n")[0]

                match = data_pattern.search(title_full)

                size = size_to_bytes(match.group(1)) if match else 0
                seeders = int(match.group(2)) if match else 0
                tracker = match.group(3) if match else "Jackettio"

                torrents.append(
                    torrents.append(
                        {
                            "title": title,
                            "infoHash": torrent["infoHash"],
                            "fileIndex": None,
                            "seeders": seeders,
                            "size": size,
                            "tracker": f"Jackettio|{tracker}",
                            "sources": None,
                        }
                    )
                )
        except Exception as e:
            log_scraper_error("Jackettio", self.url, request.media_id, e)

        return torrents
