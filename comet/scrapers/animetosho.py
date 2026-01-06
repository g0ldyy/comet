import asyncio
import xml.etree.ElementTree as ET

from comet.core.logger import logger
from comet.core.models import settings
from comet.scrapers.base import BaseScraper
from comet.scrapers.models import ScrapeRequest
from comet.services.torrent_manager import extract_trackers_from_magnet


class AnimeToshoScraper(BaseScraper):
    def __init__(self, manager, session):
        super().__init__(manager, session)

    def parse_items(self, root):
        torrents = []
        for item in root.findall(".//item"):
            try:
                title = item.find("title").text

                size = None
                info_hash = None
                seeders = None

                magnet = None

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
                    elif attr_name == "seeders":
                        seeders = int(attr_value)
                    elif attr_name == "magneturl":
                        magnet = attr_value

                if info_hash is None:
                    continue

                torrents.append(
                    {
                        "title": title,
                        "infoHash": info_hash,
                        "fileIndex": None,
                        "seeders": seeders,
                        "size": size,
                        "tracker": "AnimeTosho",
                        "sources": extract_trackers_from_magnet(magnet),
                    }
                )
            except Exception as e:
                logger.warning(f"Error parsing torrent item from AnimeTosho: {e}")
                continue
        return torrents

    async def scrape_page(self, query, offset, limit):
        try:
            async with self.session.get(
                f"https://feed.animetosho.org/api?t=search&q={query}&offset={offset}&limit={limit}"
            ) as response:
                content = await response.text()
                if not content.strip():
                    return [], 0

                root = ET.fromstring(content)

                response_node = root.find(
                    ".//newznab:response",
                    {"newznab": "http://www.newznab.com/DTD/2010/feeds/attributes/"},
                )
                total = int(response_node.get("total", 0))
                items = self.parse_items(root)
                return items, total
        except Exception as e:
            logger.warning(f"Error scraping AnimeTosho offset={offset}: {e}")
            return [], 0

    async def scrape(self, request: ScrapeRequest):
        torrents = []
        query = request.title
        limit = 150

        initial_items, total = await self.scrape_page(query, 0, limit)
        torrents.extend(initial_items)

        if total > limit:
            batch_size = settings.ANIMETOSHO_MAX_CONCURRENT_PAGES
            current_offset = limit

            while current_offset < total:
                tasks = []
                for _ in range(batch_size):
                    if current_offset >= total:
                        break

                    tasks.append(self.scrape_page(query, current_offset, limit))
                    current_offset += limit

                if tasks:
                    results = await asyncio.gather(*tasks)
                    for batch_items, _ in results:
                        torrents.extend(batch_items)

        return torrents
