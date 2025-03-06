from curl_cffi import requests

from comet.utils.models import settings
from comet.utils.logger import logger


async def get_comet(manager, media_type: str, media_id: str):
    torrents = []
    try:
        try:
            get_comet = requests.get(
                f"{settings.COMET_URL}/stream/{media_type}/{media_id}.json"
            ).json()
        except Exception as e:
            logger.warning(
                f"Failed to get Comet results without proxy for {media_id}: {e}"
            )

            get_comet = requests.get(
                f"{settings.COMET_URL}/stream/{media_type}/{media_id}.json",
                proxies={
                    "http": settings.DEBRID_PROXY_URL,
                    "https": settings.DEBRID_PROXY_URL,
                },
            ).json()

        for torrent in get_comet["streams"]:
            title_full = torrent["description"]
            title = title_full.split("\n")[0]

            seeders = (
                title_full.split("👤 ")[1].split(" ")[0] if "👤" in title_full else None
            )
            tracker = title_full.split("🔎 ")[1].split("\n")[0]

            torrents.append(
                {
                    "title": title,
                    "infoHash": torrent["infoHash"].lower(),
                    "fileIndex": torrent["fileIdx"] if "fileIdx" in torrent else None,
                    "seeders": seeders,
                    "size": torrent["behaviorHints"]["videoSize"],
                    "tracker": f"Comet|{tracker}",
                    "sources": torrent["sources"] if "sources" in torrent else [],
                }
            )
    except Exception as e:
        logger.warning(
            f"Exception while getting torrents for {media_id} with Comet, your IP is most likely blacklisted (you should try proxying Comet): {e}"
        )
        pass

    await manager.filter_manager(torrents)
