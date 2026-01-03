import xml.etree.ElementTree as ET

from comet.core.logger import log_scraper_error, logger
from comet.scrapers.base import BaseScraper
from comet.scrapers.models import ScrapeRequest
from comet.services.anime import anime_mapper


class StremthruScraper(BaseScraper):
    def __init__(self, manager, session, url: str):
        super().__init__(manager, session, url)

    async def scrape(self, request: ScrapeRequest):
        torrents = []

        try:
            media_id = request.media_only_id
            if "kitsu" in request.media_id:
                imdb_id = anime_mapper.get_imdb_from_kitsu(int(media_id))
                if imdb_id:
                    media_id = imdb_id

            async with self.session.get(
                f"{self.url}/v0/torznab/api?t=search&imdbid={media_id}"
            ) as response:
                data_text = await response.text()

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
