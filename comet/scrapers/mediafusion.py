from curl_cffi import requests

from comet.utils.models import settings
from comet.utils.logger import logger


async def get_mediafusion(manager, media_type: str, media_id: str):
    torrents = []
    try:
        try:
            get_mediafusion = requests.get(
                f"{settings.MEDIAFUSION_URL}/stream/{media_type}/{media_id}.json"
            ).json()
        except Exception as e:
            logger.warning(
                f"Failed to get MediaFusion results without proxy for {media_id}: {e}"
            )

            get_mediafusion = requests.get(
                f"{settings.MEDIAFUSION_URL}/stream/{media_type}/{media_id}.json",
                proxies={
                    "http": settings.DEBRID_PROXY_URL,
                    "https": settings.DEBRID_PROXY_URL,
                },
            ).json()

        for torrent in get_mediafusion["streams"]:
            title_full = torrent["description"]
            lines = title_full.split("\n")

            title = lines[0].replace("📂 ", "").replace("/", "")

            seeders = None
            if "👤" in lines[1]:
                seeders = int(lines[1].split("👤 ")[1].split("\n")[0])

            tracker = lines[-1].split("🔗 ")[1]

            torrents.append(
                {
                    "title": title,
                    "infoHash": torrent["infoHash"],
                    "fileIndex": torrent["fileIdx"] if "fileIdx" in torrent else 0,
                    "seeders": seeders,
                    "size": torrent["behaviorHints"][
                        "videoSize"
                    ],  # not the pack size but still useful for prowlarr userss
                    "tracker": f"MediaFusion|{tracker}",
                    "sources": torrent["sources"] if "sources" in torrent else [],
                }
            )
    except Exception as e:
        logger.warning(
            f"Exception while getting torrents for {media_id} with MediaFusion, your IP is most likely blacklisted (you should try proxying Comet): {e}"
        )
        pass

    await manager.filter_manager(torrents)
