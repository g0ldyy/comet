from comet.utils.general import (
    log_scraper_error,
    fetch_with_proxy_fallback,
)
from comet.utils.aiostreams import aiostreams_config


async def get_aiostreams(manager, url: str, uuid_password: str | None = None):
    torrents = []
    try:
        headers = aiostreams_config.get_headers_for_credential(uuid_password)

        params = {
            "type": manager.media_type,
            "id": manager.media_id,
        }

        results = await fetch_with_proxy_fallback(
            f"{url}/api/v1/search",
            params=params,
            headers=headers,
        )

        for torrent in results["data"]["results"]:
            tracker = "AIOStreams"
            if "indexer" in torrent:
                tracker += f"|{torrent['indexer']}"

            torrents.append(
                {
                    "title": torrent["filename"],
                    "infoHash": torrent["infoHash"],
                    "fileIndex": torrent.get("fileIdx", None),
                    "seeders": torrent.get("seeders", None),
                    "size": torrent["size"],
                    "tracker": tracker,
                    "sources": torrent.get("sources", []),
                }
            )
    except Exception as e:
        log_scraper_error("AIOStreams", url, manager.media_id, e)

    await manager.filter_manager("AIOStreams", torrents)
