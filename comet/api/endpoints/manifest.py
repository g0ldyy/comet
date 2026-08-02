from fastapi import APIRouter, Request

from comet.core.config_validation import config_check
from comet.core.manifest_branding import eligible_usenet_provider_badges
from comet.core.models import database, settings
from comet.debrid.manager import build_addon_name
from comet.utils.cache import CachePolicies, cached_json_response

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
async def manifest(request: Request, b64config: str | None = None):
    base_manifest = {
        "id": settings.ADDON_ID,
        "description": "Stremio's fastest torrent/debrid/usenet search add-on.",
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

    usenet_badges = await eligible_usenet_provider_badges(
        config,
        database,
        usenet_offered=settings.USENET_ENABLED,
        capability_secret=settings.COMET_CAPABILITY_SECRET,
        native_access_token=settings.USENET_NATIVE_ACCESS_TOKEN,
        native_servers=settings.USENET_NATIVE_SERVERS,
    )
    base_manifest["name"] = build_addon_name(
        settings.ADDON_NAME,
        config,
        usenet_badges,
    )

    return cached_json_response(
        request,
        base_manifest,
        cache_policy=CachePolicies.manifest(),
        vary=["Accept"],
    )
