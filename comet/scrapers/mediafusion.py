from curl_cffi import requests

from comet.utils.models import settings
from comet.utils.general import get_proxies, log_scraper_error


async def get_mediafusion(manager, media_type: str, media_id: str):
    torrents = []
    try:
        get_mediafusion = requests.get(
            f"{settings.MEDIAFUSION_URL}/D-zn4qJLK4wUZVWscY9ESCnoZBEiNJCZ9uwfCvmxuliDjY7vkc-fu0OdxUPxwsP3_A/stream/{media_type}/{media_id}.json",
            proxies=get_proxies(),
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
                    "infoHash": torrent["infoHash"].lower(),
                    "fileIndex": torrent["fileIdx"] if "fileIdx" in torrent else None,
                    "seeders": seeders,
                    "size": torrent["behaviorHints"][
                        "videoSize"
                    ],  # not the pack size but still useful for prowlarr userss
                    "tracker": f"MediaFusion|{tracker}",
                    "sources": torrent["sources"] if "sources" in torrent else [],
                }
            )
    except Exception as e:
        log_scraper_error("MediaFusion", media_id, e)
        pass

    await manager.filter_manager(torrents)
