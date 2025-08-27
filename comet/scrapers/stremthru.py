import aiohttp
import xml.etree.ElementTree as ET

from comet.utils.logger import logger
from comet.utils.general import log_scraper_error


async def get_stremthru(manager, session: aiohttp.ClientSession, url: str):
    torrents = []

    try:
        response = await session.get(
            f"{url}/v0/torznab/api?t=search&imdbid={manager.media_only_id}"
        )
        response_text = await response.text()

        root = ET.fromstring(response_text)

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

                torrent = {
                    "title": title,
                    "infoHash": info_hash,
                    "fileIndex": None,
                    "seeders": None,
                    "size": size,
                    "tracker": "StremThru",
                    "sources": [],
                }

                torrents.append(torrent)

            except Exception as e:
                logger.warning(f"Error parsing torrent item from StremThru: {e}")
                continue

    except Exception as e:
        log_scraper_error("StremThru", url, manager.media_only_id, e)

    await manager.filter_manager(torrents)
