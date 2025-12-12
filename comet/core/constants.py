import aiohttp

from comet.core.models import settings

INDEXER_TIMEOUT = aiohttp.ClientTimeout(total=settings.INDEXER_MANAGER_TIMEOUT)
TORRENT_TIMEOUT = aiohttp.ClientTimeout(total=settings.GET_TORRENT_TIMEOUT)
CATALOG_TIMEOUT = aiohttp.ClientTimeout(total=settings.CATALOG_TIMEOUT)
