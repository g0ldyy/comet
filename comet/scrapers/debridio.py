import aiohttp
import base64
import orjson
import re
from functools import lru_cache

from comet.utils.general import log_scraper_error, size_to_bytes
from comet.utils.models import settings

DATA_PATTERN = re.compile(
    r"💾\s+([\d.,]+\s+[KMGT]B|Unknown|\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})(?:\s+👤\s+(\d+|Unknown|undefined))?(?:\s+⚙️\s+(.+?))?(?:\n|$)",
    re.IGNORECASE,
)


@lru_cache(maxsize=1)
def get_debridio_config():
    config = {
        "api_key": settings.DEBRIDIO_API_KEY,
        "provider": settings.DEBRIDIO_PROVIDER,
        "providerKey": settings.DEBRIDIO_PROVIDER_KEY,
        "disableUncached": False,
        "maxSize": "",
        "maxReturnPerQuality": "",
        "resolutions": ["4k", "1440p", "1080p", "720p", "480p", "360p", "unknown"],
        "excludedQualities": [],
    }
    return base64.b64encode(orjson.dumps(config)).decode("utf-8")


async def get_debridio(manager, session: aiohttp.ClientSession):
    if (
        not settings.DEBRIDIO_API_KEY
        or not settings.DEBRIDIO_PROVIDER
        or not settings.DEBRIDIO_PROVIDER_KEY
    ):
        return

    torrents = []
    b64_config = get_debridio_config()

    try:
        results = await session.get(
            f"https://addon.debridio.com/{b64_config}/stream/{manager.media_type}/{manager.media_id}.json"
        )
        results = await results.json()

        for torrent in results["streams"]:
            title_full = torrent["title"]
            torrent_name = title_full.split("\n")[0]

            match = DATA_PATTERN.search(title_full)

            size_str = match.group(1)
            size = (
                0
                if not size_str or "Unknown" in size_str or "-" in size_str
                else size_to_bytes(size_str.replace(",", ""))
            )

            seeders_str = match.group(2)
            seeders = (
                None
                if not seeders_str or seeders_str in ["undefined", "Unknown"]
                else int(seeders_str)
            )

            tracker = f"Debridio|{match.group(3)}"

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
            manager.media_id,
            e,
        )

    await manager.filter_manager("Debridio", torrents)
