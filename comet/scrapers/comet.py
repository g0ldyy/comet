import aiohttp

from comet.core.logger import log_scraper_error
from comet.scrapers.base import BaseScraper
from comet.scrapers.models import ScrapeRequest
from comet.utils.network import fetch_with_proxy_fallback


class CometScraper(BaseScraper):
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
                title_full = torrent["description"]
                title = title_full.split("\n")[0].split("📄 ")[1]

                seeders = (
                    int(title_full.split("👤 ")[1].split(" ")[0])
                    if "👤" in title_full
                    else None
                )
                tracker = title_full.split("🔎 ")[1].split("\n")[0]

                torrents.append(
                    {
                        "title": title,
                        "infoHash": torrent["infoHash"].lower(),
                        "fileIndex": torrent.get("fileIdx", None),
                        "seeders": seeders,
                        "size": torrent["behaviorHints"]["videoSize"],
                        "tracker": f"Comet|{tracker}",
                        "sources": torrent.get("sources", []),
                    }
                )
        except Exception as e:
            log_scraper_error("Comet", self.url, request.media_id, e)
            pass

        return torrents
