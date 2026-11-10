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


class ProwlarrScraper(BaseScraper):
    async def process_torrent(self, result: dict, media_id: str, season: int):
        guid = result.get("guid")
        return await resolve_indexer_release(
            self.session,
            title=result["title"],
            seeders=result["seeders"],
            size=result["size"],
            tracker=result["indexer"],
            info_hash=result.get("infoHash"),
            magnet_url=guid if guid and guid.startswith("magnet:") else None,
            download_url=result.get("downloadUrl"),
            grab_known_hash=settings.PROWLARR_GRAB_KNOWN_HASHES,
            media_id=media_id,
            season=season,
        )

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
