from comet.core.logger import log_scraper_error
from comet.scrapers.base import BaseScraper
from comet.scrapers.models import ScrapeRequest


class MeteorScraper(BaseScraper):
    BASE_URL = "https://meteorfortheweebs.midnightignite.me"

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
                title_full = torrent["description"]

                seeders = (
                    int(title_full.split("👥 ")[1].split(" ")[0])
                    if "👥" in title_full
                    else None
                )

                tracker = None
                if "🔗 " in title_full:
                    tracker = title_full.split("🔗 ")[1].split("\n")[0]
                
                torrents.append(
                    {
                        "title":torrent["behaviorHints"].get("filename"),
                        "infoHash": torrent["infoHash"].lower(),
                        "fileIndex": torrent.get("fileIdx", None),
                        "seeders": seeders,
                        "size": torrent["behaviorHints"].get("videoSize"),
                        "tracker": f"Meteor|{tracker}"
                        if tracker is not None
                        else "Meteor",
                        "sources": torrent.get("sources", []),
                    }
                )
        except Exception as e:
            log_scraper_error("Meteor", self.BASE_URL , request.media_id, e)

        return torrents
