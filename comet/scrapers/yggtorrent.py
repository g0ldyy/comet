import asyncio
import re
from urllib.parse import urlparse

import aiohttp
from curl_cffi import requests

from comet.core.logger import logger
from comet.core.models import settings
from comet.scrapers.base import BaseScraper
from comet.scrapers.models import ScrapeRequest
from comet.utils.formatting import size_to_bytes

YGG_URL = "https://www.yggtorrent.org"
TRACKER_URL = "http://tracker.p2p-world.net:8080"

LOGIN_PAGE = "/auth/login"
LOGIN_PROCESS_PAGE = "/auth/process_login"

RESULTS_COUNT_PATTERN = re.compile(r"(\d+) résultats trouvés")
TR_PATTERN = re.compile(r"<tr.*?>(.*?)</tr>", re.DOTALL)
TD_PATTERN = re.compile(r"<td.*?>(.*?)</td>", re.DOTALL)
NAME_PATTERN = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*)', re.DOTALL)


class YGGTorrentScraper(BaseScraper):
    _session = None
    _lock = asyncio.Lock()

    def __init__(self, manager, session: aiohttp.ClientSession):
        super().__init__(manager, session)

    @classmethod
    async def _ensure_session(cls):
        async with cls._lock:
            if cls._session:
                try:
                    response = await cls._session.get(f"{YGG_URL}/")
                    if response.status_code == 200 and "Déconnexion" in response.text:
                        return True
                except Exception:
                    pass

                logger.info("YGG Session expired or invalid, re-logging...")
                await cls._session.close()
                cls._session = None

            domain = urlparse(YGG_URL).netloc

            session = requests.AsyncSession(impersonate="chrome")
            session.cookies.set("account_created", "true", domain=domain)

            try:
                response = await session.get(f"{YGG_URL}{LOGIN_PAGE}")
                if response.status_code != 200:
                    logger.error(f"Failed to get login page: {response.status_code}")
                    await session.close()
                    return False

                payload = {
                    "id": settings.YGGTORRENT_USERNAME,
                    "pass": settings.YGGTORRENT_PASSWORD,
                }

                response = await session.post(
                    f"{YGG_URL}{LOGIN_PROCESS_PAGE}", data=payload
                )

                if response.status_code != 200:
                    logger.error(f"Login failed with status {response.status_code}")
                    await session.close()
                    return False

                response = await session.get(f"{YGG_URL}/")
                if "Déconnexion" in response.text or "logout" in response.text.lower():
                    logger.info("Successfully logged in to YGGTorrent.")
                    cls._session = session
                    return True
                else:
                    logger.error("Login verification failed.")
                    await session.close()
                    return False
            except Exception as e:
                logger.error(f"Exception during login: {e}")
                await session.close()
                return False

    async def _process_torrent(self, url, title, seeders, size):
        try:
            response = await self._session.get(url)
            if response.status_code != 200:
                logger.warning(
                    f"Failed to fetch torrent page {url}: {response.status_code}"
                )
                return []

            html_content = response.text

            hash_match = re.search(
                r"Hash\s*</td>\s*<td[^>]*>(.*?)</td>",
                html_content,
                re.IGNORECASE | re.DOTALL,
            )

            if not hash_match:
                logger.warning(f"Could not find hash in page {url}")
                return []

            info_hash = (
                hash_match.group(1).strip().replace("Tester", "").strip().lower()
            )

            info_hash = re.sub(r"<[^>]+>", "", info_hash).strip()

            if not info_hash or len(info_hash) != 40:
                logger.warning(f"Invalid hash found: {info_hash} in {url}")
                return []

            if not settings.YGGTORRENT_PASSKEY:
                logger.warning(
                    "YGGTORRENT_PASSKEY not set, cannot construct source URL."
                )
                return []

            source = f"{TRACKER_URL}/{settings.YGGTORRENT_PASSKEY}/announce"

            return [
                {
                    "title": title,
                    "infoHash": info_hash,
                    "fileIndex": None,
                    "seeders": seeders,
                    "size": size,
                    "tracker": "YGGTorrent",
                    "sources": [source],
                }
            ]

        except Exception as e:
            logger.warning(f"Exception processing torrent {url}: {e}")
            return []

    async def _scrape_page(self, query, offset, semaphore):
        url = f"{YGG_URL}/engine/search?name={query}&do=search&page={offset}&category=2145"

        async with semaphore:
            try:
                response = await self._session.get(url)
                if response.status_code != 200:
                    logger.warning(
                        f"Failed to scrape YGG page {offset}: {response.status_code}"
                    )
                    return [], 0

                html_content = response.text
            except Exception as e:
                logger.warning(f"Exception scraping YGG page {offset}: {e}")
                return [], 0

        total_results = 0
        if offset == 0:
            count_match = RESULTS_COUNT_PATTERN.search(html_content)
            if not count_match:
                return [], 0
            total_results = int(count_match.group(1))

        tasks = []
        for tr_match in TR_PATTERN.finditer(html_content):
            tr_content = tr_match.group(1)
            tds = TD_PATTERN.findall(tr_content)

            if len(tds) != 9:
                continue

            name_html = tds[1]
            name_match = NAME_PATTERN.search(name_html)

            href = name_match.group(1)

            if href.startswith("/"):
                href = f"{YGG_URL}{href}"

            title = name_match.group(2).split("</a>")[0].strip()

            seeders = int(tds[7])

            size_html = tds[5]
            clean_size = size_html.replace("o", "B")
            clean_size = re.sub(r"(\d)([a-zA-Z])", r"\1 \2", clean_size)
            size = size_to_bytes(clean_size)

            tasks.append(self._process_torrent(href, title, seeders, size))

        results = await asyncio.gather(*tasks)
        return [r for res in results for r in res], total_results

    async def scrape(self, request: ScrapeRequest):
        if not await self._ensure_session():
            return []

        limit = settings.YGGTORRENT_MAX_CONCURRENT_PAGES
        semaphore = asyncio.Semaphore(limit)

        # Fetch page 0 first to get total results
        results, total_results = await self._scrape_page(request.title, 0, semaphore)

        if total_results > 50:
            tasks = []
            for offset in range(50, total_results, 50):
                tasks.append(self._scrape_page(request.title, offset, semaphore))

            if tasks:
                pages_results = await asyncio.gather(*tasks)
                for page_res, _ in pages_results:
                    results.extend(page_res)

        return results
