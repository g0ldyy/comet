import aiohttp
from yarl import URL

from comet.utils.models import settings
from comet.utils.proxy import get_proxy_url, set_proxy_auth_header


class RequestClient:
    def __init__(self, session: aiohttp.ClientSession, base_url: str):
        set_proxy_auth_header(session.headers)
        self.session = session
        self.base_url = URL(base_url)
        self.proxy = None

    def _prepare_url(self, endpoint: str):
        url = URL(endpoint)
        return get_proxy_url(
            str(
                url
                if url.absolute
                else self.base_url.joinpath(endpoint.removeprefix("/"))
            )
        )

    def enable_proxy(self):
        self.proxy = settings.DEBRID_PROXY_URL
        return bool(self.proxy)

    async def request(self, method: str, endpoint: str, **kwargs):
        return await self.session.request(
            method, self._prepare_url(endpoint), proxy=self.proxy, **kwargs
        )

    @classmethod
    async def static_request(
        cls, session: aiohttp.ClientSession, method: str, url: str, **kwargs
    ):
        return await session.request(method, get_proxy_url(url), **kwargs)
