import re

import aiohttp

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
    def __init__(self, manager, session: aiohttp.ClientSession):
        super().__init__(manager, session)

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
            results = await self.session.get(
                f"https://addon.debridio.com/{b64_config}/stream/{request.media_type}/{request.media_id}.json"
            )
            results = await results.json()

            for torrent in results["streams"]:
                title_full = torrent["title"]
                torrent_name = title_full.split("\n")[0]

                match = DATA_PATTERN.search(title_full)

                size_str = match.group(1) if match else None
                size = (
                    0
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
                    f"Debridio|{match.group(3)}"
                    if match and match.group(3)
                    else "Debridio"
                )

                info_hash = torrent["url"].split("/")[-2]

                torrents.append(
                    {
                        "title": torrent_name,
                        "infoHash": info_hash,
                        "fileIndex": None,
                        "seeders": seeders,
                        "size": size,
                        "tracker": tracker,
                        "sources": [],
                    }
                )

        except Exception as e:
            log_scraper_error(
                "Debridio",
                f"{settings.DEBRIDIO_PROVIDER}|{settings.DEBRIDIO_PROVIDER_KEY}",
                request.media_id,
                e,
            )

        return torrents
