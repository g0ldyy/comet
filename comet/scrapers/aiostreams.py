from comet.core.logger import log_scraper_error
from comet.scrapers.base import BaseScraper
from comet.scrapers.helpers.aiostreams import aiostreams_config
from comet.scrapers.models import ScrapeRequest


class AiostreamsScraper(BaseScraper):
    def __init__(
        self,
        manager,
        session,
        url: str,
        credentials: str | None = None,
    ):
        super().__init__(manager, session, url)
        self.credentials = credentials

    async def scrape(self, request: ScrapeRequest):
        torrents = []
        try:
            headers = aiostreams_config.get_headers_for_credential(self.credentials)

            params = {
                "type": request.media_type,
                "id": request.media_id,
            }

            async with self.session.get(
                f"{self.url}/api/v1/search",
                params=params,
                headers=headers,
            ) as response:
                results = await response.json()

            for torrent in results["data"]["results"]:
                tracker = "AIOStreams"
                if "indexer" in torrent:
                    tracker += f"|{torrent['indexer']}"

                torrents.append(
                    {
                        "title": torrent["filename"],
                        "infoHash": torrent["infoHash"],
                        "fileIndex": torrent.get("fileIdx", None),
                        "seeders": torrent.get("seeders", None),
                        "size": torrent["size"],
                        "tracker": tracker,
                        "sources": torrent.get("sources") or [],
                    }
                )
        except Exception as e:
            log_scraper_error("AIOStreams", self.url, request.media_id, e)

        return torrents
