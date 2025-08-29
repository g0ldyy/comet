import re


from comet.utils.general import (
    size_to_bytes,
    log_scraper_error,
    fetch_with_proxy_fallback,
)


data_pattern = re.compile(
    r"💾 ([\d.]+ [KMGT]B)\s+👥 (\d+)\s+⚙️ (\w+)",
)


async def get_jackettio(manager, url: str):
    torrents = []
    try:
        results = await fetch_with_proxy_fallback(
            f"{url}/stream/{manager.media_type}/{manager.media_id}.json"
        )

        for torrent in results["streams"]:
            title_full = torrent["title"]

            title = title_full.split("\n")[0]

            match = data_pattern.search(title_full)

            size = size_to_bytes(match.group(1))
            seeders = int(match.group(2))
            tracker = match.group(3)

            torrents.append(
                {
                    "title": title,
                    "infoHash": torrent["infoHash"],
                    "fileIndex": None,
                    "seeders": seeders,
                    "size": size,
                    "tracker": f"Jackettio|{tracker}",
                    "sources": None,
                }
            )
    except Exception as e:
        log_scraper_error("Jackettio", url, manager.media_id, e)

    await manager.filter_manager(torrents)
