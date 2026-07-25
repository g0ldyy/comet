import re

from comet.core.logger import log_scraper_error
from comet.core.models import settings
from comet.scrapers.base import BaseScraper
from comet.scrapers.helpers.debridio import debridio_config
from comet.scrapers.models import ScrapeRequest
from comet.utils.formatting import size_to_bytes

DATA_PATTERN = re.compile(
    r"💾\s+([\d.,]+\s+[KMGT]B|Unknown|\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})(?:\s+👤\s+(\d+|Unknown|undefined))?(?:\s+⚙️\s+(.+?))?(?:\n|$)",
    re.IGNORECASE,
)


class DebridioScraper(BaseScraper):
    impersonate = "chrome"

    def __init__(self, manager, session):
        super().__init__(manager, session)

    @staticmethod
    def _parse_stream(torrent):
        if not isinstance(torrent, dict):
            return None

        title_full = torrent.get("title")
        url = torrent.get("url")
        if (
            not isinstance(title_full, str)
            or not title_full
            or not isinstance(url, str)
        ):
            return None

        url_parts = url.split("/")
        if len(url_parts) < 2 or not url_parts[-2]:
            return None

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
        if (
            not settings.DEBRIDIO_API_KEY
            or not settings.DEBRIDIO_PROVIDER
            or not settings.DEBRIDIO_PROVIDER_KEY
        ):
            return []

        torrents = []
        b64_config = debridio_config.get_config()

        try:
            async with self.session.get(
                f"https://addon.debridio.com/{b64_config}/stream/{request.media_type}/{request.media_id}.json"
            ) as response:
                results = await response.json()

            if not isinstance(results, dict) or not isinstance(
                results.get("streams"), list
            ):
                return []

            for torrent in results["streams"]:
                parsed = self._parse_stream(torrent)
                if parsed is not None:
                    torrents.append(parsed)

        except Exception as e:
            log_scraper_error(
                "Debridio",
                f"{settings.DEBRIDIO_PROVIDER}|{settings.DEBRIDIO_PROVIDER_KEY}",
                request.media_id,
                e,
            )

        return torrents
