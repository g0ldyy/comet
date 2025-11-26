import asyncio
import re
from urllib.parse import urlparse

import aiohttp
from curl_cffi import requests

from comet.core.logger import logger
from comet.core.models import settings
from comet.scrapers.base import BaseScraper
from comet.scrapers.models import ScrapeRequest
from comet.services.torrent_manager import extract_torrent_metadata
from comet.utils.formatting import size_to_bytes

LOGIN_PAGE = "/auth/login"
LOGIN_PROCESS_PAGE = "/auth/process_login"

RESULTS_COUNT_PATTERN = re.compile(r"(\d+) résultats trouvés")
TR_PATTERN = re.compile(r"<tr.*?>(.*?)</tr>", re.DOTALL)
TD_PATTERN = re.compile(r"<td.*?>(.*?)</td>", re.DOTALL)
NAME_PATTERN = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*)', re.DOTALL)


class YGGTorrentScraper(BaseScraper):
    _domain = None
    _session = None
    _lock = asyncio.Lock()

    def __init__(self, manager, session: aiohttp.ClientSession):
        super().__init__(manager, session)

    @classmethod
    async def _get_domain(cls):
        if cls._domain:
            return cls._domain

        try:
            async with requests.AsyncSession(impersonate="chrome") as session:
                response = await session.get("https://ygg.to", allow_redirects=False)
                if "location" in response.headers:
                    location = response.headers["location"]
                    domain = urlparse(location).netloc
                    logger.info(f"Resolved YGG domain to: {domain}")
                    cls._domain = domain
                    return domain
                elif response.status_code == 200:
                    cls._domain = urlparse(response.url).netloc
                    return cls._domain
        except Exception as e:
            logger.error(f"Error resolving YGG domain: {e}")
            return None
        return None

    @classmethod
    async def _ensure_session(cls):
        async with cls._lock:
            if cls._session:
                domain = await cls._get_domain()
                if not domain:
                    return False

                try:
                    response = await cls._session.get(f"https://{domain}/")
                    if response.status_code == 200 and "Déconnexion" in response.text:
                        return True
                except Exception:
                    pass

                logger.info("YGG Session expired or invalid, re-logging...")
                await cls._session.close()
                cls._session = None

            domain = await cls._get_domain()
            if not domain:
                return False

            session = requests.AsyncSession(impersonate="chrome")
            session.cookies.set("account_created", "true", domain=domain)

            try:
                response = await session.get(f"https://{domain}{LOGIN_PAGE}")
                if response.status_code != 200:
                    logger.error(f"Failed to get login page: {response.status_code}")
                    await session.close()
                    return False

                payload = {
                    "id": settings.YGGTORRENT_USERNAME,
                    "pass": settings.YGGTORRENT_PASSWORD,
                }

                response = await session.post(
                    f"https://{domain}{LOGIN_PROCESS_PAGE}", data=payload
                )

                if response.status_code != 200:
                    logger.error(f"Login failed with status {response.status_code}")
                    await session.close()
                    return False

                response = await session.get(f"https://{domain}/")
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

    async def _download_torrent(self, url):
        try:
            response = await self._session.get(url)
            if response.status_code == 200:
                return response.content
            logger.warning(f"Failed to download torrent file: {response.status_code}")
        except Exception as e:
            logger.warning(f"Exception downloading torrent file: {e}")
        return None

    async def _process_torrent(self, domain, torrent_id, title, seeders, size):
        download_url = f"https://{domain}/engine/download_torrent?id={torrent_id}"

        torrent_content = await self._download_torrent(download_url)

        results = []
        if torrent_content:
            try:
                metadata = extract_torrent_metadata(torrent_content)
                if metadata:
                    for file in metadata["files"]:
                        results.append(
                            {
                                "title": file["name"],
                                "infoHash": metadata["info_hash"].lower(),
                                "fileIndex": file["index"],
                                "seeders": seeders,
                                "size": file["size"],
                                "tracker": "YGGTorrent",
                                "sources": metadata["announce_list"],
                            }
                        )
                else:
                    logger.warning(
                        f"Failed to extract metadata for torrent {torrent_id}"
                    )
            except Exception as e:
                logger.warning(
                    f"Exception extracting metadata for torrent {torrent_id}: {e}"
                )
        else:
            logger.warning(f"Failed to download torrent content for {torrent_id}")

        return results

    async def _scrape_page(self, domain, query, offset, semaphore):
        url = f"https://{domain}/engine/search?name={query}&do=search&page={offset}&category=2145"

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
            id_match = re.search(r"/torrent/.*/(\d+)-", href)
            torrent_id = id_match.group(1)

            title = name_match.group(2)

            seeders = int(tds[7])

            size_html = tds[5]
            clean_size = size_html.replace("o", "B")
            clean_size = re.sub(r"(\d)([a-zA-Z])", r"\1 \2", clean_size)
            size = size_to_bytes(clean_size)

            tasks.append(
                self._process_torrent(domain, torrent_id, title, seeders, size)
            )

        results = await asyncio.gather(*tasks)
        return [r for res in results for r in res], total_results

    async def scrape(self, request: ScrapeRequest):
        if not await self._ensure_session():
            return []

        limit = settings.YGGTORRENT_MAX_CONCURRENT_PAGES
        semaphore = asyncio.Semaphore(limit)

        # Fetch page 0 first to get total results
        results, total_results = await self._scrape_page(self._domain, request.title, 0, semaphore)

        if total_results > 50:
            tasks = []
            for offset in range(50, total_results, 50):
                tasks.append(self._scrape_page(self._domain, request.title, offset, semaphore))

            if tasks:
                pages_results = await asyncio.gather(*tasks)
                for page_res, _ in pages_results:
                    results.extend(page_res)

        return results
