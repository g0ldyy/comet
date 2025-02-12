import aiohttp
import asyncio

from comet.utils.models import settings
from comet.utils.logger import logger
from comet.utils.torrent import (
    download_torrent,
    extract_torrent_metadata,
    extract_trackers_from_magnet,
    file_index_queue,
)


async def process_torrent(
    session: aiohttp.ClientSession, result: dict, season: int, episode: int
):
    base_torrent = {
        "title": result["title"],
        "infoHash": None,
        "fileIndex": None,
        "seeders": result.get("seeders"),
        "size": result["size"],
        "tracker": result["indexer"],
        "sources": [],
    }

    torrents = []

    if "downloadUrl" in result:
        content, magnet_hash, magnet_url = await download_torrent(
            session, result["downloadUrl"]
        )

        if content:
            metadata = extract_torrent_metadata(content)
            if metadata:
                for file in metadata["files"]:
                    torrent = base_torrent.copy()
                    torrent["title"] = file["name"]
                    torrent["infoHash"] = metadata["info_hash"].lower()
                    torrent["fileIndex"] = file["index"]
                    torrent["size"] = file["size"]
                    torrent["sources"] = metadata["announce_list"]
                    torrents.append(torrent)
                return torrents

        if magnet_hash and magnet_url:
            base_torrent["infoHash"] = magnet_hash.lower()
            base_torrent["sources"] = extract_trackers_from_magnet(magnet_url)

            await file_index_queue.add_torrent(
                base_torrent["infoHash"], magnet_url, season, episode
            )

            torrents.append(base_torrent)
            return torrents

    if "infoHash" in result and result["infoHash"]:
        base_torrent["infoHash"] = result["infoHash"].lower()
        if "guid" in result and result["guid"].startswith("magnet:"):
            base_torrent["sources"] = extract_trackers_from_magnet(result["guid"])

            await file_index_queue.add_torrent(
                base_torrent["infoHash"], result["guid"], season, episode
            )

        torrents.append(base_torrent)

    return torrents


async def get_prowlarr(manager, session: aiohttp.ClientSession, title: str, seen: set):
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

        torrent_tasks = []
        for result in response:
            if result["infoUrl"] in seen:
                continue

            seen.add(result["infoUrl"])
            torrent_tasks.append(
                process_torrent(session, result, manager.season, manager.episode)
            )

        processed_torrents = await asyncio.gather(*torrent_tasks)
        torrents = [
            t for sublist in processed_torrents for t in sublist if t["infoHash"]
        ]
    except Exception as e:
        logger.warning(
            f"Exception while getting torrents for {title} with Prowlarr: {e}"
        )

    await manager.filter_manager(torrents)
