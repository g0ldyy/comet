from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager

import aiohttp
from aiohttp_socks import ProxyConnector, ProxyType
from yarl import URL

from comet.core.logger import logger
from comet.core.models import settings

_SOCKS_TYPES = {
    "socks4": ProxyType.SOCKS4,
    "socks4a": ProxyType.SOCKS4,
    "socks5": ProxyType.SOCKS5,
    "socks5h": ProxyType.SOCKS5,
}
SAFE_HTTP_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
HTTP_CONNECTION_ERRORS = (aiohttp.ClientConnectionError, TimeoutError)


class ProxyPolicy:
    def __init__(self, url: str | None):
        url = (url or "").strip()
        if url.lower().startswith("socks5://"):
            # Keep origin DNS on the proxy for both curl and aiohttp.
            url = "socks5h://" + url[len("socks5://") :]
        ethos = (settings.PROXY_ETHOS or "always").strip().lower()
        self.initial_url = (url or None) if ethos == "always" else None
        self.fallback_url = (url or None) if ethos == "on_failure" else None
        self.use_environment_proxy = ethos == "always" and not url

    async def connect[T](
        self,
        send: Callable[[str | None], Awaitable[T]],
        *,
        retry_errors: tuple[type[Exception], ...] = (Exception,),
    ) -> T:
        if self.fallback_url is None or not retry_errors:
            return await send(self.initial_url)
        try:
            return await send(None)
        except retry_errors as exc:
            logger.warning(
                f"Direct request failed ({type(exc).__name__}), retrying with proxy"
            )
        return await send(self.fallback_url)


class HttpSession:
    def __init__(
        self,
        *,
        policy: ProxyPolicy | None = None,
        connector_options=None,
        **session_options,
    ):
        self.policy = (
            policy if policy is not None else ProxyPolicy(settings.GLOBAL_PROXY_URL)
        )
        self._connector_options = connector_options or {}
        self._session_options = session_options
        self._sessions: dict[str | None, aiohttp.ClientSession] = {}
        self.closed = False

    def open(self, method: str, url, proxy: str | None, **kwargs):
        if self.closed:
            raise RuntimeError("Session is closed")

        scheme = proxy.partition(":")[0].lower() if proxy else ""
        socks_proxy = proxy if scheme in _SOCKS_TYPES else None
        session = self._sessions.get(socks_proxy)
        if session is None:
            if socks_proxy:
                endpoint = URL(socks_proxy)
                connector = ProxyConnector(
                    proxy_type=_SOCKS_TYPES[scheme],
                    host=endpoint.host,
                    port=1080 if endpoint.port is None else endpoint.port,
                    username=endpoint.user,
                    password=endpoint.password,
                    rdns=scheme in {"socks4a", "socks5h"},
                    **self._connector_options,
                )
            else:
                connector = aiohttp.TCPConnector(**self._connector_options)
            session = aiohttp.ClientSession(
                connector=connector, **self._session_options
            )
            self._sessions[socks_proxy] = session

        return session.request(
            method, url, proxy=None if socks_proxy else proxy, **kwargs
        )

    def request(self, method: str, url, **kwargs):
        # A request may already have changed state before a network failure.
        if self.policy.fallback_url is None or method.upper() not in SAFE_HTTP_METHODS:
            return self.open(method, url, self.policy.initial_url, **kwargs)
        return self._request_with_fallback(method, url, **kwargs)

    @asynccontextmanager
    async def _request_with_fallback(self, method: str, url, **kwargs):
        async with AsyncExitStack() as stack:
            yield await self.policy.connect(
                lambda proxy: stack.enter_async_context(
                    self.open(method, url, proxy, **kwargs)
                ),
                retry_errors=HTTP_CONNECTION_ERRORS,
            )

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    async def close(self):
        self.closed = True
        for session in self._sessions.values():
            await session.close()
        self._sessions.clear()


class HttpClientManager:
    def __init__(self):
        self._session: HttpSession | None = None

    async def init(self) -> HttpSession:
        if self._session is None or self._session.closed:
            self._session = HttpSession(
                timeout=aiohttp.ClientTimeout(total=settings.HTTP_CLIENT_TIMEOUT_TOTAL),
                connector_options={
                    "limit": settings.HTTP_CLIENT_LIMIT,
                    "limit_per_host": settings.HTTP_CLIENT_LIMIT_PER_HOST,
                    "ttl_dns_cache": settings.HTTP_CLIENT_TTL_DNS_CACHE,
                    "keepalive_timeout": settings.HTTP_CLIENT_KEEPALIVE_TIMEOUT,
                    "enable_cleanup_closed": True,
                },
            )
        return self._session

    async def get_session(self) -> HttpSession:
        return await self.init()

    async def close(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            await session.close()


http_client_manager = HttpClientManager()
