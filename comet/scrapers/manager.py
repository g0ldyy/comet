import asyncio
import importlib
import inspect
import os
import pkgutil
from typing import Dict

from comet.core.logger import logger
from comet.core.models import settings
from comet.scrapers.base import BaseScraper
from comet.scrapers.models import ScrapeRequest
from comet.services.anime import anime_mapper
from comet.utils.network_manager import network_manager
from comet.utils.parsing import (associate_urls_credentials,
                                 parse_url_scrape_mode,
                                 url_mode_matches_context)

ANIME_ONLY_SETTING_BY_SCRAPER = {
    "NyaaScraper": "NYAA_ANIME_ONLY",
    "AnimeToshoScraper": "ANIMETOSHO_ANIME_ONLY",
    "SeaDexScraper": "SEADEX_ANIME_ONLY",
    "NekoBTScraper": "NEKOBT_ANIME_ONLY",
}


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

    @staticmethod
    def _resolve_url_for_context(url: str, context: str):
        parsed_url, mode = parse_url_scrape_mode(url)
        if not url_mode_matches_context(mode, context):
            return None
        return parsed_url

    async def scrape_all(self, request: ScrapeRequest):
        tasks = []
        is_anime_content = None
        for scraper_name, scraper_class in self.scrapers.items():
            # Determine if scraper should be enabled
            # Convention: Scraper class name "NyaaScraper" -> settings.SCRAPE_NYAA
            scraper_name_clean = scraper_name.replace("Scraper", "")
            setting_name = scraper_name_clean.upper()
            setting_key = f"SCRAPE_{setting_name}"

            if hasattr(settings, setting_key):
                if not settings.is_scraper_enabled(
                    getattr(settings, setting_key), request.context
                ):
                    continue
            else:
                continue

            anime_only_setting = ANIME_ONLY_SETTING_BY_SCRAPER.get(scraper_name)
            if anime_only_setting and getattr(settings, anime_only_setting, False):
                if is_anime_content is None:
                    is_anime_content = anime_mapper.is_anime_content(
                        request.media_id, request.media_only_id
                    )
                if not is_anime_content:
                    continue

            # Get client wrapper
            client = network_manager.get_client(
                scraper_name=scraper_name_clean, impersonate=scraper_class.impersonate
            )

            if scraper_name == "MediaFusionScraper":
                url_credentials_pairs = associate_urls_credentials(
                    settings.MEDIAFUSION_URL, settings.MEDIAFUSION_API_PASSWORD
                )
                if url_credentials_pairs:
                    active_instance_count = 0
                    for url, password in url_credentials_pairs:
                        parsed_url = self._resolve_url_for_context(url, request.context)
                        if parsed_url is None:
                            continue
                        active_instance_count += 1
                        scraper = scraper_class(self, client, parsed_url, password)
                        tasks.append(
                            self._scrape_wrapper(
                                f"{scraper_name_clean} #{active_instance_count}",
                                scraper,
                                request,
                            )
                        )

            elif scraper_name == "AiostreamsScraper":
                url_credentials_pairs = associate_urls_credentials(
                    settings.AIOSTREAMS_URL, settings.AIOSTREAMS_USER_UUID_AND_PASSWORD
                )
                if url_credentials_pairs:
                    active_instance_count = 0
                    for url, credentials in url_credentials_pairs:
                        parsed_url = self._resolve_url_for_context(url, request.context)
                        if parsed_url is None:
                            continue
                        active_instance_count += 1
                        scraper = scraper_class(self, client, parsed_url, credentials)
                        tasks.append(
                            self._scrape_wrapper(
                                f"{scraper_name_clean} #{active_instance_count}",
                                scraper,
                                request,
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
                    active_instance_count = 0
                    for url in urls:
                        parsed_url = self._resolve_url_for_context(url, request.context)
                        if parsed_url is None:
                            continue
                        active_instance_count += 1
                        scraper = scraper_class(self, client, parsed_url)
                        tasks.append(
                            self._scrape_wrapper(
                                f"{scraper_name_clean} #{active_instance_count}",
                                scraper,
                                request,
                            )
                        )
                else:
                    scraper = scraper_class(self, client)
                    tasks.append(
                        self._scrape_wrapper(scraper_name_clean, scraper, request)
                    )

        for future in asyncio.as_completed(tasks):
            try:
                yield await future
            except Exception as e:
                logger.error(
                    f"Error during scraping: {e}"
                )  # todo: better error handling


scraper_manager = ScraperManager()
