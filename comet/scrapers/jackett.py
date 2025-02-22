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
    base_torrent = {
        "title": result["Title"],
        "infoHash": None,
        "fileIndex": None,
        "seeders": result["Seeders"],
        "size": result["Size"],
        "tracker": result["Tracker"],
        "sources": [],
    }

    torrents = []

    if result["Link"] is not None:
        content, magnet_hash, magnet_url = await download_torrent(
            session, result["Link"]
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

            await add_torrent_queue.add_torrent(
                magnet_url,
                base_torrent["seeders"],
                base_torrent["tracker"],
                media_id,
                season,
            )

            torrents.append(base_torrent)
            return torrents

    if "InfoHash" in result and result["InfoHash"]:
        base_torrent["infoHash"] = result["InfoHash"].lower()
        if result["MagnetUri"] is not None:
            base_torrent["sources"] = extract_trackers_from_magnet(result["MagnetUri"])

            await add_torrent_queue.add_torrent(
                result["MagnetUri"],
                base_torrent["seeders"],
                base_torrent["tracker"],
                media_id,
                season,
            )

        torrents.append(base_torrent)

    return torrents


async def fetch_jackett_results(
    session: aiohttp.ClientSession, indexer: str, query: str
):
    try:
        response = await session.get(
            f"{settings.INDEXER_MANAGER_URL}/api/v2.0/indexers/all/results?apikey={settings.INDEXER_MANAGER_API_KEY}&Query={query}&Tracker[]={indexer}",
            timeout=aiohttp.ClientTimeout(total=settings.INDEXER_MANAGER_TIMEOUT),
        )
        response = await response.json()
        return response.get("Results", [])
    except Exception as e:
        logger.warning(
            f"Exception while fetching Jackett results for indexer {indexer}: {e}"
        )
        return []


async def get_jackett(manager, session: aiohttp.ClientSession, title: str, seen: set):
    torrents = []
    try:
        tasks = [
            fetch_jackett_results(session, indexer, title)
            for indexer in settings.INDEXER_MANAGER_INDEXERS
        ]
        all_results = await asyncio.gather(*tasks)

        torrent_tasks = []
        for result_set in all_results:
            for result in result_set:
                if result["Details"] in seen:
                    continue

                seen.add(result["Details"])
                torrent_tasks.append(
                    process_torrent(
                        session, result, manager.media_only_id, manager.season
                    )
                )

        processed_torrents = await asyncio.gather(*torrent_tasks)
        torrents = [
            t for sublist in processed_torrents for t in sublist if t["infoHash"]
        ]
    except Exception as e:
        logger.warning(
            f"Exception while getting torrents for {title} with Jackett: {e}"
        )

    await manager.filter_manager(torrents)
