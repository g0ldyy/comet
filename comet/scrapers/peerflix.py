from comet.core.logger import log_scraper_error
from comet.scrapers.base import BaseScraper
from comet.scrapers.models import ScrapeRequest


class PeerflixScraper(BaseScraper):
    BASE_URL = "https://peerflix.mov"

    async def scrape(self, request: ScrapeRequest):
        torrents = []
        try:
            async with self.session.get(
                f"{self.BASE_URL}/stream/{request.media_type}/{request.media_id}.json",
            ) as response:
                results = await response.json()

            for stream in results["streams"]:
                description = stream["description"]
                parts = description.split("🌐")
                tracker = parts[1] if len(parts) > 1 else None

                torrents.append(
                    {
                        "title": description.split("\n")[0],
                        "infoHash": stream["infoHash"].lower(),
                        "fileIndex": stream["fileIdx"],
                        "seeders": stream.get("seed"),
                        "size": stream.get("sizebytes"),
                        "tracker": f"Peerflix|{tracker}"
                        if tracker and tracker != "Peerflix"
                        else "Peerflix",
                        "sources": stream["sources"],
                    }
                )
        except Exception as e:
            log_scraper_error("Peerflix", self.BASE_URL, request.media_id, e)

        return torrents
