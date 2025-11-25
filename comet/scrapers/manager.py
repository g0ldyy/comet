import asyncio
import importlib
import inspect
import os
import pkgutil
from typing import Dict

import aiohttp

from comet.core.logger import logger
from comet.core.models import settings
from comet.scrapers.base import BaseScraper
from comet.scrapers.models import ScrapeRequest
from comet.services.anime import anime_mapper
from comet.utils.parsing import associate_urls_credentials


class ScraperManager:
    def __init__(self):
        self.scrapers: Dict[str, BaseScraper] = {}
        self.discover_scrapers()

    def discover_scrapers(self):
        """
        Dynamically discover and load scraper classes from the scrapers directory.
        """
        package = "comet.scrapers"
        path = os.path.dirname(__file__)

        for _, name, _ in pkgutil.iter_modules([path]):
            if name in ["base", "manager", "models"]:  # Skip base, manager, and models
                continue

            module = importlib.import_module(f"{package}.{name}")

            # Find classes inheriting from BaseScraper
            for name, obj in inspect.getmembers(module):
                if (
                    inspect.isclass(obj)
                    and issubclass(obj, BaseScraper)
                    and obj is not BaseScraper
                ):
                    self.scrapers[obj.__name__] = obj

    async def _scrape_wrapper(
        self, name: str, scraper: BaseScraper, request: ScrapeRequest
    ):
        try:
            return name, await scraper.scrape(request)
        except Exception as e:
            logger.warning(f"Scraper {name} failed: {e}")  # todo: better error handling
            return name, []

    async def scrape_all(self, request: ScrapeRequest, session: aiohttp.ClientSession):
        tasks = []
        for scraper_name, scraper_class in self.scrapers.items():
            # Determine if scraper should be enabled
            # Convention: Scraper class name "NyaaScraper" -> settings.SCRAPE_NYAA
            scraper_name_clean = scraper_name.replace("Scraper", "")
            setting_name = scraper_name_clean.upper()
            setting_key = f"SCRAPE_{setting_name}"

            if scraper_name == "JackettScraper":
                if not (
                    settings.INDEXER_MANAGER_API_KEY
                    and settings.is_scraper_enabled(
                        settings.INDEXER_MANAGER_MODE, request.context
                    )
                    and settings.INDEXER_MANAGER_TYPE == "jackett"
                ):
                    continue
            elif scraper_name == "ProwlarrScraper":
                if not (
                    settings.INDEXER_MANAGER_API_KEY
                    and settings.is_scraper_enabled(
                        settings.INDEXER_MANAGER_MODE, request.context
                    )
                    and settings.INDEXER_MANAGER_TYPE == "prowlarr"
                ):
                    continue
            else:
                if hasattr(settings, setting_key):
                    if not settings.is_scraper_enabled(
                        getattr(settings, setting_key), request.context
                    ):
                        continue
                else:
                    logger.debug(
                        f"No {setting_key} found for {scraper_name_clean}, disabling"
                    )
                    continue

                if (
                    scraper_name == "NyaaScraper"
                    and settings.NYAA_ANIME_ONLY
                    and not anime_mapper.is_anime_content(
                        request.media_id, request.media_only_id
                    )
                ):
                    continue

            if scraper_name == "MediaFusionScraper":
                url_credentials_pairs = associate_urls_credentials(
                    settings.MEDIAFUSION_URL, settings.MEDIAFUSION_API_PASSWORD
                )
                if url_credentials_pairs:
                    for i, (url, password) in enumerate(url_credentials_pairs):
                        scraper = scraper_class(self, session, url, password)
                        tasks.append(
                            self._scrape_wrapper(
                                f"{scraper_name_clean} #{i+1}", scraper, request
                            )
                        )

            elif scraper_name == "AiostreamsScraper":
                url_credentials_pairs = associate_urls_credentials(
                    settings.AIOSTREAMS_URL, settings.AIOSTREAMS_USER_UUID_AND_PASSWORD
                )
                if url_credentials_pairs:
                    for i, (url, credentials) in enumerate(url_credentials_pairs):
                        scraper = scraper_class(self, session, url, credentials)
                        tasks.append(
                            self._scrape_wrapper(
                                f"{scraper_name_clean} #{i+1}", scraper, request
                            )
                        )

            else:
                url_setting_key = f"{setting_name}_URL"
                if scraper_name == "StremthruScraper":
                    url_setting_key = "STREMTHRU_SCRAPE_URL"

                urls = getattr(settings, url_setting_key, None)
                if isinstance(urls, str):
                    urls = [urls]

                if urls:
                    for i, url in enumerate(urls):
                        scraper = scraper_class(self, session, url)
                        tasks.append(
                            self._scrape_wrapper(
                                f"{scraper_name_clean} #{i+1}", scraper, request
                            )
                        )
                else:
                    scraper = scraper_class(self, session)
                    tasks.append(self._scrape_wrapper(scraper_name_clean, scraper, request))

        for future in asyncio.as_completed(tasks):
            try:
                yield await future
            except Exception as e:
                logger.error(
                    f"Error during scraping: {e}"
                )  # todo: better error handling


scraper_manager = ScraperManager()
