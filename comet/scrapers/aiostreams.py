from comet.utils.general import (
    log_scraper_error,
    fetch_with_proxy_fallback,
)


async def get_aiostreams(manager, url: str):
    torrents = []
    try:
        results = await fetch_with_proxy_fallback(
            f"{url}/stream/{manager.media_type}/{manager.media_id}.json"
        )

        for torrent in results["streams"]:
            stream_data = torrent["streamData"]

            if "error" in stream_data:
                continue

            torrent_info = stream_data["torrent"]

            tracker = "AIOStreams"
            if "indexer" in stream_data:
                tracker += f"|{stream_data['indexer']}"

            torrents.append(
                {
                    "title": stream_data["filename"],
                    "infoHash": torrent_info["infoHash"],
                    "fileIndex": torrent.get("fileIdx", None),
                    "seeders": torrent_info.get("seeders", None),
                    "size": stream_data["size"],
                    "tracker": tracker,
                    "sources": torrent_info.get("sources", []),
                }
            )
    except Exception as e:
        log_scraper_error("AIOStreams", url, manager.media_id, e)

    await manager.filter_manager(torrents)
