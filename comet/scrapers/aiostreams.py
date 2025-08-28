from comet.utils.general import (
    log_scraper_error,
    fetch_with_proxy_fallback,
)


async def get_aiostreams(manager, url: str):
    torrents = []
    try:
        get_aiostreams = await fetch_with_proxy_fallback(
            f"{url}/stream/{manager.media_type}/{manager.media_id}.json"
        )

        for torrent in get_aiostreams["streams"]:
            stream_data = torrent["streamData"]
            torrent_info = stream_data["torrent"]

            torrents.append(
                {
                    "title": stream_data["filename"],
                    "infoHash": torrent_info["infoHash"],
                    "fileIndex": torrent.get("fileIdx", None),
                    "seeders": torrent_info.get("seeders", None),
                    "size": stream_data["size"],
                    "tracker": f"AIOStreams|{stream_data['indexer']}",
                    "sources": torrent_info["sources"],
                }
            )
    except Exception as e:
        log_scraper_error("AIOStreams", url, manager.media_id, e)

    await manager.filter_manager(torrents)
