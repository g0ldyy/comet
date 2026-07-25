import asyncio

from comet.core.constants import INDEXER_TIMEOUT
from comet.core.logger import logger
from comet.core.models import settings
from comet.scrapers.base import (
    BaseScraper,
    deduplicate_torrents,
    gather_with_error_logging,
)
from comet.scrapers.models import ScrapeRequest, ScrapeResult
from comet.services.indexer_manager import indexer_manager
from comet.services.torrent_manager import (
    add_torrent_queue,
    download_torrent,
    extract_torrent_metadata,
    extract_trackers_from_magnet,
)


class ProwlarrScraper(BaseScraper):
    def __init__(self, manager, session, url: str):
        super().__init__(manager, session, url)

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

        if result.get("infoHash"):
            base_torrent["infoHash"] = result["infoHash"].lower()
            if "guid" in result and result["guid"].startswith("magnet:"):
                base_torrent["sources"] = extract_trackers_from_magnet(result["guid"])

                await add_torrent_queue.add_torrent(
                    result["guid"],
                    base_torrent["seeders"],
                    base_torrent["tracker"],
                    media_id,
                    season,
                    base_torrent["infoHash"],
                )

            torrents.append(base_torrent)

        return torrents

    async def _fetch_search_results(self, query):
        url = f"{self.url}/api/v1/search"
        params = [
            ("query", query),
            *(("indexerIds", indexer_id) for indexer_id in settings.PROWLARR_INDEXERS),
            ("type", "search"),
        ]
        async with self.session.get(
            url,
            params=params,
            headers={"X-Api-Key": settings.PROWLARR_API_KEY},
            timeout=INDEXER_TIMEOUT,
        ) as response:
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status}")
            data = await response.json()
            if not isinstance(data, list):
                raise ValueError("response payload is not a list")
            return data

    async def scrape(self, request: ScrapeRequest):
        if not settings.PROWLARR_INDEXERS:
            try:
                await asyncio.wait_for(
                    indexer_manager.prowlarr_initialized.wait(),
                    timeout=settings.INDEXER_MANAGER_WAIT_TIMEOUT,
                )
            except TimeoutError:
                logger.warning(
                    "Timed out waiting for Prowlarr indexers; skipping scrape."
                )
                return []

        if not settings.PROWLARR_INDEXERS:
            logger.warning("No Prowlarr indexers available, skipping scrape.")
            return []

        torrents: list[ScrapeResult] = []
        seen: set[str] = set()

        queries = request.title_queries(include_episode_variants=True)

        try:
            responses = await gather_with_error_logging(
                (
                    f"Prowlarr query {query!r} ({self.url})",
                    self._fetch_search_results(query),
                )
                for query in queries
            )
            all_results = []
            for response in responses:
                all_results.extend(response)

            torrent_tasks = []
            for result in all_results:
                if not isinstance(result, dict):
                    continue
                info_url = result.get("infoUrl")
                if not isinstance(info_url, str) or not info_url or info_url in seen:
                    continue

                seen.add(info_url)
                torrent_tasks.append(
                    (
                        f"Prowlarr result {info_url!r}",
                        self.process_torrent(
                            result, request.media_only_id, request.season
                        ),
                    )
                )

            processed_torrents = await gather_with_error_logging(torrent_tasks)
            for sublist in processed_torrents:
                for t in sublist:
                    if isinstance(t, dict) and t.get("infoHash"):
                        torrents.append(t)

        except Exception as e:
            logger.warning(
                f"Exception while getting torrents for {request.title} with Prowlarr: {e}"
            )

        return deduplicate_torrents(torrents)
