from typing import Dict, Optional

import aiohttp
from curl_cffi.requests import AsyncSession as CurlSession

from comet.core.logger import logger
from comet.core.models import settings


class ResponseWrapper:
    def __init__(self, response, backend: str):
        self._response = response
        self.backend = backend

    @property
    def status(self):
        if self.backend == "curl":
            return self._response.status_code
        return self._response.status

    @property
    def status_code(self):
        return self.status

    @property
    def headers(self):
        return self._response.headers

    async def text(self):
        if self.backend == "curl":
            return self._response.text
        return await self._response.text()

    async def json(self):
        if self.backend == "curl":
            return self._response.json()
        return await self._response.json()

    async def read(self):
        if self.backend == "curl":
            return self._response.content
        return await self._response.read()

    def __getattr__(self, name):
        return getattr(self._response, name)


class _RequestContextManager:
    def __init__(self, wrapper, method, url, **kwargs):
        self.wrapper = wrapper
        self.method = method
        self.url = url
        self.kwargs = kwargs
        self.aiohttp_cm = None
        self.response = None

    async def __aenter__(self):
        # Determine strict proxy usage
        use_proxy_explicit = self.kwargs.pop("use_proxy", None)
        proxy_url = self.wrapper.proxy_url
        proxy_ethos = self.wrapper.proxy_ethos

        should_use_proxy = False
        if use_proxy_explicit is not None:
            should_use_proxy = use_proxy_explicit and bool(proxy_url)
        elif proxy_ethos == "always":
            should_use_proxy = bool(proxy_url)

        try:
            return await self._attempt_request(
                should_use_proxy, proxy_url if should_use_proxy else None
            )
        except Exception as e:
            if proxy_ethos == "on_failure" and not should_use_proxy and proxy_url:
                logger.warning(
                    f"[{self.wrapper.scraper_name}] Direct request failed, retrying with proxy: {e}"
                )
                return await self._attempt_request(True, proxy_url)
            raise e

    async def _attempt_request(self, use_proxy, proxy):
        if self.wrapper.impersonate:
            # Use curl_cffi
            session = await self.wrapper._get_curl_session()
            raw_response = await session.request(
                self.method, self.url, proxy=proxy, **self.kwargs
            )
            self.response = ResponseWrapper(raw_response, "curl")
            return self.response
        else:
            # Use aiohttp
            session = await self.wrapper._get_aiohttp_session()
            self.aiohttp_cm = session.request(
                self.method, self.url, proxy=proxy, **self.kwargs
            )
            raw_response = await self.aiohttp_cm.__aenter__()
            self.response = ResponseWrapper(raw_response, "aiohttp")
            return self.response

    async def __aexit__(self, exc_type, exc, tb):
        if self.aiohttp_cm:
            await self.aiohttp_cm.__aexit__(exc_type, exc, tb)
        # For curl_cffi, nothing special for now.

    def __await__(self):
        return self.__aenter__().__await__()


class AsyncClientWrapper:
    """
    A unified wrapper for aiohttp and curl_cffi sessions.
    Handles proxy logic, retries, and backend selection.
    """

    def __init__(
        self,
        scraper_name: str,
        base_url: Optional[str] = None,
        impersonate: Optional[str] = None,
        proxy_url: Optional[str] = None,
        headers: Optional[dict] = None,
        timeout: int = 30,
    ):
        self.scraper_name = scraper_name
        self.base_url = base_url
        self.impersonate = impersonate
        self.timeout = timeout
        self.headers = headers or {}

        # Determine proxy configuration
        self.proxy_url = proxy_url
        if not self.proxy_url:
            # Try specific scraper proxy
            scraper_proxy_key = (
                f"{scraper_name.upper().replace('SCRAPER', '')}_PROXY_URL"
            )
            self.proxy_url = getattr(settings, scraper_proxy_key, None)

        if not self.proxy_url:
            # Fallback to global proxy
            self.proxy_url = settings.GLOBAL_PROXY_URL

        self.proxy_ethos = settings.PROXY_ETHOS.lower()
        if not self.proxy_url:
            self.proxy_ethos = "never"  # No proxy to use

        self._aiohttp_session: Optional[aiohttp.ClientSession] = None
        self._curl_session: Optional[CurlSession] = None

    async def _get_aiohttp_session(self) -> aiohttp.ClientSession:
        if not self._aiohttp_session or self._aiohttp_session.closed:
            self._aiohttp_session = aiohttp.ClientSession(
                headers=self.headers, timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
        return self._aiohttp_session

    async def _get_curl_session(self) -> CurlSession:
        if not self._curl_session:
            self._curl_session = CurlSession(
                headers=self.headers,
                impersonate=self.impersonate or "chrome",
                timeout=self.timeout,
            )
        return self._curl_session

    async def close(self):
        if self._aiohttp_session:
            await self._aiohttp_session.close()
        if self._curl_session:
            await self._curl_session.close()

    def request(self, method: str, url: str, **kwargs) -> _RequestContextManager:
        return _RequestContextManager(self, method, url, **kwargs)

    def get(self, url: str, **kwargs) -> _RequestContextManager:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> _RequestContextManager:
        return self.request("POST", url, **kwargs)


class NetworkManager:
    _instance = None
    _clients: Dict[str, AsyncClientWrapper] = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(NetworkManager, cls).__new__(cls)
        return cls._instance

    def get_client(
        self,
        scraper_name: str,
        impersonate: Optional[str] = None,
        headers: Optional[dict] = None,
    ) -> AsyncClientWrapper:
        # Unique key for client configuration
        key = f"{scraper_name}|{impersonate}"

        if key not in self._clients:
            self._clients[key] = AsyncClientWrapper(
                scraper_name=scraper_name, impersonate=impersonate, headers=headers
            )
        else:
            pass
        return self._clients[key]

    async def close_all(self):
        for client in self._clients.values():
            await client.close()
        self._clients.clear()


network_manager = NetworkManager()
