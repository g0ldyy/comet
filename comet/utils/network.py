import aiohttp
from fastapi import Request

from comet.core.models import settings

NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


def get_client_ip(request: Request):
    return (
        request.headers["cf-connecting-ip"]
        if "cf-connecting-ip" in request.headers
        else request.client.host
    )


async def fetch_with_proxy_fallback(
    session: aiohttp.ClientSession, url: str, headers: dict = None, params: dict = None
):
    try:
        async with session.get(url, headers=headers, params=params) as response:
            return await response.json()
    except Exception as first_error:
        if settings.BYPASS_PROXY_URL:
            try:
                async with session.get(
                    url,
                    headers=headers,
                    proxy=settings.BYPASS_PROXY_URL,
                    params=params,
                ) as response:
                    return await response.json()
            except Exception as second_error:
                raise second_error
        else:
            raise first_error
