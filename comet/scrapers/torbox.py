import aiohttp

from comet.utils.general import log_scraper_error
from comet.utils.torrent import extract_trackers_from_magnet
from comet.utils.models import settings


async def get_torbox(manager, session: aiohttp.ClientSession):
    torrents = []

    try:
        data = await session.get(
            f"https://search-api.torbox.app/torrents/imdb:{manager.media_only_id}",
            headers={"Authorization": f"Bearer {settings.TORBOX_API_KEY}"},
        )
        data = await data.json()

        for torrent in data["data"]["torrents"]:
            torrents.append(
                {
                    "title": torrent["raw_title"],
                    "infoHash": torrent["hash"],
                    "fileIndex": None,
                    "seeders": torrent["last_known_seeders"],
                    "size": torrent["size"],
                    "tracker": f"TorBox|{torrent['tracker']}",
                    "sources": extract_trackers_from_magnet(torrent["magnet"]),
                }
            )
    except Exception as e:
        log_scraper_error("TorBox", settings.TORBOX_API_KEY, manager.media_only_id, e)

    await manager.filter_manager(torrents)
