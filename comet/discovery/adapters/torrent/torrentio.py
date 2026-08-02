import re

from comet.core.provider_json import is_success_status
from comet.discovery.torrent_base import TorrentDiscoveryAdapter
from comet.discovery.torrent_models import ScrapeRequest
from comet.utils.formatting import size_to_bytes

DATA_PATTERN = re.compile(
    r"(?:👤 (\d+) )?💾 ([\d.]+ [KMGT]B)(?: ⚙️ (\w+))?", re.IGNORECASE
)


class TorrentioScraper(TorrentDiscoveryAdapter):
    url_setting = "TORRENTIO_URL"
    impersonate = "chrome"

    def __init__(self, manager, session, url: str):
        super().__init__(manager, session, url)

    @staticmethod
    def _parse_stream(torrent):
        if not isinstance(torrent, dict):
            raise ValueError("Torrentio result is invalid")

        title_full = torrent.get("title")
        info_hash = torrent.get("infoHash")
        sources = torrent.get("sources", [])
        if (
            not isinstance(title_full, str)
            or not title_full
            or not isinstance(info_hash, str)
            or not info_hash
            or not isinstance(sources, list)
        ):
            raise ValueError("Torrentio result is invalid")

        if "\n💾" in title_full:
            title = title_full.split("\n💾")[0].split("\n")[-1]
        else:
            title = title_full.split("\n")[0]

        match = DATA_PATTERN.search(title_full)
        seeders = int(match.group(1)) if match and match.group(1) else None
        size = size_to_bytes(match.group(2)) if match and match.group(2) else None
        tracker = match.group(3) if match and match.group(3) else "KnightCrawler"

        return {
            "title": title,
            "infoHash": info_hash.lower(),
            "fileIndex": torrent.get("fileIdx"),
            "seeders": seeders,
            "size": size,
            "tracker": f"Torrentio|{tracker}",
            "sources": sources,
        }

    async def scrape(self, request: ScrapeRequest):
        async with self.session.get(
            f"{self.url}/stream/{request.media_type}/{request.media_id}.json",
        ) as response:
            if not is_success_status(response.status):
                raise RuntimeError(f"HTTP {response.status}")
            results = await response.json()
        if not isinstance(results, dict) or not isinstance(
            results.get("streams"), list
        ):
            raise ValueError("Torrentio response is invalid")
        return [self._parse_stream(torrent) for torrent in results["streams"]]
