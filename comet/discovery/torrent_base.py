import asyncio
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable
from typing import TypeVar

from comet.core.sources import TransportKind
from comet.discovery.models import DiscoveryBatch, DiscoveryContext, MediaQuery
from comet.discovery.torrent_models import ScrapeRequest
from comet.discovery.torrent_repository import (
    torrent_candidate_from_scrape_result,
)
from comet.observability import metrics
from comet.utils.network_manager import AsyncClientWrapper

T = TypeVar("T")


def deduplicate_torrents(torrents: list[dict]) -> list[dict]:
    """Keep the first occurrence of each torrent file across title queries."""

    unique = []
    seen = set()
    for torrent in torrents:
        identity = (
            torrent["infoHash"].lower(),
            torrent.get("fileIndex"),
        )
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(torrent)
    return unique


async def gather_concurrently[T](
    tasks: Iterable[Awaitable[T]],
    *,
    preserve_successes: bool = False,
) -> list[T]:
    """Run siblings concurrently, optionally keeping partial processed results."""
    results = await asyncio.gather(*tasks, return_exceptions=True)
    successful = []
    failures = []
    for result in results:
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, Exception):
            failures.append(result)
        else:
            successful.append(result)
    if failures and (not preserve_successes or not successful):
        raise failures[0]
    return successful


def parse_valid_items[T, R](items: Iterable[T], parser: Callable[[T], R]) -> list[R]:
    """Parse provider siblings independently and discard only malformed items."""
    parsed = []
    for item in items:
        try:
            parsed.append(parser(item))
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    return parsed


class TorrentDiscoveryAdapter(ABC):
    url_setting: str | None = None
    credential_setting: str | None = None
    anime_only_setting: str | None = None
    startup_timeout_setting: str | None = None
    impersonate: str | None = None

    def __init__(self, manager, session: AsyncClientWrapper, url: str | None = None):
        del manager
        self.session = session
        self.url = url
        self.discovery_name = type(self).__name__.removesuffix("Scraper")
        self.discovery_timeout: float | None = None

    async def search(
        self,
        query: MediaQuery,
        context: DiscoveryContext,
    ) -> DiscoveryBatch:
        """Expose every existing torrent source through DiscoveryAdapter."""
        if TransportKind.BITTORRENT.value not in context.branches:
            return DiscoveryBatch()
        request = ScrapeRequest(
            media_type=query.media_type,
            media_id=query.request_media_id or query.media_id,
            media_only_id=query.media_id,
            title=query.title,
            year=query.year,
            year_end=query.year_end,
            season=query.season,
            episode=query.episode,
            context=context.work_class,
            search_titles=query.search_titles or query.title_aliases,
        )
        started_at = time.perf_counter()
        outcome = "success"
        result_count = 0
        try:
            if self.discovery_timeout is None:
                raw_results = await self.scrape(request)
            else:
                async with asyncio.timeout(self.discovery_timeout):
                    raw_results = await self.scrape(request)
            result_count = len(raw_results)
        except TimeoutError:
            outcome = "timeout"
            raise
        except BaseException:
            outcome = "error"
            raise
        finally:
            metrics.observe_scraper(
                self.discovery_name,
                request.context.value,
                outcome,
                time.perf_counter() - started_at,
                result_count,
            )
        return DiscoveryBatch(
            tuple(
                torrent_candidate_from_scrape_result(result, query)
                for result in raw_results
            ),
            coverage=frozenset({TransportKind.BITTORRENT.value}),
        )

    @abstractmethod
    async def scrape(self, request: ScrapeRequest):
        pass
