import re


from comet.utils.general import (
    size_to_bytes,
    log_scraper_error,
    fetch_with_proxy_fallback,
)


data_pattern = re.compile(
    r"(?:👤 (\d+) )?💾 ([\d.]+ [KMGT]B)(?: ⚙️ (\w+))?", re.IGNORECASE
)


async def get_torrentio(manager, url: str, media_type: str, media_id: str):
    torrents = []
    try:
        get_torrentio = await fetch_with_proxy_fallback(
            f"{url}/stream/{media_type}/{media_id}.json"
        )

        for torrent in get_torrentio["streams"]:
            title_full = torrent["title"]
            title = (
                title_full.split("\n")[0]
                if url == "https://torrentio.strem.fun"
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
