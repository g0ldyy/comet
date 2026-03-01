from fastapi import APIRouter, Request

from comet.core.config_validation import config_check
from comet.core.models import BUILTIN_PREFIXES, settings
from comet.debrid.manager import build_addon_name
from comet.utils.cache import (CachedJSONResponse, CachePolicies,
                               check_etag_match, generate_etag,
                               not_modified_response)

router = APIRouter()


def _build_custom_catalog_manifest(custom_catalogs: list) -> tuple[list, list]:
    """
    Given a user's customCatalogs config (list of {url, prefix} dicts),
    return (stremio_catalogs, extra_id_prefixes).

    stremio_catalogs - catalog entries to include in the manifest
    extra_id_prefixes - additional idPrefixes to advertise so Stremio sends
                        stream requests for IDs with those prefixes to Comet
    """
    stremio_catalogs = []
    extra_prefixes = []

    seen_prefixes = set()
    for idx, entry in enumerate(custom_catalogs or []):
        url = (entry.get("url") or "").strip().rstrip("/")
        prefix = (entry.get("prefix") or "").strip()
        if not url or not prefix:
            continue
        if prefix in BUILTIN_PREFIXES:
            continue  # never override built-ins

        # One search-style catalog per custom addon (minimal; Stremio will
        # discover via the addon's own manifest, but we still expose it so
        # the user can search from the Comet manifest).
        stremio_catalogs.append({
            "type": "movie",
            "id": f"cstm{idx}_{prefix}_movie",
            "name": f"Custom ({prefix})",
        })
        stremio_catalogs.append({
            "type": "series",
            "id": f"cstm{idx}_{prefix}_series",
            "name": f"Custom ({prefix})",
        })

        if prefix not in seen_prefixes:
            extra_prefixes.append(prefix)
            seen_prefixes.add(prefix)

    return stremio_catalogs, extra_prefixes


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
        "id": settings.ADDON_ID,
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

    config = config_check(b64config, strict_b64config=True)
    if not config:
        base_manifest["name"] = "❌ | Comet"
        base_manifest["description"] = (
            f"⚠️ OBSOLETE CONFIGURATION, PLEASE RE-CONFIGURE ON {request.url.scheme}://{request.url.netloc} ⚠️"
        )
        return base_manifest

    base_manifest["name"] = build_addon_name(settings.ADDON_NAME, config)

    # Inject custom catalog entries and extra idPrefixes from user config
    custom_catalogs_cfg = config.get("customCatalogs") or []
    if custom_catalogs_cfg:
        stremio_catalogs, extra_prefixes = _build_custom_catalog_manifest(
            custom_catalogs_cfg)
        if stremio_catalogs:
            base_manifest["catalogs"] = stremio_catalogs
            # Add "catalog" to resources if not already present
            resource_names = [
                r["name"] if isinstance(r, dict) else r
                for r in base_manifest["resources"]
            ]
            if "catalog" not in resource_names:
                base_manifest["resources"].append("catalog")

        if extra_prefixes:
            # Extend the stream resource's idPrefixes
            stream_resource = next(
                (r for r in base_manifest["resources"] if isinstance(
                    r, dict) and r.get("name") == "stream"),
                None,
            )
            if stream_resource:
                existing = stream_resource.get("idPrefixes", [])
                stream_resource["idPrefixes"] = existing + [
                    p for p in extra_prefixes if p not in existing
                ]

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
