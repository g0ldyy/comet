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


class ProwlarrScraper(BaseScraper):
    def __init__(self, manager, session: aiohttp.ClientSession):
        super().__init__(manager, session)

    async def process_torrent(self, result: dict, media_id: str, season: int):
        base_torrent = {
            "title": result["title"],
            "infoHash": None,
            "fileIndex": None,
            "seeders": int(result["seeders"])
            if result["seeders"] is not None
            else None,
            "size": result["size"],
            "tracker": result["indexer"],
            "sources": [],
        }

        torrents = []

        if "downloadUrl" in result:
            content, magnet_hash, magnet_url = await download_torrent(
                self.session, result["downloadUrl"]
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

        if "infoHash" in result and result["infoHash"]:
            base_torrent["infoHash"] = result["infoHash"].lower()
            if "guid" in result and result["guid"].startswith("magnet:"):
                base_torrent["sources"] = extract_trackers_from_magnet(result["guid"])

                await add_torrent_queue.add_torrent(
                    result["guid"],
                    base_torrent["seeders"],
                    base_torrent["tracker"],
                    media_id,
                    season,
                )

            torrents.append(base_torrent)

        return torrents

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
            indexers = [indexer.lower() for indexer in settings.PROWLARR_INDEXERS]

            get_indexers = await self.session.get(
                f"{settings.PROWLARR_URL}/api/v1/indexer",
                headers={"X-Api-Key": settings.PROWLARR_API_KEY},
                timeout=aiohttp.ClientTimeout(total=settings.INDEXER_MANAGER_TIMEOUT),
            )
            get_indexers = await get_indexers.json()

            indexers_id = []
            for indexer in get_indexers:
                if (
                    indexer["name"].lower() in indexers
                    or indexer["definitionName"].lower() in indexers
                ):
                    indexers_id.append(indexer["id"])

            tasks = []
            for query in queries:
                tasks.append(
                    self.session.get(
                        f"{settings.PROWLARR_URL}/api/v1/search?query={query}&indexerIds={'&indexerIds='.join(str(indexer_id) for indexer_id in indexers_id)}&type=search",
                        headers={"X-Api-Key": settings.PROWLARR_API_KEY},
                        timeout=aiohttp.ClientTimeout(
                            total=settings.INDEXER_MANAGER_TIMEOUT
                        ),
                    )
                )

            responses = await asyncio.gather(*tasks)
            all_results = []
            for response in responses:
                all_results.extend(await response.json())

            torrent_tasks = []
            for result in all_results:
                if result["infoUrl"] in seen:
                    continue

                seen.add(result["infoUrl"])
                torrent_tasks.append(
                    self.process_torrent(result, request.media_only_id, request.season)
                )

            processed_torrents = await asyncio.gather(*torrent_tasks)
            torrents = [
                t for sublist in processed_torrents for t in sublist if t["infoHash"]
            ]
        except Exception as e:
            logger.warning(
                f"Exception while getting torrents for {request.title} with Prowlarr: {e}"
            )

        return torrents
