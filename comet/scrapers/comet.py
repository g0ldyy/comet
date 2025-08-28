from comet.utils.general import (
    log_scraper_error,
    fetch_with_proxy_fallback,
)


async def get_comet(manager, url: str):
    torrents = []
    try:
        get_comet = await fetch_with_proxy_fallback(
            f"{url}/stream/{manager.media_type}/{manager.media_id}.json"
        )

        for torrent in get_comet["streams"]:
            title_full = torrent["description"]
            title = title_full.split("\n")[0].split("📄 ")[1]

            seeders = (
                int(title_full.split("👤 ")[1].split(" ")[0])
                if "👤" in title_full
                else None
            )
            tracker = title_full.split("🔎 ")[1].split("\n")[0]

            torrents.append(
                {
                    "title": title,
                    "infoHash": torrent["infoHash"].lower(),
                    "fileIndex": torrent.get("fileIdx", None),
                    "seeders": seeders,
                    "size": torrent["behaviorHints"]["videoSize"],
                    "tracker": f"Comet|{tracker}",
                    "sources": torrent.get("sources", []),
                }
            )
    except Exception as e:
        log_scraper_error("Comet", url, manager.media_id, e)
        pass

    await manager.filter_manager(torrents)
