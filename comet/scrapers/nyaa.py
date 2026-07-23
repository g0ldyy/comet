import asyncio
import html
import re
from urllib.parse import quote_plus

from comet.core.logger import log_scraper_error, logger
from comet.core.models import settings
from comet.scrapers.base import BaseScraper, deduplicate_torrents
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
ROW_PATTERN = re.compile(r"<tr(?:\s[^>]*)?>.*?</tr>", re.IGNORECASE | re.DOTALL)

NYAA_BASE_URL = "https://nyaa.si"


def extract_torrent_data(html_content: str):
    torrents = []
    for row in ROW_PATTERN.findall(html_content):
        magnet_match = MAGNET_PATTERN.search(row)
        size_match = SIZE_PATTERN.search(row)
        seeders_match = SEEDERS_PATTERN.search(row)
        title_match = TITLE_PATTERN.search(row)
        if not magnet_match or not size_match or not seeders_match or not title_match:
            continue

        magnet = html.unescape(magnet_match.group(1))
        info_hash_match = INFO_HASH_PATTERN.search(magnet)
        if not info_hash_match:
            continue
        try:
            size_bytes = size_to_bytes(size_match.group(1).replace("iB", "B"))
            seeders = int(seeders_match.group(1))
        except (TypeError, ValueError):
            continue

        torrents.append(
            {
                "title": html.unescape(title_match.group(1)),
                "infoHash": info_hash_match.group(1),
                "fileIndex": None,
                "seeders": seeders,
                "size": size_bytes,
                "tracker": "Nyaa",
                "sources": extract_trackers_from_magnet(magnet),
            }
        )

    return torrents


async def scrape_nyaa_page(
    session, semaphore: asyncio.Semaphore, query: str, page: int
):
    async with semaphore:
        url = f"{NYAA_BASE_URL}/?q={quote_plus(query)}"
        if page > 1:
            url += f"&p={page}"

        async with session.get(url) as response:
            if response.status != 200:
                logger.warning(
                    f"Failed to scrape Nyaa page {page} (consider reducing NYAA_MAX_CONCURRENT_PAGES): HTTP {response.status}"
                )
                return []

            html_content = await response.text()
            return extract_torrent_data(html_content)


async def get_all_nyaa_pages(
    session,
    query: str,
    semaphore: asyncio.Semaphore | None = None,
):
    all_torrents = []

    if semaphore is None:
        semaphore = asyncio.Semaphore(settings.NYAA_MAX_CONCURRENT_PAGES)

    first_page_url = f"{NYAA_BASE_URL}/?q={quote_plus(query)}"

    async with semaphore:
        async with session.get(first_page_url) as response:
            if response.status != 200:
                logger.warning(f"Failed to scrape Nyaa page 1: HTTP {response.status}")
                return []

            first_page_text = await response.text()

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
    impersonate = "chrome"

    def __init__(self, manager, session):
        super().__init__(manager, session)

    async def scrape(self, request: ScrapeRequest):
        torrents = []

        try:
            semaphore = asyncio.Semaphore(settings.NYAA_MAX_CONCURRENT_PAGES)
            results = await asyncio.gather(
                *(
                    get_all_nyaa_pages(self.session, query, semaphore)
                    for query in request.query_titles
                ),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, list):
                    torrents.extend(result)

        except Exception as e:
            log_scraper_error("Nyaa", NYAA_BASE_URL, request.media_id, e)

        return deduplicate_torrents(torrents)
