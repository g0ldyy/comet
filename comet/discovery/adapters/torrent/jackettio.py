import re

from comet.core.provider_json import is_success_status
from comet.discovery.torrent_base import TorrentDiscoveryAdapter
from comet.discovery.torrent_models import ScrapeRequest
from comet.utils.formatting import size_to_bytes

data_pattern = re.compile(
    r"💾 ([\d.]+ [KMGT]B)\s+👥 (\d+)\s+⚙️ (\w+)",
)


class JackettioScraper(TorrentDiscoveryAdapter):
    url_setting = "JACKETTIO_URL"

    def __init__(self, manager, session, url: str):
        super().__init__(manager, session, url)

    @staticmethod
    def _parse_stream(torrent):
        title_full = torrent["title"]
        info_hash = torrent["infoHash"]

        match = data_pattern.search(title_full)
        size = size_to_bytes(match.group(1)) if match else None
        seeders = int(match.group(2)) if match else None
        tracker = match.group(3) if match else "Jackettio"
        return {
            "title": title_full.split("\n")[0],
            "infoHash": info_hash,
            "fileIndex": None,
            "seeders": seeders,
            "size": size,
            "tracker": f"Jackettio|{tracker}",
            "sources": [],
        }

    async def scrape(self, request: ScrapeRequest):
        async with self.session.get(
            f"{self.url}/stream/{request.media_type}/{request.media_id}.json",
        ) as response:
            if not is_success_status(response.status):
                raise RuntimeError(f"HTTP {response.status}")
            results = await response.json()
        return [self._parse_stream(stream) for stream in results["streams"]]
