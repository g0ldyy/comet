import asyncio
import re

import aiohttp
from curl_cffi import requests

from comet.core.logger import log_scraper_error, logger
from comet.core.models import settings
from comet.scrapers.base import BaseScraper
from comet.scrapers.models import ScrapeRequest
from comet.services.torrent_manager import extract_trackers_from_magnet
from comet.utils.formatting import size_to_bytes

PAGE_PATTERN = re.compile(r'(\d+)(?=">\d+<\/a><\/li><li class="next">)')
MAGNET_PATTERN = re.compile(r'href="(magnet:[^"]+)"')
SIZE_PATTERN = re.compile(r'<td class="text-center">([\d.]+ (?:KiB|MiB|GiB|TiB))</td>')
SEEDERS_PATTERN = re.compile(
    r'<td class="text-center">(\d+)</td>\s*<td class="text-center">(\d+)</td>\s*<td class="text-center">(\d+)</td>'
)
TITLE_PATTERN = re.compile(r'href="/view/\d+" title="([^"]+)"')
INFO_HASH_PATTERN = re.compile(r"btih:([a-fA-F0-9]{40}|[a-zA-Z0-9]{32})")


def extract_torrent_data(html_content: str):
    torrents = []

    magnet_links = MAGNET_PATTERN.findall(html_content)

    sizes = SIZE_PATTERN.findall(html_content)

    seeders_data = SEEDERS_PATTERN.findall(html_content)
    seeders = [int(match[0]) for match in seeders_data]

    titles = TITLE_PATTERN.findall(html_content)

    for i in range(len(magnet_links)):
        magnet = magnet_links[i]
        info_hash = INFO_HASH_PATTERN.search(magnet).group(1)

        size_str = sizes[i]
        try:
            size_bytes = size_to_bytes(size_str.replace("iB", "B"))
        except Exception:
            size_bytes = 0

        torrents.append(
            {
                "title": titles[i],
                "infoHash": info_hash,
                "fileIndex": None,
                "seeders": seeders[i],
                "size": size_bytes,
                "tracker": "Nyaa",
                "sources": extract_trackers_from_magnet(magnet),
            }
        )

    return torrents


async def scrape_nyaa_page(
    session: requests.AsyncSession, semaphore: asyncio.Semaphore, query: str, page: int
):
    async with semaphore:
        url = f"https://nyaa.si/?q={query}"
        if page > 1:
            url += f"&p={page}"

        response = await session.get(url)
        if response.status_code != 200:
            logger.warning(
                f"Failed to scrape Nyaa page {page} (consider reducing NYAA_MAX_CONCURRENT_PAGES): HTTP {response.status_code}"
            )
            return []

        html_content = response.text
        return extract_torrent_data(html_content)


async def get_all_nyaa_pages(session: requests.AsyncSession, query: str):
    all_torrents = []

    max_concurrent = settings.NYAA_MAX_CONCURRENT_PAGES
    semaphore = asyncio.Semaphore(max_concurrent)

    first_page_url = f"https://nyaa.si/?q={query}"
    response = await session.get(first_page_url)
    if response.status_code != 200:
        logger.warning(f"Failed to scrape Nyaa page 1: HTTP {response.status_code}")
        return []

    first_page_text = response.text

    first_page_torrents = extract_torrent_data(first_page_text)
    all_torrents.extend(first_page_torrents)

    last_page_matches = PAGE_PATTERN.findall(first_page_text)
    if len(last_page_matches) == 0:
        return all_torrents

    last_page_number = int(last_page_matches[0])

    if last_page_number > 1:
        tasks = []
        for page_number in range(2, last_page_number + 1):
            tasks.append(scrape_nyaa_page(session, semaphore, query, page_number))

        page_results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in page_results:
            if isinstance(result, list):
                all_torrents.extend(result)

    return all_torrents


class NyaaScraper(BaseScraper):
    def __init__(self, manager, session: aiohttp.ClientSession):
        super().__init__(manager, session)

    async def scrape(self, request: ScrapeRequest):
        torrents = []

        try:
            async with requests.AsyncSession() as session:
                query = request.title

                all_torrents = await get_all_nyaa_pages(session, query)
                torrents.extend(all_torrents)

        except Exception as e:
            log_scraper_error("Nyaa", "https://nyaa.si", request.media_id, e)

        return torrents
