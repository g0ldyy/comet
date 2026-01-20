from comet.core.logger import logger
from comet.scrapers.base import BaseScraper
from comet.scrapers.models import ScrapeRequest


class ZileanScraper(BaseScraper):
    def __init__(self, manager, session, url: str):
        super().__init__(manager, session, url)

    async def scrape(self, request: ScrapeRequest):
        torrents = []
        try:
            show = (
                f"&season={request.season}&episode={request.episode}"
                if request.media_type == "series"
                else ""
            )
            queries = request.title_variants or [request.title]
            for query in queries:
                data = await self.session.get(
                    f"{self.url}/dmm/filtered?query={query}{show}"
                )
                data = await data.json()

                for result in data:
                    torrents.append(
                        {
                            "title": result["raw_title"],
                            "infoHash": result["info_hash"].lower(),
                            "fileIndex": None,
                            "seeders": None,
                            "size": int(result["size"]),
                            "tracker": "DMM",
                            "sources": [],
                        }
                    )
        except Exception as e:
            logger.warning(
                f"Exception while getting torrents for {request.title} with Zilean ({self.url}): {e}"
            )

        return torrents
