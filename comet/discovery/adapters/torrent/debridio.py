import base64
import re

import orjson

from comet.core.models import settings
from comet.core.provider_json import is_success_status
from comet.discovery.torrent_base import TorrentDiscoveryAdapter
from comet.discovery.torrent_models import ScrapeRequest
from comet.utils.formatting import size_to_bytes

DATA_PATTERN = re.compile(
    r"💾\s+([\d.,]+\s+[KMGT]B|Unknown|\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})(?:\s+👤\s+(\d+|Unknown|undefined))?(?:\s+⚙️\s+(.+?))?(?:\n|$)",
    re.IGNORECASE,
)


def _debridio_config() -> str:
    return base64.b64encode(
        orjson.dumps(
            {
                "api_key": settings.DEBRIDIO_API_KEY,
                "provider": settings.DEBRIDIO_PROVIDER,
                "providerKey": settings.DEBRIDIO_PROVIDER_KEY,
                "disableUncached": False,
                "maxSize": "",
                "maxReturnPerQuality": "",
                "resolutions": [
                    "4k",
                    "1440p",
                    "1080p",
                    "720p",
                    "480p",
                    "360p",
                    "unknown",
                ],
                "excludedQualities": [],
            }
        )
    ).decode()


class DebridioScraper(TorrentDiscoveryAdapter):
    impersonate = "chrome"

    def __init__(self, manager, session):
        super().__init__(manager, session)

    @staticmethod
    def _parse_stream(torrent):
        if not isinstance(torrent, dict):
            raise ValueError("Debridio result is invalid")

        title_full = torrent.get("title")
        url = torrent.get("url")
        if (
            not isinstance(title_full, str)
            or not title_full
            or not isinstance(url, str)
        ):
            raise ValueError("Debridio result is invalid")

        url_parts = url.split("/")
        if len(url_parts) < 2 or not url_parts[-2]:
            raise ValueError("Debridio result is invalid")

        match = DATA_PATTERN.search(title_full)
        size_str = match.group(1) if match else None
        size = (
            None
            if not size_str or "Unknown" in size_str or "-" in size_str
            else size_to_bytes(size_str.replace(",", ""))
        )
        seeders_str = match.group(2) if match else None
        seeders = (
            None
            if not seeders_str or seeders_str in ["undefined", "Unknown"]
            else int(seeders_str)
        )
        tracker = (
            f"Debridio|{match.group(3)}" if match and match.group(3) else "Debridio"
        )
        return {
            "title": title_full.split("\n")[0],
            "infoHash": url_parts[-2],
            "fileIndex": None,
            "seeders": seeders,
            "size": size,
            "tracker": tracker,
            "sources": [],
        }

    async def scrape(self, request: ScrapeRequest):
        async with self.session.get(
            f"https://addon.debridio.com/{_debridio_config()}/stream/"
            f"{request.media_type}/{request.media_id}.json"
        ) as response:
            if not is_success_status(response.status):
                raise RuntimeError(f"HTTP {response.status}")
            results = await response.json()
        if not isinstance(results, dict) or not isinstance(
            results.get("streams"), list
        ):
            raise ValueError("Debridio response is invalid")
        return [self._parse_stream(torrent) for torrent in results["streams"]]
