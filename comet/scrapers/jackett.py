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
    torrent = {
        "title": result["Title"],
        "infoHash": None,
        "fileIndex": 0,
        "seeders": result.get("Seeders"),
        "size": result["Size"],
        "tracker": result["Tracker"],
        "sources": [],
    }

    if result["Link"] is not None:
        content, magnet_hash, magnet_url = await download_torrent(
            session, result["Link"]
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

            await file_index_queue.add_torrent(
                magnet_hash.lower(), magnet_url, season, episode
            )

            return torrent

    if "InfoHash" in result and result["InfoHash"]:
        torrent["infoHash"] = result["InfoHash"].lower()
        if "MagnetUri" in result and result["MagnetUri"]:
            torrent["sources"] = extract_trackers_from_magnet(result["MagnetUri"])

            await file_index_queue.add_torrent(
                torrent["infoHash"], result["MagnetUri"], season, episode
            )

    return torrent


async def fetch_jackett_results(
    session: aiohttp.ClientSession, indexer: str, query: str
):
    try:
        async with session.get(
            f"{settings.INDEXER_MANAGER_URL}/api/v2.0/indexers/all/results?apikey={settings.INDEXER_MANAGER_API_KEY}&Query={query}&Tracker[]={indexer}",
            timeout=aiohttp.ClientTimeout(total=settings.INDEXER_MANAGER_TIMEOUT),
        ) as response:
            response_json = await response.json()
            return response_json.get("Results", [])
    except Exception as e:
        logger.warning(
            f"Exception while fetching Jackett results for indexer {indexer}: {e}"
        )
        return []


async def get_jackett(manager, session: aiohttp.ClientSession, title: str):
    torrents = []
    try:
        indexers = [
            indexer.replace("_", " ") for indexer in settings.INDEXER_MANAGER_INDEXERS
        ]
        tasks = [fetch_jackett_results(session, indexer, title) for indexer in indexers]
        all_results = await asyncio.gather(*tasks)

        torrent_tasks = []
        for result_set in all_results:
            for result in result_set:
                torrent_tasks.append(
                    process_torrent(session, result, manager.season, manager.episode)
                )

        processed_torrents = await asyncio.gather(*torrent_tasks)
        torrents = [t for t in processed_torrents if t["infoHash"]]
    except Exception as e:
        logger.warning(
            f"Exception while getting torrents for {title} with Jackett: {e}"
        )

    await manager.filter_manager(torrents)
