from abc import ABC, abstractmethod

import aiohttp

from comet.scrapers.models import ScrapeRequest


class BaseScraper(ABC):
    def __init__(self, manager, session: aiohttp.ClientSession, url: str = None):
        self.manager = manager
        self.session = session
        self.url = url

    @abstractmethod
    async def scrape(self, request: ScrapeRequest):
        pass
