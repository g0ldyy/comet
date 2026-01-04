from abc import ABC, abstractmethod

from comet.scrapers.models import ScrapeRequest
from comet.utils.network_manager import AsyncClientWrapper


class BaseScraper(ABC):
    impersonate: str | None = None

    def __init__(self, manager, session: AsyncClientWrapper, url: str = None):
        self.manager = manager
        self.session = session
        self.url = url

    @abstractmethod
    async def scrape(self, request: ScrapeRequest):
        pass
