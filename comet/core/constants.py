import aiohttp

from comet.core.models import settings


def indexer_timeout() -> aiohttp.ClientTimeout:
    return aiohttp.ClientTimeout(total=settings.INDEXER_MANAGER_TIMEOUT)


def torrent_timeout() -> aiohttp.ClientTimeout:
    return aiohttp.ClientTimeout(total=settings.GET_TORRENT_TIMEOUT)


def catalog_timeout() -> aiohttp.ClientTimeout:
    return aiohttp.ClientTimeout(total=settings.CATALOG_TIMEOUT)
