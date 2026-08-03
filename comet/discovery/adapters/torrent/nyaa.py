import asyncio
import html
import re
from urllib.parse import quote_plus

from comet.core.models import settings
from comet.core.provider_json import is_success_status
from comet.discovery.torrent_base import (
    TorrentDiscoveryAdapter,
    deduplicate_torrents,
    gather_concurrently,
)
from comet.discovery.torrent_models import ScrapeRequest
from comet.services.torrent_manager import extract_trackers_from_magnet
from comet.utils.formatting import normalize_info_hash, size_to_bytes

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
_MAX_PAGES = 100


def extract_torrent_data(html_content: str):
    torrents = []
    for row in ROW_PATTERN.findall(html_content):
        magnet_match = MAGNET_PATTERN.search(row)
        size_match = SIZE_PATTERN.search(row)
        seeders_match = SEEDERS_PATTERN.search(row)
        title_match = TITLE_PATTERN.search(row)
        if magnet_match is None and title_match is None:
            continue
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
        info_hash = normalize_info_hash(info_hash_match.group(1))
        torrents.append(
            {
                "title": html.unescape(title_match.group(1)),
                "infoHash": info_hash,
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
            if not is_success_status(response.status):
                raise RuntimeError(f"Nyaa returned HTTP {response.status}")

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

    async with semaphore, session.get(first_page_url) as response:
        if not is_success_status(response.status):
            raise RuntimeError(f"Nyaa returned HTTP {response.status}")

        first_page_text = await response.text()

    first_page_torrents = extract_torrent_data(first_page_text)
    all_torrents.extend(first_page_torrents)

    last_page_matches = PAGE_PATTERN.findall(first_page_text)
    if len(last_page_matches) == 0:
        return all_torrents

    last_page_number = int(last_page_matches[0])
    if last_page_number > _MAX_PAGES:
        raise ValueError("Nyaa pagination exceeds the page limit")

    if last_page_number > 1:
        page_results = await gather_concurrently(
            scrape_nyaa_page(session, semaphore, query, page_number)
            for page_number in range(2, last_page_number + 1)
        )
        for result in page_results:
            all_torrents.extend(result)

    return all_torrents


class NyaaScraper(TorrentDiscoveryAdapter):
    anime_only_setting = "NYAA_ANIME_ONLY"
    impersonate = "chrome"

    def __init__(self, manager, session):
        super().__init__(manager, session)

    async def scrape(self, request: ScrapeRequest):
        torrents = []
        semaphore = asyncio.Semaphore(settings.NYAA_MAX_CONCURRENT_PAGES)
        results = await gather_concurrently(
            get_all_nyaa_pages(self.session, query, semaphore)
            for query in request.scoped_query_titles()
        )
        for result in results:
            torrents.extend(result)

        return deduplicate_torrents(torrents)
