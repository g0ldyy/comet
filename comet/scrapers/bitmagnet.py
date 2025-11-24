import aiohttp
import asyncio
import xml.etree.ElementTree as ET

from comet.utils.logger import logger
from comet.utils.models import settings


def parse_bitmagnet_items(root):
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

            torrent = {
                "title": title,
                "infoHash": info_hash,
                "fileIndex": None,
                "seeders": seeders,
                "size": size,
                "tracker": "BitMagnet",
                "sources": [],
            }

            torrents.append(torrent)

        except Exception as e:
            logger.warning(f"Error parsing torrent item from BitMagnet: {e}")
            continue
    return torrents


async def scrape_bitmagnet_page(session, url, query, offset, limit):
    try:
        params = {"t": "search", "q": query, "offset": offset, "limit": limit}
        async with session.get(f"{url}/torznab/api", params=params) as response:
            data_text = await response.text()
            root = ET.fromstring(data_text)
            return parse_bitmagnet_items(root)
    except Exception as e:
        logger.warning(f"Error scraping BitMagnet page offset={offset}: {e}")
        return []


async def get_bitmagnet(manager, session: aiohttp.ClientSession, url: str):
    torrents = []
    limit = 100
    query = manager.title

    batch_size = settings.BITMAGNET_MAX_CONCURRENT_PAGES
    offset = 0

    while True:
        tasks = []
        for i in range(batch_size):
            current_offset = offset + (i * limit)
            tasks.append(
                scrape_bitmagnet_page(session, url, query, current_offset, limit)
            )

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

    await manager.filter_manager("BitMagnet", torrents)
