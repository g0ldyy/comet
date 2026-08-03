import asyncio
import contextvars
from contextlib import asynccontextmanager

import aiohttp

from comet.utils.proxy import is_socks_proxy, socks_proxy_connector


async def read_bounded_body(response, maximum: int) -> bytes:
    """Read the actual response body while enforcing the only useful size bound."""
    body = bytearray()
    while chunk := await response.content.read(64 * 1024):
        body.extend(chunk)
        if len(body) > maximum:
            raise ValueError("response body is too large")
    if not body:
        raise ValueError("response body is empty")
    return bytes(body)


class HttpClientManager:
    def __init__(self):
        self._session: aiohttp.ClientSession | None = None
        self._user_session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()
        self._bound = contextvars.ContextVar("comet_http_session", default=None)
        self._leases: dict[aiohttp.ClientSession, int] = {}
        self._retired: set[aiohttp.ClientSession] = set()

    async def init(self) -> aiohttp.ClientSession:
        from comet.core.models import settings

        if self._session and not self._session.closed:
            return self._session

        async with self._lock:
            return self._ensure_session(settings)

    def _ensure_session(self, config) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = self.build(config)
        return self._session

    @staticmethod
    def build(config) -> aiohttp.ClientSession:
        connector = aiohttp.TCPConnector(
            limit=config.HTTP_CLIENT_LIMIT,
            limit_per_host=config.HTTP_CLIENT_LIMIT_PER_HOST,
            ttl_dns_cache=config.HTTP_CLIENT_TTL_DNS_CACHE,
            keepalive_timeout=config.HTTP_CLIENT_KEEPALIVE_TIMEOUT,
        )
        timeout = aiohttp.ClientTimeout(total=config.HTTP_CLIENT_TIMEOUT_TOTAL)
        return aiohttp.ClientSession(connector=connector, timeout=timeout)

    @staticmethod
    def build_user(config) -> aiohttp.ClientSession | None:
        proxy_url = config.USER_PROVIDED_PROXY_URL
        if proxy_url is None:
            return None
        connector_options = {
            "limit": config.HTTP_CLIENT_LIMIT,
            "limit_per_host": config.HTTP_CLIENT_LIMIT_PER_HOST,
            "ttl_dns_cache": config.HTTP_CLIENT_TTL_DNS_CACHE,
            "keepalive_timeout": config.HTTP_CLIENT_KEEPALIVE_TIMEOUT,
        }
        if is_socks_proxy(proxy_url):
            connector = socks_proxy_connector(proxy_url, **connector_options)
            session_proxy = None
        else:
            connector = aiohttp.TCPConnector(**connector_options)
            session_proxy = proxy_url
        return aiohttp.ClientSession(
            connector=connector,
            cookie_jar=aiohttp.DummyCookieJar(),
            proxy=session_proxy,
            timeout=aiohttp.ClientTimeout(total=config.HTTP_CLIENT_TIMEOUT_TOTAL),
        )

    async def replace(self, session: aiohttp.ClientSession) -> None:
        async with self._lock:
            previous, self._session = self._session, session
            if previous is not None and self._leases.get(previous, 0) > 0:
                self._retired.add(previous)
                previous = None
        if previous is not None and not previous.closed:
            await previous.close()

    async def replace_user(self, session: aiohttp.ClientSession | None) -> None:
        async with self._lock:
            previous, self._user_session = self._user_session, session
        if previous is not None and not previous.closed:
            await previous.close()

    async def get_session(self) -> aiohttp.ClientSession:
        return self._bound.get() or await self.init()

    async def get_user_session(self) -> aiohttp.ClientSession:
        from comet.core.models import settings

        if settings.USER_PROVIDED_PROXY_URL is None:
            return await self.get_session()
        if self._user_session is not None and not self._user_session.closed:
            return self._user_session
        async with self._lock:
            if self._user_session is None or self._user_session.closed:
                self._user_session = self.build_user(settings)
            return self._user_session

    @asynccontextmanager
    async def bind(self):
        from comet.core.models import settings

        async with self._lock:
            session = self._ensure_session(settings)
            self._leases[session] = self._leases.get(session, 0) + 1
        token = self._bound.set(session)
        try:
            yield session
        finally:
            self._bound.reset(token)
            close = False
            async with self._lock:
                leases = self._leases[session] - 1
                if leases:
                    self._leases[session] = leases
                else:
                    self._leases.pop(session)
                    close = session in self._retired
                    self._retired.discard(session)
            if close and not session.closed:
                await session.close()

    async def close(self) -> None:
        async with self._lock:
            sessions = self._retired | {
                session
                for session in (self._session, self._user_session)
                if session is not None
            }
            leased = {
                session for session in sessions if self._leases.get(session, 0) > 0
            }
            self._session = None
            self._user_session = None
            self._retired = leased
        await asyncio.gather(
            *(session.close() for session in sessions - leased if not session.closed),
            return_exceptions=True,
        )


http_client_manager = HttpClientManager()
