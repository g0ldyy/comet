import asyncio
from typing import List, Set

from comet.core.constants import INDEXER_TIMEOUT
from comet.core.logger import logger
from comet.core.models import settings
from comet.scrapers.base import BaseScraper, deduplicate_torrents
from comet.scrapers.models import ScrapeRequest, ScrapeResult
from comet.services.indexer_manager import indexer_manager
from comet.services.torrent_manager import (
    add_torrent_queue,
    download_torrent,
    extract_torrent_metadata,
    extract_trackers_from_magnet,
)


class JackettScraper(BaseScraper):
    def __init__(self, manager, session, url: str):
        super().__init__(manager, session, url)

    async def process_torrent(self, result: dict, media_id: str, season: int):
        base_torrent = {
            "title": result["Title"],
            "infoHash": None,
            "fileIndex": None,
            "seeders": int(result["Seeders"])
            if result["Seeders"] is not None
            else None,
            "size": result["Size"],
            "tracker": result["Tracker"],
            "sources": [],
        }

        torrents = []

        if result["Link"] is not None:
            content, magnet_hash, magnet_url = await download_torrent(
                self.session, result["Link"]
            )

            if content:
                metadata = await asyncio.to_thread(extract_torrent_metadata, content)
                if metadata:
                    for file in metadata["files"]:
                        torrent = base_torrent.copy()
                        torrent["title"] = file["title"]
                        torrent["infoHash"] = metadata["info_hash"].lower()
                        torrent["fileIndex"] = file["index"]
                        torrent["size"] = file["size"]
                        torrent["sources"] = metadata["sources"]
                        torrents.append(torrent)
                    return torrents

            if magnet_hash:
                base_torrent["infoHash"] = magnet_hash.lower()
                base_torrent["sources"] = extract_trackers_from_magnet(magnet_url)

                await add_torrent_queue.add_torrent(
                    magnet_url,
                    base_torrent["seeders"],
                    base_torrent["tracker"],
                    media_id,
                    season,
                    base_torrent["infoHash"],
                )

                torrents.append(base_torrent)
                return torrents

        if "InfoHash" in result and result["InfoHash"]:
            base_torrent["infoHash"] = result["InfoHash"].lower()
            if result["MagnetUri"] is not None:
                base_torrent["sources"] = extract_trackers_from_magnet(
                    result["MagnetUri"]
                )

                await add_torrent_queue.add_torrent(
                    result["MagnetUri"],
                    base_torrent["seeders"],
                    base_torrent["tracker"],
                    media_id,
                    season,
                    base_torrent["infoHash"],
                )

            torrents.append(base_torrent)

        return torrents

    async def fetch_jackett_results(self, indexer: str, query: str):
        try:
            async with self.session.get(
                f"{self.url}/api/v2.0/indexers/all/results",
                params={
                    "apikey": settings.JACKETT_API_KEY,
                    "Query": query,
                    "Tracker[]": indexer,
                },
                timeout=INDEXER_TIMEOUT,
            ) as response:
                data = await response.json()
                if not isinstance(data, dict) or not isinstance(
                    data.get("Results"), list
                ):
                    return []
                return data["Results"]
        except Exception as e:
            logger.warning(
                f"Exception while fetching Jackett results for indexer {indexer}: {e}"
            )
            return []

    async def scrape(self, request: ScrapeRequest):
        if not settings.JACKETT_INDEXERS:
            try:
                await asyncio.wait_for(
                    indexer_manager.jackett_initialized.wait(),
                    timeout=settings.INDEXER_MANAGER_WAIT_TIMEOUT,
                )
            except asyncio.TimeoutError:
                pass

        if not settings.JACKETT_INDEXERS:
            logger.warning("No Jackett indexers available, skipping scrape.")
            return []
        torrents: List[ScrapeResult] = []
        seen: Set[str] = set()

        queries = request.title_queries(include_episode_variants=True)

        try:
            tasks = []
            for query in queries:
                tasks.extend(
                    [
                        self.fetch_jackett_results(indexer, query)
                        for indexer in settings.JACKETT_INDEXERS
                    ]
                )

            all_results = await asyncio.gather(*tasks)

            torrent_tasks = []
            for result_set in all_results:
                if not isinstance(result_set, list):
                    continue
                for result in result_set:
                    if not isinstance(result, dict):
                        continue
                    details = result.get("Details")
                    if not isinstance(details, str) or not details or details in seen:
                        continue

                    seen.add(details)
                    torrent_tasks.append(
                        self.process_torrent(
                            result, request.media_only_id, request.season
                        )
                    )

            processed_torrents = await asyncio.gather(
                *torrent_tasks, return_exceptions=True
            )
            for sublist in processed_torrents:
                if isinstance(sublist, Exception):
                    logger.warning(f"Error processing torrent with Jackett: {sublist}")
                    continue
                for torrent in sublist:
                    if isinstance(torrent, dict) and torrent.get("infoHash"):
                        torrents.append(torrent)
        except Exception as e:
            logger.warning(
                f"Exception while getting torrents for {request.title} with Jackett: {e}"
            )

        return deduplicate_torrents(torrents)
