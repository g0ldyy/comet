import asyncio
import math
import socket
from typing import ClassVar
from urllib.parse import urlparse, urlunparse

import aiohttp
import orjson
from curl_cffi.requests import AsyncSession as CurlSession
from curl_cffi.requests import RequestsError

from comet.core.models import settings
from comet.utils.proxy import (
    REMOTE_DNS_PROXY_SCHEMES,
    is_socks_proxy,
    socks_proxy_connector,
)

_MAX_DISCOVERY_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_RETRY_DELAY_SECONDS = 60.0
_DEFAULT_PROXY = object()


class DiscoveryResponseTooLarge(ValueError):
    pass


def resolve_proxy_url(proxy_url: str | None):
    """
    Resolve proxy hostname to IP address.

    This fixes an issue where curl_cffi/libcurl cannot resolve Docker service
    names even though Python and the shell's curl can.
    By resolving the hostname first via Python (which uses Docker's DNS),
    we can pass an IP-based URL to curl_cffi.
    """

    if proxy_url is None:
        return proxy_url

    parsed = urlparse(proxy_url)
    hostname = parsed.hostname
    if parsed.scheme == "https" or parsed.scheme in REMOTE_DNS_PROXY_SCHEMES:
        return proxy_url

    try:
        socket.inet_aton(hostname)
        return proxy_url
    except OSError:
        pass

    try:
        ip = socket.gethostbyname(hostname)
    except socket.gaierror:
        return proxy_url

    new_netloc = f"{ip}:{parsed.port}" if parsed.port is not None else ip
    if parsed.username is not None:
        credentials = parsed.username
        if parsed.password is not None:
            credentials = f"{credentials}:{parsed.password}"
        new_netloc = f"{credentials}@{new_netloc}"

    return urlunparse(
        (
            parsed.scheme,
            new_netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )


class ResponseWrapper:
    def __init__(
        self,
        response,
        backend: str,
        body: bytes | None = None,
        maximum_body_bytes: int = _MAX_DISCOVERY_RESPONSE_BYTES,
    ):
        self._response = response
        self.backend = backend
        self._body = body
        self._maximum_body_bytes = maximum_body_bytes

    @property
    def status(self):
        if self.backend == "curl":
            return self._response.status_code
        return self._response.status

    @property
    def headers(self):
        return self._response.headers

    async def _read_body(self) -> bytes:
        if self._body is not None:
            return self._body

        body = bytearray()
        while True:
            chunk = await self._response.content.read(64 * 1024)
            if not chunk:
                break
            if len(chunk) > self._maximum_body_bytes - len(body):
                raise DiscoveryResponseTooLarge("discovery response is too large")
            body.extend(chunk)
        self._body = bytes(body)
        return self._body

    async def read(self) -> bytes:
        return await self._read_body()

    async def text(self):
        body = await self._read_body()
        if self.backend == "curl":
            encoding = self._response.encoding or "utf-8"
        else:
            encoding = self._response.charset or "utf-8"
        return body.decode(encoding)

    async def json(self):
        return orjson.loads(await self._read_body())


def _retry_delay(retry_after, base_delay: float, attempt: int) -> float | None:
    bounded_base = min(base_delay, _MAX_RETRY_DELAY_SECONDS)
    try:
        requested = float(retry_after) if retry_after else None
    except (TypeError, ValueError):
        requested = None
    if requested is None or not math.isfinite(requested):
        requested = bounded_base * (2**attempt)
    elif requested > _MAX_RETRY_DELAY_SECONDS:
        return None
    return min(max(requested, bounded_base), _MAX_RETRY_DELAY_SECONDS)


class _RequestContextManager:
    def __init__(self, wrapper, method, url, **kwargs):
        self.wrapper = wrapper
        self.method = method
        self.url = url
        self.kwargs = kwargs
        self.kwargs["allow_redirects"] = False
        self.maximum_body_bytes = self.kwargs.pop(
            "maximum_body_bytes", _MAX_DISCOVERY_RESPONSE_BYTES
        )
        self.aiohttp_cm = None
        self.response = None

    async def __aenter__(self):
        self.wrapper._request_started()
        # Determine strict proxy usage
        use_proxy_explicit = self.kwargs.pop("use_proxy", None)
        proxy_url = self.wrapper.proxy_url
        proxy_ethos = self.wrapper.proxy_ethos

        if use_proxy_explicit is not None:
            should_use_proxy = use_proxy_explicit and proxy_url is not None
        elif proxy_ethos == "always":
            should_use_proxy = proxy_url is not None
        else:
            should_use_proxy = False

        try:
            try:
                return await self._attempt_request(
                    proxy_url if should_use_proxy else None
                )
            except (aiohttp.ClientError, RequestsError, TimeoutError):
                if (
                    proxy_ethos == "on_failure"
                    and not should_use_proxy
                    and proxy_url is not None
                ):
                    return await self._attempt_request(proxy_url)
                raise
        except BaseException:
            await self.wrapper._request_finished()
            raise

    async def _attempt_request(self, proxy):
        max_retries = settings.RATELIMIT_MAX_RETRIES
        base_delay = settings.RATELIMIT_RETRY_BASE_DELAY
        for attempt in range(max_retries + 1):
            if self.wrapper.impersonate:
                # Use curl_cffi
                curl_proxy = self.wrapper._resolved_proxy_url if proxy else None
                session = await self.wrapper._get_curl_session()
                body = bytearray()

                def receive(chunk, body=body):
                    if len(chunk) > self.maximum_body_bytes - len(body):
                        raise DiscoveryResponseTooLarge(
                            "discovery response is too large"
                        )
                    body.extend(chunk)

                raw_response = await session.request(
                    self.method,
                    self.url,
                    proxy=curl_proxy,
                    content_callback=receive,
                    **self.kwargs,
                )
                self.response = ResponseWrapper(
                    raw_response,
                    "curl",
                    bytes(body),
                    self.maximum_body_bytes,
                )
            else:
                # Use aiohttp
                aiohttp_proxy = proxy
                if proxy and is_socks_proxy(proxy):
                    session = await self.wrapper._get_socks_session(proxy)
                    aiohttp_proxy = None
                else:
                    session = await self.wrapper._get_aiohttp_session()
                self.aiohttp_cm = session.request(
                    self.method,
                    self.url,
                    proxy=aiohttp_proxy,
                    **self.kwargs,
                )
                raw_response = await self.aiohttp_cm.__aenter__()
                self.response = ResponseWrapper(
                    raw_response,
                    "aiohttp",
                    maximum_body_bytes=self.maximum_body_bytes,
                )

            if self.response.status != 429:
                return self.response

            # Handle 429 Too Many Requests
            if attempt < max_retries:
                retry_after = self.response.headers.get("Retry-After")
                delay = _retry_delay(retry_after, base_delay, attempt)
                if delay is None:
                    return self.response

                # Cleanup aiohttp context manager for the failed attempt
                if not self.wrapper.impersonate and self.aiohttp_cm is not None:
                    await self.aiohttp_cm.__aexit__(None, None, None)
                    self.aiohttp_cm = None

                await asyncio.sleep(delay)
            else:
                return self.response

    async def __aexit__(self, exc_type, exc, tb):
        try:
            if self.aiohttp_cm is not None:
                await self.aiohttp_cm.__aexit__(exc_type, exc, tb)
            # For curl_cffi, nothing special for now.
        finally:
            await self.wrapper._request_finished()


class AsyncClientWrapper:
    """
    A unified wrapper for aiohttp and curl_cffi sessions.
    Handles proxy logic, retries, and backend selection.
    """

    def __init__(
        self,
        impersonate: str | None = None,
        proxy_url: str | None | object = _DEFAULT_PROXY,
        headers: dict | None = None,
        timeout: float | None = None,
        discard_cookies: bool = False,
        proxy_ethos: str | None = None,
    ):
        self.impersonate = impersonate
        self.timeout = (
            settings.HTTP_CLIENT_TIMEOUT_TOTAL if timeout is None else timeout
        )
        self.headers = {} if headers is None else headers
        self.discard_cookies = discard_cookies
        self.proxy_url = (
            settings.GLOBAL_PROXY_URL if proxy_url is _DEFAULT_PROXY else proxy_url
        )

        self.proxy_ethos = settings.PROXY_ETHOS if proxy_ethos is None else proxy_ethos
        if self.proxy_url is None:
            self.proxy_ethos = "never"

        # Pre-resolve proxy hostname for curl_cffi
        self._resolved_proxy_url = resolve_proxy_url(self.proxy_url)

        self._aiohttp_session: aiohttp.ClientSession | None = None
        self._socks_sessions: dict[str, aiohttp.ClientSession] = {}
        self._curl_session: CurlSession | None = None
        self._active_requests = 0
        self._retiring = False

    def _request_started(self) -> None:
        self._active_requests += 1

    async def _request_finished(self) -> None:
        self._active_requests -= 1
        if self._retiring and self._active_requests == 0:
            await self.close()

    async def retire(self) -> None:
        self._retiring = True
        if self._active_requests == 0:
            await self.close()

    async def _get_aiohttp_session(self):
        if self._aiohttp_session is None or self._aiohttp_session.closed:
            self._aiohttp_session = aiohttp.ClientSession(
                headers=self.headers, timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
        return self._aiohttp_session

    async def _get_socks_session(self, proxy_url: str):
        session = self._socks_sessions.get(proxy_url)
        if session is None or session.closed:
            connector = socks_proxy_connector(proxy_url)
            session = aiohttp.ClientSession(
                connector=connector,
                headers=self.headers,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            )
            self._socks_sessions[proxy_url] = session
        return session

    async def _get_curl_session(self):
        if self._curl_session is None:
            self._curl_session = CurlSession(
                headers=self.headers,
                impersonate=self.impersonate,
                timeout=self.timeout,
                discard_cookies=self.discard_cookies,
            )
        return self._curl_session

    async def close(self):
        if self._aiohttp_session is not None:
            await self._aiohttp_session.close()
            self._aiohttp_session = None
        for session in self._socks_sessions.values():
            await session.close()
        self._socks_sessions.clear()
        if self._curl_session is not None:
            await self._curl_session.close()
            self._curl_session = None

    def request(self, method: str, url: str, **kwargs):
        return _RequestContextManager(self, method, url, **kwargs)

    def get(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs)


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
        *,
        discard_cookies: bool = False,
        proxy_ethos: str | None = None,
        proxy_setting: str | None = None,
    ):
        # Unique key for client configuration
        key = (
            f"{scraper_name}|{impersonate}|{discard_cookies}|"
            f"{proxy_ethos}|{proxy_setting}"
        )

        if key not in self._clients:
            if proxy_setting is None:
                proxy_url = (
                    settings.value(f"{scraper_name.upper()}_PROXY_URL")
                    or settings.GLOBAL_PROXY_URL
                )
            else:
                proxy_url = settings.value(proxy_setting)
            self._clients[key] = AsyncClientWrapper(
                impersonate=impersonate,
                proxy_url=proxy_url,
                headers=headers,
                discard_cookies=discard_cookies,
                proxy_ethos=proxy_ethos,
            )
        return self._clients[key]

    async def close_all(self):
        clients = tuple(self._clients.values())
        self._clients.clear()
        await asyncio.gather(*(client.close() for client in clients))

    async def retire_all(self):
        clients = tuple(self._clients.values())
        self._clients.clear()
        await asyncio.gather(*(client.retire() for client in clients))


network_manager = NetworkManager()
