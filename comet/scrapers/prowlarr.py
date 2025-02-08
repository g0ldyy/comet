import aiohttp
import asyncio

from comet.utils.models import settings
from comet.utils.logger import logger
from comet.utils.torrent import (
    download_torrent,
    extract_torrent_metadata,
    extract_trackers_from_magnet,
)


async def process_torrent(session: aiohttp.ClientSession, result: dict):
    torrent = {
        "title": result["title"],
        "infoHash": None,
        "fileIndex": 0,
        "seeders": result.get("seeders"),
        "size": result["size"],
        "tracker": result["indexer"],
        "sources": [],
    }

    if "infoHash" in result and result["infoHash"]:
        torrent["infoHash"] = result["infoHash"].lower()
        torrent["sources"] = extract_trackers_from_magnet(result["guid"])
        return torrent

    if "downloadUrl" in result:
        content, magnet_hash, magnet_url = await download_torrent(
            session, result["downloadUrl"]
        )

        if magnet_hash:
            torrent["infoHash"] = magnet_hash.lower()
            torrent["sources"] = extract_trackers_from_magnet(magnet_url)
            return torrent

        if content:
            metadata = extract_torrent_metadata(content, result["title"])
            if metadata:
                torrent["infoHash"] = metadata["info_hash"]
                torrent["sources"] = metadata["announce_list"]
                torrent["fileIndex"] = metadata["file_index"]
                torrent["size"] = metadata["total_size"]

    return torrent


async def get_prowlarr(manager, session: aiohttp.ClientSession, title: str):
    torrents = []
    try:
        indexers = [indexer.lower() for indexer in settings.INDEXER_MANAGER_INDEXERS]

        get_indexers = await session.get(
            f"{settings.INDEXER_MANAGER_URL}/api/v1/indexer",
            headers={"X-Api-Key": settings.INDEXER_MANAGER_API_KEY},
        )
        get_indexers = await get_indexers.json()

        indexers_id = []
        for indexer in get_indexers:
            if (
                indexer["name"].lower() in indexers
                or indexer["definitionName"].lower() in indexers
            ):
                indexers_id.append(indexer["id"])

        response = await session.get(
            f"{settings.INDEXER_MANAGER_URL}/api/v1/search?query={title}&indexerIds={'&indexerIds='.join(str(indexer_id) for indexer_id in indexers_id)}&type=search",
            headers={"X-Api-Key": settings.INDEXER_MANAGER_API_KEY},
        )
        response = await response.json()

        torrent_tasks = [process_torrent(session, result) for result in response]
        processed_torrents = await asyncio.gather(*torrent_tasks)

        torrents = [t for t in processed_torrents if t["infoHash"]]
    except Exception as e:
        logger.warning(
            f"Exception while getting torrents for {title} with Prowlarr: {e}"
        )

    await manager.filter_manager(torrents)
