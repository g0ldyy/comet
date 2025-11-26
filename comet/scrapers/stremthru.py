import xml.etree.ElementTree as ET

import aiohttp

from comet.core.logger import log_scraper_error, logger
from comet.scrapers.base import BaseScraper
from comet.scrapers.models import ScrapeRequest


class StremthruScraper(BaseScraper):
    def __init__(self, manager, session: aiohttp.ClientSession, url: str):
        super().__init__(manager, session, url)

    async def scrape(self, request: ScrapeRequest):
        torrents = []

        try:
            data = await self.session.get(
                f"{self.url}/v0/torznab/api?t=search&imdbid={request.media_only_id}"
            )
            data_text = await data.text()

            root = ET.fromstring(data_text)

            for item in root.findall(".//item"):
                try:
                    title = item.find("title").text

                    size = None
                    info_hash = None

                    for attr in item.findall(
                        ".//torznab:attr",
                        {"torznab": "http://torznab.com/schemas/2015/feed"},
                    ):
                        attr_name = attr.get("name")
                        attr_value = attr.get("value")

                        if attr_name == "size":
                            size = int(attr_value)
                        elif attr_name == "infohash":
                            info_hash = attr_value

                    if size is None or info_hash is None:
                        continue

                    torrents.append(
                        {
                            "title": title,
                            "infoHash": info_hash,
                            "fileIndex": None,
                            "seeders": None,
                            "size": size,
                            "tracker": "StremThru",
                            "sources": [],
                        }
                    )

                except Exception as e:
                    logger.warning(f"Error parsing torrent item from StremThru: {e}")
                    continue

        except Exception as e:
            log_scraper_error("StremThru", self.url, request.media_only_id, e)

        return torrents
