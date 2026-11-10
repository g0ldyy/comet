import asyncio

from comet.core.constants import INDEXER_TIMEOUT
from comet.core.logger import logger
from comet.core.models import settings
from comet.scrapers.base import (
    BaseScraper,
    deduplicate_torrents,
    gather_with_error_logging,
)
from comet.scrapers.helpers.indexer import resolve_indexer_release
from comet.scrapers.models import ScrapeRequest, ScrapeResult
from comet.services.indexer_manager import indexer_manager


class JackettScraper(BaseScraper):
    async def process_torrent(self, result: dict, media_id: str, season: int):
        return await resolve_indexer_release(
            self.session,
            title=result["Title"],
            seeders=result["Seeders"],
            size=result["Size"],
            tracker=result["Tracker"],
            info_hash=result.get("InfoHash"),
            magnet_url=result.get("MagnetUri"),
            download_url=result.get("Link"),
            grab_known_hash=settings.JACKETT_GRAB_KNOWN_HASHES,
            media_id=media_id,
            season=season,
        )

    async def fetch_jackett_results(self, indexer: str, query: str):
        async with self.session.get(
            f"{self.url}/api/v2.0/indexers/all/results",
            params={
                "apikey": settings.JACKETT_API_KEY,
                "Query": query,
                "Tracker[]": indexer,
            },
            timeout=INDEXER_TIMEOUT,
        ) as response:
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status}")
            data = await response.json()
            if not isinstance(data, dict) or not isinstance(data.get("Results"), list):
                raise ValueError("response payload is missing a results list")
            return data["Results"]

    async def scrape(self, request: ScrapeRequest):
        if not settings.JACKETT_INDEXERS:
            try:
                await asyncio.wait_for(
                    indexer_manager.jackett_initialized.wait(),
                    timeout=settings.INDEXER_MANAGER_WAIT_TIMEOUT,
                )
            except TimeoutError:
                logger.warning(
                    "Timed out waiting for Jackett indexers; skipping scrape."
                )
                return []

        if not settings.JACKETT_INDEXERS:
            logger.warning("No Jackett indexers available, skipping scrape.")
            return []
        torrents: list[ScrapeResult] = []
        seen: set[str] = set()

        queries = request.title_queries(include_episode_variants=True)

        try:
            all_results = await gather_with_error_logging(
                (
                    f"Jackett query {query!r} via indexer {indexer!r}",
                    self.fetch_jackett_results(indexer, query),
                )
                for query in queries
                for indexer in settings.JACKETT_INDEXERS
            )

            torrent_tasks = []
            for result_set in all_results:
                for result in result_set:
                    if not isinstance(result, dict):
                        continue
                    details = result.get("Details")
                    if not isinstance(details, str) or not details or details in seen:
                        continue

                    seen.add(details)
                    torrent_tasks.append(
                        (
                            f"Jackett result {details!r}",
                            self.process_torrent(
                                result,
                                request.media_only_id,
                                request.season,
                            ),
                        )
                    )

            processed_torrents = await gather_with_error_logging(torrent_tasks)
            for sublist in processed_torrents:
                for torrent in sublist:
                    if isinstance(torrent, dict) and torrent.get("infoHash"):
                        torrents.append(torrent)
        except Exception as e:
            logger.warning(
                f"Exception while getting torrents for {request.title} with Jackett: {e}"
            )

        return deduplicate_torrents(torrents)
