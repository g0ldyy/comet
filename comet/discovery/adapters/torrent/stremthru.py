import xml.etree.ElementTree as ET

from comet.core.provider_json import is_success_status
from comet.discovery.torrent_base import TorrentDiscoveryAdapter
from comet.discovery.torrent_models import ScrapeRequest
from comet.services.anime import anime_mapper
from comet.utils.formatting import normalize_info_hash


class StremthruScraper(TorrentDiscoveryAdapter):
    url_setting = "STREMTHRU_SCRAPE_URL"

    def __init__(self, manager, session, url: str):
        super().__init__(manager, session, url)

    async def scrape(self, request: ScrapeRequest):
        torrents = []
        media_id = request.media_only_id
        if request.media_id.startswith("kitsu:"):
            imdb_id = await anime_mapper.get_imdb_from_kitsu(int(media_id))
            if imdb_id:
                media_id = imdb_id
        async with self.session.get(
            f"{self.url}/v0/torznab/api?t=search&imdbid={media_id}"
        ) as response:
            if not is_success_status(response.status):
                raise RuntimeError(f"HTTP {response.status}")
            data_text = await response.text()
        root = ET.fromstring(data_text)
        for item in root.findall(".//item"):
            title_node = item.find("title")
            title = title_node.text if title_node is not None else None
            size = None
            info_hash = None
            indexer_name = None
            for attr in item.findall(
                ".//torznab:attr",
                {"torznab": "http://torznab.com/schemas/2015/feed"},
            ):
                attr_name = attr.get("name")
                attr_value = attr.get("value")
                if attr_name == "size":
                    try:
                        size = int(attr_value)
                    except (TypeError, ValueError):
                        size = None
                elif attr_name == "infohash":
                    info_hash = attr_value
                elif attr_name == "indexername" and attr_value:
                    indexer_name = attr_value
            if (
                not isinstance(title, str)
                or not title
                or not isinstance(info_hash, str)
                or not info_hash
            ):
                continue
            info_hash = normalize_info_hash(info_hash)
            try:
                if len(info_hash) != 40:
                    continue
                int(info_hash, 16)
            except ValueError:
                continue
            tracker = "StremThru" + (f"|{indexer_name}" if indexer_name else "")
            torrents.append(
                {
                    "title": title,
                    "infoHash": info_hash,
                    "fileIndex": None,
                    "seeders": None,
                    "size": size,
                    "tracker": tracker,
                    "sources": [],
                }
            )

        return torrents
