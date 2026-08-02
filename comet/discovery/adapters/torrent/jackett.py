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
    active_jackett_indexers,
    indexer_manager,
    read_indexer_json,
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


class JackettScraper(TorrentDiscoveryAdapter):
    url_setting = "JACKETT_URL"
    startup_timeout_setting = "INDEXER_MANAGER_WAIT_TIMEOUT"

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
                self.session,
                result["Link"],
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

        if result.get("InfoHash"):
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
            timeout=indexer_timeout(),
        ) as response:
            if not is_success_status(response.status):
                raise RuntimeError(f"HTTP {response.status}")
            data = await read_indexer_json(response)
            if (
                not isinstance(data, dict)
                or not isinstance(data.get("Results"), list)
                or len(data["Results"]) > 10_000
            ):
                raise ValueError("response payload is missing a results list")
            return data["Results"]

    async def scrape(self, request: ScrapeRequest):
        indexers = active_jackett_indexers()
        if not indexers:
            await asyncio.wait_for(
                indexer_manager.jackett_initialized.wait(),
                timeout=settings.INDEXER_MANAGER_WAIT_TIMEOUT,
            )
            indexers = active_jackett_indexers()

        if not indexers:
            return []
        torrents: list[ScrapeResult] = []

        queries = request.title_queries(include_episode_variants=True)

        torrent_results = []
        inspected_results = 0
        requests = (
            self.fetch_jackett_results(indexer, query)
            for query in queries
            for indexer in indexers
        )
        for request_batch in batched(requests, _REQUEST_BATCH_SIZE):
            result_sets = await gather_concurrently(request_batch)
            for result_set in result_sets:
                remaining = _MAX_AGGREGATE_RESULTS - inspected_results
                if remaining <= 0:
                    break
                inspected_results += min(len(result_set), remaining)
                torrent_results.extend(result_set[:remaining])
            if inspected_results >= _MAX_AGGREGATE_RESULTS:
                break

        for result_batch in batched(torrent_results, _REQUEST_BATCH_SIZE):
            processed_torrents = await gather_concurrently(
                self.process_torrent(
                    result,
                    request.media_only_id,
                    request.season,
                )
                for result in result_batch
            )
            for sublist in processed_torrents:
                torrents.extend(sublist)

        return deduplicate_torrents(torrents)
