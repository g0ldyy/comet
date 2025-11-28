import aiohttp

from comet.core.logger import log_scraper_error
from comet.scrapers.base import BaseScraper
from comet.scrapers.helpers.aiostreams import aiostreams_config
from comet.scrapers.models import ScrapeRequest
from comet.utils.network import fetch_with_proxy_fallback


class AiostreamsScraper(BaseScraper):
    def __init__(
        self,
        manager,
        session: aiohttp.ClientSession,
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

            results = await fetch_with_proxy_fallback(
                self.session,
                f"{self.url}/api/v1/search",
                params=params,
                headers=headers,
            )

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
                        "sources": torrent.get("sources", []),
                    }
                )
        except Exception as e:
            log_scraper_error("AIOStreams", self.url, request.media_id, e)

        return torrents
