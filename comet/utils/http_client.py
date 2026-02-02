import asyncio
from typing import Optional

import aiohttp

from comet.core.models import settings


class HttpClientManager:
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()

    async def init(self) -> aiohttp.ClientSession:
        if self._session and not self._session.closed:
            return self._session

        async with self._lock:
            if self._session and not self._session.closed:
                return self._session

            limit = (
                settings.HTTP_CLIENT_LIMIT
                if settings.HTTP_CLIENT_LIMIT is not None
                else 100
            )
            limit_per_host = (
                settings.HTTP_CLIENT_LIMIT_PER_HOST
                if settings.HTTP_CLIENT_LIMIT_PER_HOST is not None
                else 20
            )
            connector = aiohttp.TCPConnector(
                limit=limit,
                limit_per_host=limit_per_host,
                ttl_dns_cache=settings.HTTP_CLIENT_TTL_DNS_CACHE,
                keepalive_timeout=settings.HTTP_CLIENT_KEEPALIVE_TIMEOUT,
                enable_cleanup_closed=True,
            )
            timeout = aiohttp.ClientTimeout(total=settings.HTTP_CLIENT_TIMEOUT_TOTAL)
            self._session = aiohttp.ClientSession(connector=connector, timeout=timeout)
            return self._session

    async def get_session(self) -> aiohttp.ClientSession:
        return await self.init()

    async def close(self) -> None:
        async with self._lock:
            if self._session and not self._session.closed:
                await self._session.close()
            self._session = None


http_client_manager = HttpClientManager()
