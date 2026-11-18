import asyncio
import ipaddress
import math
import os
import socket
from contextlib import AsyncExitStack, asynccontextmanager
from functools import partial
from typing import ClassVar
from urllib.parse import urlsplit

import aiohttp
from curl_cffi import CurlOpt
from curl_cffi.requests import AsyncSession as CurlSession
from curl_cffi.requests.exceptions import ConnectionError as CurlConnectionError
from curl_cffi.requests.exceptions import Timeout as CurlTimeout

from comet.core.logger import logger
from comet.core.models import settings
from comet.utils.http_client import (
    HTTP_CONNECTION_ERRORS,
    SAFE_HTTP_METHODS,
    HttpSession,
    ProxyPolicy,
)


def resolve_proxy_url(proxy_url: str) -> str:
    """Resolve Docker service names that curl's resolver may not handle."""

    try:
        parsed = urlsplit(proxy_url)
        hostname, port = parsed.hostname, parsed.port
    except ValueError:
        return proxy_url

    # HTTPS proxies need the hostname for certificate verification.
    if not hostname or parsed.scheme == "https":
        return proxy_url

    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        return proxy_url

    try:
        address = socket.gethostbyname(hostname)
    except OSError as exc:
        logger.warning(f"Failed to resolve proxy hostname {hostname!r}: {exc}")
        return proxy_url

    credentials, separator, _ = parsed.netloc.rpartition("@")
    if port is not None:
        address = f"{address}:{port}"
    return parsed._replace(netloc=f"{credentials}{separator}{address}").geturl()


class CurlResponse:
    def __init__(self, response):
        self._response = response

    @property
    def status(self):
        return self._response.status_code

    @property
    def headers(self):
        return self._response.headers

    async def text(self):
        return self._response.text

    async def json(self):
        return self._response.json()

    async def read(self):
        return self._response.content


class AsyncClientWrapper:
    """Scraper HTTP client with optional TLS impersonation and 429 retries."""

    def __init__(
        self,
        scraper_name: str,
        impersonate: str | None = None,
        proxy_url: str | None = None,
        headers: dict | None = None,
        timeout: float | None = None,
    ):
        self.scraper_name = scraper_name
        self.impersonate = impersonate
        self.timeout = (
            settings.HTTP_CLIENT_TIMEOUT_TOTAL if timeout is None else timeout
        )
        self.headers = headers

        proxy_key = f"{scraper_name.upper()}_PROXY_URL"
        self._policy = ProxyPolicy(
            proxy_url
            or settings.model_extra.get(proxy_key.lower())
            or os.environ.get(proxy_key)
            or os.environ.get(proxy_key.lower())
            or settings.GLOBAL_PROXY_URL
        )
        self._http_session = None
        self._curl_session = None
        if impersonate:
            self._proxy_lock = asyncio.Lock()
            self._resolved_proxy: str | None = None
        else:
            self._http_session = HttpSession(
                policy=self._policy,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            )

    async def _send(self, stack, method, url, kwargs, proxy):
        if self._http_session is not None:
            return await stack.enter_async_context(
                self._http_session.open(method, url, proxy, **kwargs)
            )

        if proxy and self._resolved_proxy is None:
            async with self._proxy_lock:
                if self._resolved_proxy is None:
                    self._resolved_proxy = await asyncio.to_thread(
                        resolve_proxy_url, proxy
                    )

        if self._curl_session is None:
            self._curl_session = CurlSession(
                headers=self.headers,
                impersonate=self.impersonate,
                timeout=self.timeout,
                curl_options=None
                if self._policy.use_environment_proxy
                else {CurlOpt.NOPROXY: ""},
            )
        response = await self._curl_session.request(
            method,
            url,
            proxies=None
            if self._policy.use_environment_proxy
            else {"all": self._resolved_proxy if proxy else ""},
            **kwargs,
        )
        return CurlResponse(response)

    async def _attempt_request(self, stack, method, url, kwargs, proxy):
        max_retries = settings.RATELIMIT_MAX_RETRIES
        base_delay = settings.RATELIMIT_RETRY_BASE_DELAY
        for attempt in range(max_retries + 1):
            response = await self._send(stack, method, url, kwargs, proxy)
            if response.status != 429 or attempt == max_retries:
                return response

            try:
                delay = float(response.headers.get("Retry-After"))
            except (TypeError, ValueError):
                delay = math.nan
            if not math.isfinite(delay):
                delay = base_delay * (2**attempt)
            delay = max(base_delay, delay)
            logger.warning(
                f"[{self.scraper_name}] Received 429 Too Many Requests. "
                f"Retrying in {delay}s (attempt {attempt + 1}/{max_retries})"
            )
            await stack.aclose()
            await asyncio.sleep(delay)

    @asynccontextmanager
    async def request(self, method: str, url: str, **kwargs):
        retry_errors = (
            (CurlConnectionError, CurlTimeout)
            if self.impersonate
            else HTTP_CONNECTION_ERRORS
        )
        async with AsyncExitStack() as stack:
            yield await self._policy.connect(
                partial(self._attempt_request, stack, method, url, kwargs),
                retry_errors=retry_errors
                if method.upper() in SAFE_HTTP_METHODS
                else (),
            )

    def get(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self.request("POST", url, **kwargs)

    async def close(self):
        if self._http_session is not None:
            await self._http_session.close()
        if self._curl_session is not None:
            await self._curl_session.close()
            self._curl_session = None


class NetworkManager:
    _instance: ClassVar = None
    _clients: ClassVar[dict[str, AsyncClientWrapper]] = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def get_client(
        self,
        scraper_name: str,
        impersonate: str | None = None,
        headers: dict | None = None,
    ):
        # Unique key for client configuration
        key = f"{scraper_name}|{impersonate}"

        if key not in self._clients:
            self._clients[key] = AsyncClientWrapper(
                scraper_name=scraper_name, impersonate=impersonate, headers=headers
            )
        return self._clients[key]

    async def close_all(self):
        for key, client in list(self._clients.items()):
            try:
                await client.close()
            except Exception as e:
                logger.warning(f"Failed to close network client {key}: {e}")
        self._clients.clear()


network_manager = NetworkManager()
