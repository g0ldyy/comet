import asyncio
from itertools import batched

from comet.core.constants import indexer_timeout
from comet.core.models import settings
from comet.core.provider_json import is_success_status
from comet.discovery.torrent_base import (
    TorrentDiscoveryAdapter,
    deduplicate_torrents,
    gather_concurrently,
)
from comet.discovery.torrent_models import ScrapeRequest, ScrapeResult
from comet.services.indexer_manager import (
    MAX_INDEXER_RESPONSE_BYTES,
    active_prowlarr_indexers,
    decode_indexer_json,
    indexer_manager,
)
from comet.services.torrent_manager import (
    add_torrent_queue,
    download_torrent,
    extract_torrent_metadata,
    extract_trackers_from_magnet,
)
from comet.usenet.outbound import configured_http_origin

_MAX_AGGREGATE_RESULTS = 10_000
_REQUEST_BATCH_SIZE = 16


class ProwlarrScraper(TorrentDiscoveryAdapter):
    url_setting = "PROWLARR_URL"
    startup_timeout_setting = "INDEXER_MANAGER_WAIT_TIMEOUT"

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
                self.session,
                result["downloadUrl"],
                allowed_private_origins=frozenset({configured_http_origin(self.url)}),
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
            guid = result.get("guid")
            if isinstance(guid, str) and guid.startswith("magnet:"):
                base_torrent["sources"] = extract_trackers_from_magnet(guid)

                await add_torrent_queue.add_torrent(
                    guid,
                    base_torrent["seeders"],
                    base_torrent["tracker"],
                    media_id,
                    season,
                    base_torrent["infoHash"],
                )

            torrents.append(base_torrent)

        return torrents

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
            timeout=indexer_timeout(),
            maximum_body_bytes=MAX_INDEXER_RESPONSE_BYTES,
        ) as response:
            if not is_success_status(response.status):
                raise RuntimeError(f"HTTP {response.status}")
            data = decode_indexer_json(await response.read())
            if not isinstance(data, list) or len(data) > 10_000:
                raise ValueError("response payload is not a list")
            return data

    async def scrape(self, request: ScrapeRequest):
        indexers = active_prowlarr_indexers()
        if not indexers:
            await asyncio.wait_for(
                indexer_manager.prowlarr_initialized.wait(),
                timeout=settings.INDEXER_MANAGER_WAIT_TIMEOUT,
            )
            indexers = active_prowlarr_indexers()

        if not indexers:
            return []

        torrents: list[ScrapeResult] = []

        queries = request.title_queries(include_episode_variants=True)

        torrent_results = []
        inspected_results = 0
        requests = (self._fetch_search_results(query) for query in queries)
        for request_batch in batched(requests, _REQUEST_BATCH_SIZE):
            responses = await gather_concurrently(request_batch)
            for response in responses:
                remaining = _MAX_AGGREGATE_RESULTS - inspected_results
                if remaining <= 0:
                    break
                inspected_results += min(len(response), remaining)
                torrent_results.extend(response[:remaining])
            if inspected_results >= _MAX_AGGREGATE_RESULTS:
                break

        for result_batch in batched(torrent_results, _REQUEST_BATCH_SIZE):
            processed_torrents = await gather_concurrently(
                (
                    self.process_torrent(
                        result,
                        request.media_only_id,
                        request.season,
                    )
                    for result in result_batch
                ),
                preserve_successes=True,
            )
            for sublist in processed_torrents:
                torrents.extend(sublist)

        return deduplicate_torrents(torrents)
