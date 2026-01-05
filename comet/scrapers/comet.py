from comet.core.logger import log_scraper_error
from comet.scrapers.base import BaseScraper
from comet.scrapers.models import ScrapeRequest


class CometScraper(BaseScraper):
    def __init__(self, manager, session, url: str):
        super().__init__(manager, session, url)

    async def scrape(self, request: ScrapeRequest):
        torrents = []
        try:
            async with self.session.get(
                f"{self.url}/e30=/stream/{request.media_type}/{request.media_id}.json",
            ) as response:
                results = await response.json()

            for torrent in results["streams"]:
                title_full = torrent["description"]
                if title_full == "Content not digitally released yet.":
                    break

                try:
                    title = title_full.split("\n")[0].split("📄 ")[1]
                except:
                    continue
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

        return torrents
