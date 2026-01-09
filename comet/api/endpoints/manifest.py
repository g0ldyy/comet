import random
import string

from fastapi import APIRouter, Request

from comet.core.config_validation import config_check
from comet.core.models import settings
from comet.debrid.manager import get_debrid_extension
from comet.utils.cache import (CachedJSONResponse, CachePolicies,
                               check_etag_match, generate_etag,
                               not_modified_response)

router = APIRouter()


@router.get(
    "/manifest.json",
    tags=["Stremio"],
    summary="Add-on Manifest",
    description="Returns the add-on manifest.",
)
@router.get(
    "/{b64config}/manifest.json",
    tags=["Stremio"],
    summary="Add-on Manifest",
    description="Returns the add-on manifest with existing configuration.",
)
async def manifest(request: Request, b64config: str = None):
    base_manifest = {
        "id": f"{settings.ADDON_ID}.{''.join(random.choice(string.ascii_letters) for _ in range(4))}",
        "description": "Stremio's fastest torrent/debrid search add-on.",
        "version": "2.0.0",
        "catalogs": [],
        "resources": [
            {
                "name": "stream",
                "types": ["movie", "series"],
                "idPrefixes": ["tt", "kitsu"],
            }
        ],
        "types": ["movie", "series", "anime", "other"],
        "logo": "https://raw.githubusercontent.com/g0ldyy/comet/refs/heads/main/comet/assets/icon.png",
        "background": "https://raw.githubusercontent.com/g0ldyy/comet/refs/heads/main/comet/assets/background.png",
        "behaviorHints": {"configurable": True, "configurationRequired": False},
    }

    config = config_check(b64config)
    if not config:
        base_manifest["name"] = "❌ | Comet"
        base_manifest["description"] = (
            f"⚠️ OBSOLETE CONFIGURATION, PLEASE RE-CONFIGURE ON {request.url.scheme}://{request.url.netloc} ⚠️"
        )
        return base_manifest

    debrid_extension = get_debrid_extension(config["debridService"])
    base_manifest["name"] = (
        f"{settings.ADDON_NAME}{(' | ' + debrid_extension) if debrid_extension != 'TORRENT' else ''}"
    )

    if settings.HTTP_CACHE_ENABLED:
        etag = generate_etag(base_manifest)
        if check_etag_match(request, etag):
            return not_modified_response(etag)

        return CachedJSONResponse(
            content=base_manifest,
            cache_control=CachePolicies.manifest(),
            etag=etag,
            vary=["Accept", "Accept-Encoding"],
        )

    return base_manifest
