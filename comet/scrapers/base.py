import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Iterable
from typing import TypeVar

from comet.core.logger import logger
from comet.scrapers.models import ScrapeRequest
from comet.utils.network_manager import AsyncClientWrapper

T = TypeVar("T")


def deduplicate_torrents(torrents: list[dict]) -> list[dict]:
    """Keep the first occurrence of each torrent file across title queries."""

    unique = []
    seen = set()
    for torrent in torrents:
        info_hash = torrent.get("infoHash") if isinstance(torrent, dict) else None
        if not isinstance(info_hash, str):
            unique.append(torrent)
            continue
        identity = (
            info_hash.lower(),
            torrent.get("fileIndex"),
        )
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(torrent)
    return unique


async def gather_with_error_logging[T](
    tasks: Iterable[tuple[str, Awaitable[T]]],
) -> list[T]:
    """Run independent tasks concurrently while preserving successful results."""

    entries = tuple(tasks)
    if not entries:
        return []

    results = await asyncio.gather(
        *(task for _, task in entries),
        return_exceptions=True,
    )
    successful = []
    for (context, _), result in zip(entries, results, strict=True):
        if isinstance(result, BaseException):
            if not isinstance(result, Exception):
                raise result
            logger.opt(depth=1).warning(
                f"{context} failed: {type(result).__name__}: {result}"
            )
            continue
        successful.append(result)
    return successful


class BaseScraper(ABC):
    impersonate: str | None = None

    def __init__(self, manager, session: AsyncClientWrapper, url: str | None = None):
        self.manager = manager
        self.session = session
        self.url = url

    @abstractmethod
    async def scrape(self, request: ScrapeRequest):
        pass
