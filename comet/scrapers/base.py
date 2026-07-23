from abc import ABC, abstractmethod

from comet.scrapers.models import ScrapeRequest
from comet.utils.network_manager import AsyncClientWrapper


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


class BaseScraper(ABC):
    impersonate: str | None = None

    def __init__(self, manager, session: AsyncClientWrapper, url: str = None):
        self.manager = manager
        self.session = session
        self.url = url

    @abstractmethod
    async def scrape(self, request: ScrapeRequest):
        pass
