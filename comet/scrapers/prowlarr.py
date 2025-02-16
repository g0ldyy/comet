import aiohttp
import asyncio

from comet.utils.models import settings
from comet.utils.logger import logger
from comet.utils.torrent import (
    download_torrent,
    extract_torrent_metadata,
    extract_trackers_from_magnet,
    add_torrent_queue,
)


async def process_torrent(
    session: aiohttp.ClientSession, result: dict, media_id: str, season: int
):
    torrent = {
        "title": result["title"],
        "infoHash": None,
<<<<<<< HEAD
        "fileIndex": None,
        "seeders": result["seeders"],
=======
        "fileIndex": 0,
        "seeders": result.get("seeders"),
>>>>>>> d16a8c377b2b562c49647dc997792749ce0bd35b
        "size": result["size"],
        "tracker": result["indexer"],
        "sources": [],
    }

    if "downloadUrl" in result:
        content, magnet_hash, magnet_url = await download_torrent(
            session, result["downloadUrl"]
        )

        if content:
            metadata = extract_torrent_metadata(content, season, episode)
            if metadata:
                torrent["infoHash"] = metadata["info_hash"]
                torrent["sources"] = metadata["announce_list"]
                torrent["fileIndex"] = metadata["file_index"]
                torrent["size"] = metadata["file_size"]
                return torrent

        if magnet_hash and magnet_url:
            torrent["infoHash"] = magnet_hash.lower()
            torrent["sources"] = extract_trackers_from_magnet(magnet_url)

<<<<<<< HEAD
            await add_torrent_queue.add_torrent(
                magnet_url,
                base_torrent["seeders"],
                base_torrent["tracker"],
                media_id,
                season,
=======
            await file_index_queue.add_torrent(
                magnet_hash.lower(), magnet_url, season, episode
>>>>>>> d16a8c377b2b562c49647dc997792749ce0bd35b
            )

            return torrent

    if "infoHash" in result and result["infoHash"]:
        torrent["infoHash"] = result["infoHash"].lower()
        if "guid" in result and result["guid"].startswith("magnet:"):
            torrent["sources"] = extract_trackers_from_magnet(result["guid"])

<<<<<<< HEAD
            await add_torrent_queue.add_torrent(
                result["guid"],
                base_torrent["seeders"],
                base_torrent["tracker"],
                media_id,
                season,
=======
            await file_index_queue.add_torrent(
                torrent["infoHash"], result["guid"], season, episode
>>>>>>> d16a8c377b2b562c49647dc997792749ce0bd35b
            )

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

<<<<<<< HEAD
        torrent_tasks = []
        for result in response:
            if result["infoUrl"] in seen:
                continue

            seen.add(result["infoUrl"])
            torrent_tasks.append(
                process_torrent(session, result, manager.media_only_id, manager.season)
            )

        processed_torrents = await asyncio.gather(*torrent_tasks)
        torrents = [
            t for sublist in processed_torrents for t in sublist if t["infoHash"]
=======
        torrent_tasks = [
            process_torrent(session, result, manager.season, manager.episode)
            for result in response
>>>>>>> d16a8c377b2b562c49647dc997792749ce0bd35b
        ]
        processed_torrents = await asyncio.gather(*torrent_tasks)

        torrents = [t for t in processed_torrents if t["infoHash"]]
    except Exception as e:
        logger.warning(
            f"Exception while getting torrents for {title} with Prowlarr: {e}"
        )

    await manager.filter_manager(torrents)
