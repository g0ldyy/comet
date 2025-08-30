import aiohttp

from comet.utils.general import log_scraper_error, size_to_bytes
from comet.utils.torrent import extract_trackers_from_magnet
from comet.utils.models import settings


async def get_debridio(manager, session: aiohttp.ClientSession):
    torrents = []

    try:
        data = await session.get(
            f"https://debapi.debridio.com/{settings.DEBRIDIO_API_KEY}/search/{manager.media_only_id}"
        )
        data = await data.json()

        for torrent in data:
            size = torrent["size"]
            if isinstance(size, str):
                size = size_to_bytes(size)
            elif isinstance(size, int):
                pass
            else:
                size = 0

            torrents.append(
                {
                    "title": torrent["name"],
                    "infoHash": torrent["hash"],
                    "fileIndex": None,
                    "seeders": torrent["seeders"],
                    "size": size,
                    "tracker": f"Debridio|{torrent['indexer']}",
                    "sources": extract_trackers_from_magnet(torrent["magnet"]),
                }
            )
    except Exception as e:
        log_scraper_error(
            "Debridio", settings.DEBRIDIO_API_KEY, manager.media_only_id, e
        )

    await manager.filter_manager(torrents)
