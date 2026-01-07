import asyncio
import os
import socket
from typing import Dict, Optional
from urllib.parse import urlparse, urlunparse

import aiohttp
from curl_cffi.requests import AsyncSession as CurlSession

from comet.core.logger import logger
from comet.core.models import settings


def resolve_proxy_url(proxy_url: Optional[str]):
    """
    Resolve proxy hostname to IP address.

    This fixes an issue where curl_cffi/libcurl cannot resolve Docker service
    names even though Python and the shell's curl can.
    By resolving the hostname first via Python (which uses Docker's DNS),
    we can pass an IP-based URL to curl_cffi.
    """

    if not proxy_url:
        return proxy_url

    try:
        parsed = urlparse(proxy_url)
        hostname = parsed.hostname

        if not hostname:
            return proxy_url

        # Skip if already an IP address
        try:
            socket.inet_aton(hostname)
            return proxy_url  # Already an IP
        except socket.error:
            pass  # Not an IP, continue with resolution

        # Resolve hostname to IP
        ip = socket.gethostbyname(hostname)

        # Reconstruct netloc with IP instead of hostname
        if parsed.port:
            new_netloc = f"{ip}:{parsed.port}"
        else:
            new_netloc = ip

        # Preserve auth if present
        if parsed.username:
            if parsed.password:
                new_netloc = f"{parsed.username}:{parsed.password}@{new_netloc}"
            else:
                new_netloc = f"{parsed.username}@{new_netloc}"

        resolved_url = urlunparse(
            (
                parsed.scheme,
                new_netloc,
                parsed.path,
                parsed.params,
                parsed.query,
                parsed.fragment,
            )
        )

        return resolved_url
    except socket.gaierror as e:
        logger.warning(f"Failed to resolve proxy hostname in '{proxy_url}': {e}")
        return proxy_url  # Return original, let curl try
    except Exception as e:
        logger.warning(f"Error resolving proxy URL '{proxy_url}': {e}")
        return proxy_url


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
        max_retries = max(0, settings.RATELIMIT_MAX_RETRIES)
        base_delay = settings.RATELIMIT_RETRY_BASE_DELAY
        for attempt in range(max_retries + 1):
            if self.wrapper.impersonate:
                # Use curl_cffi
                curl_proxy = self.wrapper._resolved_proxy_url if proxy else None
                session = await self.wrapper._get_curl_session()
                raw_response = await session.request(
                    self.method, self.url, proxy=curl_proxy, **self.kwargs
                )
                self.response = ResponseWrapper(raw_response, "curl")
            else:
                # Use aiohttp
                session = await self.wrapper._get_aiohttp_session()
                self.aiohttp_cm = session.request(
                    self.method, self.url, proxy=proxy, **self.kwargs
                )
                raw_response = await self.aiohttp_cm.__aenter__()
                self.response = ResponseWrapper(raw_response, "aiohttp")

            if self.response.status != 429:
                return self.response

            # Handle 429 Too Many Requests
            if attempt < max_retries:
                retry_after = self.response.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else None
                except (ValueError, TypeError):
                    delay = None

                if delay is None:
                    delay = base_delay * (2**attempt)

                # Enforce a minimum delay if the server indicates 0 or very small retry-after
                delay = max(delay, base_delay)

                logger.warning(
                    f"[{self.wrapper.scraper_name}] Received 429 Too Many Requests. Retrying in {delay}s... (Attempt {attempt + 1}/{max_retries})"
                )

                # Cleanup aiohttp context manager for the failed attempt
                if not self.wrapper.impersonate and self.aiohttp_cm:
                    await self.aiohttp_cm.__aexit__(None, None, None)
                    self.aiohttp_cm = None

                await asyncio.sleep(delay)
            else:
                logger.error(
                    f"[{self.wrapper.scraper_name}] Max retries ({max_retries}) exceeded for 429 Too Many Requests."
                )
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
            scraper_proxy_key = f"{scraper_name.upper()}_PROXY_URL"

            self.proxy_url = settings.model_extra.get(scraper_proxy_key.lower())

            if not self.proxy_url:
                self.proxy_url = os.environ.get(scraper_proxy_key)
                if not self.proxy_url:
                    self.proxy_url = os.environ.get(scraper_proxy_key.lower())

        if not self.proxy_url:
            # Fallback to global proxy
            self.proxy_url = settings.GLOBAL_PROXY_URL

        self.proxy_ethos = settings.PROXY_ETHOS.lower()
        if not self.proxy_url:
            self.proxy_ethos = "never"  # No proxy to use

        # Pre-resolve proxy hostname for curl_cffi
        self._resolved_proxy_url = resolve_proxy_url(self.proxy_url)

        self._aiohttp_session: Optional[aiohttp.ClientSession] = None
        self._curl_session: Optional[CurlSession] = None

    async def _get_aiohttp_session(self):
        if not self._aiohttp_session or self._aiohttp_session.closed:
            self._aiohttp_session = aiohttp.ClientSession(
                headers=self.headers, timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
        return self._aiohttp_session

    async def _get_curl_session(self):
        if not self._curl_session:
            self._curl_session = CurlSession(
                headers=self.headers,
                impersonate=self.impersonate,
                timeout=self.timeout,
            )
        return self._curl_session

    async def close(self):
        if self._aiohttp_session:
            await self._aiohttp_session.close()
        if self._curl_session:
            await self._curl_session.close()

    def request(self, method: str, url: str, **kwargs):
        return _RequestContextManager(self, method, url, **kwargs)

    def get(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
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
    ):
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
