from comet.core.models import settings
from comet.core.provider_json import is_success_status
from comet.discovery.indexer_release import resolve_indexer_release
from comet.discovery.torrent_base import (
    TorrentDiscoveryAdapter,
    deduplicate_torrents,
    gather_concurrently,
)
from comet.discovery.torrent_models import ScrapeRequest
from comet.services.indexer_manager import (
    active_prowlarr_indexers,
    decode_indexer_json,
    indexer_manager,
    is_private_prowlarr_indexer,
)

_MAX_CONCURRENT_REQUESTS = 16


class ProwlarrScraper(TorrentDiscoveryAdapter):
    url_setting = "PROWLARR_URL"

    async def process_torrent(self, result: dict, media_id: str, season: int):
        guid = result.get("guid")
        return await resolve_indexer_release(
            self.url,
            title=result["title"],
            seeders=result["seeders"],
            size=result["size"],
            tracker=result["indexer"],
            is_private=is_private_prowlarr_indexer(result.get("indexerId")),
            info_hash=result.get("infoHash"),
            magnet_url=guid if guid and guid.startswith("magnet:") else None,
            download_url=result.get("downloadUrl"),
            grab_known_hash=settings.PROWLARR_GRAB_KNOWN_HASHES,
            media_id=media_id,
            season=season,
        )

    async def _fetch_search_results(self, query):
        indexers = active_prowlarr_indexers()
        url = f"{self.url}/api/v1/search"
        params = [
            ("query", query),
            *(("indexerIds", indexer_id) for indexer_id in indexers),
            ("type", "search"),
        ]
        async with self.session.get(
            url,
            params=params,
            headers={
                "X-Api-Key": settings.PROWLARR_API_KEY,
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
            allow_redirects=False,
        ) as response:
            if not is_success_status(response.status):
                raise RuntimeError(f"HTTP {response.status}")
            return decode_indexer_json(await response.read())

    async def scrape(self, request: ScrapeRequest):
        indexers = active_prowlarr_indexers()
        if not indexers:
            await indexer_manager.prowlarr_initialized.wait()
            indexers = active_prowlarr_indexers()

        if not indexers:
            return []

        queries = request.title_queries(include_episode_variants=True)

        responses = await gather_concurrently(
            (self._fetch_search_results(query) for query in queries),
            limit=_MAX_CONCURRENT_REQUESTS,
        )
        processed = await gather_concurrently(
            (
                self.process_torrent(result, request.media_only_id, request.season)
                for response in responses
                for result in response
            ),
            preserve_successes=True,
            limit=_MAX_CONCURRENT_REQUESTS,
        )
        return deduplicate_torrents(
            [torrent for torrents in processed for torrent in torrents]
        )
