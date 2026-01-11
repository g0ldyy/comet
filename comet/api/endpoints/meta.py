import aiohttp
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from comet.core.config_validation import config_check
from comet.core.constants import CATALOG_TIMEOUT
from comet.core.logger import logger

router = APIRouter()


@router.get("/meta/{type}/{id}.json")
@router.get("/{b64config}/meta/{type}/{id}.json")
async def get_meta(request: Request, type: str, id: str, b64config: str = None):
    """
    Proxies metadata requests to configured external metadata providers.
    Extracts the prefix from the ID (e.g., "kbx3585128:1:1" -> "kbx")
    and routes the request to the corresponding provider.
    """
    if type not in ["movie", "series"]:
        return JSONResponse(
            status_code=404,
            content={"meta": None}
        )

    # Parse configuration
    if not b64config:
        return JSONResponse(
            status_code=404,
            content={"meta": None}
        )
    
    config = config_check(b64config)
    if not config:
        return JSONResponse(
            status_code=404,
            content={"meta": None}
        )

    # Get metadata providers from config
    metadata_providers = config.get("metadataProviders", [])
    if not metadata_providers:
        return JSONResponse(
            status_code=404,
            content={"meta": None}
        )

    # Extract prefix from ID (e.g., "kbx3585128:1:1" -> "kbx")
    # The prefix is all alphabetic characters at the start before the first digit
    prefix = ""
    for char in id:
        if char.isdigit() or char == ":":
            break
        prefix += char

    if not prefix:
        return JSONResponse(
            status_code=404,
            content={"meta": None}
        )

    # Find matching provider
    provider = None
    for p in metadata_providers:
        if p.get("prefix") == prefix:
            provider = p
            break

    if not provider:
        logger.warning(f"No metadata provider found for prefix: {prefix}")
        return JSONResponse(
            status_code=404,
            content={"meta": None}
        )

    # Build provider URL
    provider_url = provider.get("url", "").rstrip("/")
    provider_meta_url = f"{provider_url}/meta/{type}/{id}.json"

    # Proxy request to external provider
    try:
        async with aiohttp.ClientSession(timeout=CATALOG_TIMEOUT) as session:
            async with session.get(provider_meta_url) as response:
                if response.status == 404:
                    return JSONResponse(
                        status_code=404,
                        content={"meta": None}
                    )
                
                response.raise_for_status()
                data = await response.json()
                
                # Return the proxied response
                return JSONResponse(content=data)
                
    except aiohttp.ClientError as e:
        logger.error(f"Error proxying metadata request to {provider_meta_url}: {e}")
        return JSONResponse(
            status_code=404,
            content={"meta": None}
        )
    except Exception as e:
        logger.exception(f"Unexpected error proxying metadata request to {provider_meta_url}: {e}")
        return JSONResponse(
            status_code=404,
            content={"meta": None}
        )
