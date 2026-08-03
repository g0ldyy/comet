import asyncio
from urllib.parse import quote_plus

from comet.core.models import settings
from comet.core.provider_json import is_success_status
from comet.discovery.adapters.newznab import (
    NewznabError,
    _read_bounded,
    parse_newznab_feed,
)
from comet.discovery.torrent_base import (
    TorrentDiscoveryAdapter,
    deduplicate_torrents,
    gather_concurrently,
)
from comet.discovery.torrent_models import ScrapeRequest
from comet.services.torrent_manager import extract_trackers_from_magnet

_MAX_RESULTS_PER_QUERY = 1_000


class AnimeToshoScraper(TorrentDiscoveryAdapter):
    anime_only_setting = "ANIMETOSHO_ANIME_ONLY"

    def __init__(self, manager, session):
        super().__init__(manager, session)

    def parse_items(self, items):
        torrents = []
        for item in items:
            title = item.fields.get("title")
            attributes = item.attributes
            raw_size = attributes.get("size")
            size = int(raw_size) if raw_size else None
            if size == 0:
                size = None
            info_hash = attributes.get("infohash")
            raw_seeders = attributes.get("seeders")
            seeders = int(raw_seeders) if raw_seeders else None
            magnet = attributes.get("magneturl")
            if not title or info_hash is None:
                continue
            torrents.append(
                {
                    "title": title,
                    "infoHash": info_hash.lower(),
                    "fileIndex": None,
                    "seeders": seeders,
                    "size": size,
                    "tracker": "AnimeTosho",
                    "sources": (
                        extract_trackers_from_magnet(magnet)
                        if magnet is not None
                        else []
                    ),
                }
            )
        return torrents

    async def _scrape_page(self, query, offset, limit):
        async with self.session.get(
            "https://feed.animetosho.org/api"
            f"?t=search&q={quote_plus(query)}&offset={offset}&limit={limit}"
        ) as response:
            if response.status == 429:
                raise NewznabError("provider_limit_exhausted")
            if response.status >= 500:
                raise NewznabError("provider_unavailable")
            if not is_success_status(response.status):
                raise NewznabError("provider_response_invalid")

            content = await _read_bounded(response, 2 * 1024 * 1024)
            if not content.strip():
                return [], 0

            feed_items, total = parse_newznab_feed(content)
            items = self.parse_items(feed_items)
            return items, total or 0

    async def scrape_page(self, query, offset, limit, semaphore=None):
        if semaphore is None:
            return await self._scrape_page(query, offset, limit)
        async with semaphore:
            return await self._scrape_page(query, offset, limit)

    async def _scrape_query(self, query: str, semaphore: asyncio.Semaphore):
        torrents = []
        page_size = 150

        initial_items, total = await self.scrape_page(
            query,
            0,
            page_size,
            semaphore,
        )
        torrents.extend(initial_items)

        target_total = min(total, _MAX_RESULTS_PER_QUERY)
        if target_total > page_size:
            batch_size = settings.ANIMETOSHO_MAX_CONCURRENT_PAGES
            current_offset = page_size

            while current_offset < target_total:
                tasks = []
                for _ in range(batch_size):
                    if current_offset >= target_total:
                        break

                    limit = min(page_size, target_total - current_offset)
                    tasks.append(
                        self.scrape_page(query, current_offset, limit, semaphore)
                    )
                    current_offset += limit

                if tasks:
                    results = await gather_concurrently(tasks)
                    for batch_items, _ in results:
                        torrents.extend(batch_items)

        return torrents

    async def scrape(self, request: ScrapeRequest):
        semaphore = asyncio.Semaphore(settings.ANIMETOSHO_MAX_CONCURRENT_PAGES)
        results = await gather_concurrently(
            self._scrape_query(query, semaphore) for query in request.query_titles
        )
        return deduplicate_torrents(
            [torrent for torrents in results for torrent in torrents]
        )
