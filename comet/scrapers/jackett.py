import asyncio
from typing import List, Set

import aiohttp

from comet.core.logger import logger
from comet.core.models import settings
from comet.scrapers.base import BaseScraper
from comet.scrapers.models import ScrapeRequest, ScrapeResult
from comet.services.torrent_manager import (add_torrent_queue,
                                            download_torrent,
                                            extract_torrent_metadata,
                                            extract_trackers_from_magnet)

INDEXER_TIMEOUT = aiohttp.ClientTimeout(total=settings.INDEXER_MANAGER_TIMEOUT)


class JackettScraper(BaseScraper):
    def __init__(self, manager, session: aiohttp.ClientSession, url: str):
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

            if magnet_hash:
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
                base_torrent["sources"] = extract_trackers_from_magnet(
                    result["MagnetUri"]
                )

                await add_torrent_queue.add_torrent(
                    result["MagnetUri"],
                    base_torrent["seeders"],
                    base_torrent["tracker"],
                    media_id,
                    season,
                )

            torrents.append(base_torrent)

        return torrents

    async def fetch_jackett_results(self, indexer: str, query: str):
        try:
            response = await self.session.get(
                f"{self.url}/api/v2.0/indexers/all/results?apikey={settings.JACKETT_API_KEY}&Query={query}&Tracker[]={indexer}",
                timeout=INDEXER_TIMEOUT,
            )
            response = await response.json()
            return response.get("Results", [])
        except Exception as e:
            logger.warning(
                f"Exception while fetching Jackett results for indexer {indexer}: {e}"
            )
            return []

    async def scrape(self, request: ScrapeRequest):
        torrents: List[ScrapeResult] = []
        seen: Set[str] = set()

        queries = [request.title]
        if request.media_type == "series" and request.episode is not None:
            queries.append(f"{request.title} S{request.season:02d}")
            queries.append(
                f"{request.title} S{request.season:02d}E{request.episode:02d}"
            )

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
                for result in result_set:
                    if result["Details"] in seen:
                        continue

                    seen.add(result["Details"])
                    torrent_tasks.append(
                        self.process_torrent(
                            result, request.media_only_id, request.season
                        )
                    )

            processed_torrents = await asyncio.gather(*torrent_tasks)
            torrents = [
                t for sublist in processed_torrents for t in sublist if t["infoHash"]
            ]
        except Exception as e:
            logger.warning(
                f"Exception while getting torrents for {request.title} with Jackett: {e}"
            )

        return torrents
