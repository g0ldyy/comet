import asyncio
import xml.etree.ElementTree as ET

from comet.core.logger import logger
from comet.core.models import settings
from comet.scrapers.base import BaseScraper
from comet.scrapers.models import ScrapeRequest


class BitmagnetScraper(BaseScraper):
    def __init__(self, manager, session, url: str):
        super().__init__(manager, session, url)

    def parse_bitmagnet_items(self, root):
        torrents = []
        for item in root.findall(".//item"):
            try:
                title = item.find("title").text

                size = None
                info_hash = None
                seeders = None

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

                if info_hash is None:
                    continue

                torrents.append(
                    {
                        "title": title,
                        "infoHash": info_hash,
                        "fileIndex": None,
                        "seeders": seeders,
                        "size": size,
                        "tracker": "BitMagnet",
                        "sources": [],
                    }
                )

            except Exception as e:
                logger.warning(f"Error parsing torrent item from BitMagnet: {e}")
                continue
        return torrents

    async def scrape_bitmagnet_page(
        self, imdb_id, scrape_type, offset, limit, season=None, episode=None
    ):
        try:
            params = {
                "t": scrape_type,
                "imdbid": imdb_id,
                "offset": offset,
                "limit": limit,
            }
            if season is not None:
                params["season"] = season
            if episode is not None:
                params["ep"] = episode
            async with self.session.get(
                f"{self.url}/torznab/api", params=params
            ) as response:
                data_text = await response.text()
                if not data_text.strip():
                    return []
                root = ET.fromstring(data_text)
                return self.parse_bitmagnet_items(root)
        except ET.ParseError:
            return []
        except Exception as e:
            logger.warning(f"Error scraping BitMagnet page offset={offset}: {e}")
            return []

    async def scrape(self, request: ScrapeRequest):
        torrents = []
        limit = 100
        imdb_id = request.media_only_id
        scrape_type = "movie" if request.media_type == "movie" else "tvsearch"
        season = request.season
        episode = request.episode

        batch_size = settings.BITMAGNET_MAX_CONCURRENT_PAGES
        offset = 0

        while True:
            if offset >= settings.BITMAGNET_MAX_OFFSET:
                break

            tasks = []
            for i in range(batch_size):
                current_offset = offset + (i * limit)
                if current_offset >= settings.BITMAGNET_MAX_OFFSET:
                    break
                tasks.append(
                    self.scrape_bitmagnet_page(
                        imdb_id, scrape_type, current_offset, limit, season, episode
                    )
                )

            if not tasks:
                break

            results = await asyncio.gather(*tasks)

            should_stop = False
            for batch_results in results:
                if not batch_results:
                    should_stop = True
                    break

                torrents.extend(batch_results)

                if len(batch_results) < limit:
                    should_stop = True
                    break

            if should_stop:
                break

            offset += batch_size * limit

        return torrents
