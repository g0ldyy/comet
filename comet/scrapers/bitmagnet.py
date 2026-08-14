import asyncio
import xml.etree.ElementTree as ET

from comet.core.logger import logger
from comet.core.models import settings
from comet.metadata.serioussportsync import is_serioussportsync_event_id
from comet.scrapers.base import BaseScraper, deduplicate_torrents
from comet.scrapers.models import ScrapeRequest


class BitmagnetScraper(BaseScraper):
    def __init__(self, manager, session, url: str):
        super().__init__(manager, session, url)

    def parse_items(self, root):
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

    async def scrape_page(
        self,
        imdb_id,
        scrape_type,
        offset,
        limit,
        season=None,
        episode=None,
        query=None,
    ):
        try:
            params = {
                "offset": offset,
                "limit": limit,
            }
            if query:
                params.update({"t": "search", "q": query})
            else:
                params.update({"t": scrape_type, "imdbid": imdb_id})
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
                return self.parse_items(root)
        except ET.ParseError as e:
            logger.warning(f"Error parsing BitMagnet page offset={offset}: {e}")
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

        queries = (
            request.query_titles
            if is_serioussportsync_event_id(request.media_type, request.media_only_id)
            else (None,)
        )
        batch_size = settings.BITMAGNET_MAX_CONCURRENT_PAGES

        for query in queries:
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
                        self.scrape_page(
                            imdb_id,
                            scrape_type,
                            current_offset,
                            limit,
                            season,
                            episode,
                            query,
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

        return deduplicate_torrents(torrents)
