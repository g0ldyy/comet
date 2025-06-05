import re

from curl_cffi import requests

from comet.utils.models import settings
from comet.utils.general import size_to_bytes, get_proxies, log_scraper_error


data_pattern = re.compile(
    r"(?:👤 (\d+) )?💾 ([\d.]+ [KMGT]B)(?: ⚙️ (\w+))?", re.IGNORECASE
)


async def get_torrentio(manager, media_type: str, media_id: str):
    torrents = []
    try:
        get_torrentio = requests.get(
            f"{settings.TORRENTIO_URL}/stream/{media_type}/{media_id}.json",
            proxies=get_proxies(),
        ).json()

        for torrent in get_torrentio["streams"]:
            title_full = torrent["title"]
            title = (
                title_full.split("\n")[0]
                if settings.TORRENTIO_URL == "https://torrentio.strem.fun"
                else title_full.split("\n💾")[0].split("\n")[-1]
            )

            match = data_pattern.search(title_full)

            seeders = int(match.group(1)) if match.group(1) else None
            size = size_to_bytes(match.group(2))
            tracker = match.group(3) if match.group(3) else "KnightCrawler"

            torrents.append(
                {
                    "title": title,
                    "infoHash": torrent["infoHash"].lower(),
                    "fileIndex": torrent["fileIdx"] if "fileIdx" in torrent else None,
                    "seeders": seeders,
                    "size": size,
                    "tracker": f"Torrentio|{tracker}",
                    "sources": torrent["sources"] if "sources" in torrent else [],
                }
            )
    except Exception as e:
        log_scraper_error("Torrentio", media_id, e)

    await manager.filter_manager(torrents)
