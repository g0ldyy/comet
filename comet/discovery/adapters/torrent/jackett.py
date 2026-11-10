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
    active_jackett_indexers,
    decode_indexer_json,
    indexer_manager,
    is_private_indexer_type,
    is_private_jackett_indexer,
)

_MAX_CONCURRENT_REQUESTS = 16


class JackettScraper(TorrentDiscoveryAdapter):
    url_setting = "JACKETT_URL"

    async def process_torrent(self, result: dict, media_id: str, season: int):
        return await resolve_indexer_release(
            self.url,
            title=result["Title"],
            seeders=result["Seeders"],
            size=result["Size"],
            tracker=result["Tracker"],
            is_private=is_private_indexer_type(result.get("TrackerType"))
            or is_private_jackett_indexer(result.get("TrackerId")),
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
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
            allow_redirects=False,
        ) as response:
            if not is_success_status(response.status):
                raise RuntimeError(f"HTTP {response.status}")
            data = decode_indexer_json(await response.read())
            return data.get("Results", [])

    async def scrape(self, request: ScrapeRequest):
        indexers = active_jackett_indexers()
        if not indexers:
            await indexer_manager.jackett_initialized.wait()
            indexers = active_jackett_indexers()

        if not indexers:
            return []
        queries = request.title_queries(include_episode_variants=True)

        result_sets = await gather_concurrently(
            (
                self.fetch_jackett_results(indexer, query)
                for query in queries
                for indexer in indexers
            ),
            limit=_MAX_CONCURRENT_REQUESTS,
        )
        processed = await gather_concurrently(
            (
                self.process_torrent(result, request.media_only_id, request.season)
                for result_set in result_sets
                for result in result_set
            ),
            preserve_successes=True,
            limit=_MAX_CONCURRENT_REQUESTS,
        )
        return deduplicate_torrents(
            [torrent for torrents in processed for torrent in torrents]
        )
