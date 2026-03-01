"""
Custom catalog proxy endpoints.

Handles:
- GET /{b64config}/catalog/{type}/{id}.json
- GET /{b64config}/catalog/{type}/{id}/{extra:path}.json

Catalog IDs with the pattern `cstm{idx}_{prefix}_{type}` are proxied
to the user-configured custom catalog addon URL.

Also exposes a helper `resolve_custom_prefix_to_imdb` used by stream.py
to convert custom-prefix IDs (e.g. csfd12345) into IMDB IDs.
"""

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse
from typing import Optional

import aiohttp
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from loguru import logger

from comet.core.config_validation import config_check
from comet.utils.http_client import http_client_manager

router = APIRouter()


# ---------------------------------------------------------------------------
# SSRF protection helpers
# ---------------------------------------------------------------------------

_PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fc00::/7"),
]


def _is_safe_url(url: str) -> bool:
    """
    Return True if the URL resolves to a public, non-private address.
    Blocks SSRF targets: loopback, private ranges, link-local, multicast, etc.
    """
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return False

        # Reject raw IP literals in private ranges without DNS resolution
        try:
            addr = ipaddress.ip_address(hostname)
            for net in _PRIVATE_NETWORKS:
                if addr in net:
                    return False
            if addr.is_multicast or addr.is_unspecified or addr.is_reserved:
                return False
            return True
        except ValueError:
            pass  # hostname is a name, not an IP literal

        # Resolve hostname to IP and check
        resolved_ip = socket.getaddrinfo(hostname, None)[0][4][0]
        addr = ipaddress.ip_address(resolved_ip)
        for net in _PRIVATE_NETWORKS:
            if addr in net:
                logger.warning(
                    f"Custom catalog: SSRF block - {hostname!r} resolved to private range"
                )
                return False
        if addr.is_multicast or addr.is_unspecified or addr.is_reserved:
            return False
        return True
    except Exception as e:
        logger.warning(f"Custom catalog: URL safety check failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Internal HTTP helper
# ---------------------------------------------------------------------------

async def _fetch_json(url: str, timeout: int = 15) -> Optional[dict]:
    parsed = urlparse(url)
    host_label = parsed.hostname or "<unknown>"

    if not _is_safe_url(url):
        logger.warning(
            f"Custom catalog: blocked request to private/unsafe host {host_label!r}"
        )
        return None

    try:
        session = await http_client_manager.get_session()
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=timeout),
            headers={"Accept": "application/json"},
        ) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                if not isinstance(data, dict):
                    logger.warning(
                        f"Custom catalog: unexpected response type {type(data).__name__!r} "
                        f"from {host_label!r}"
                    )
                    return None
                logger.info(
                    f"Custom catalog: fetch success from {host_label!r}")
                return data
            logger.warning(
                f"Custom catalog: HTTP {resp.status} from {host_label!r}"
            )
            try:
                body_snippet = (await resp.text())[:200]
                logger.debug(
                    f"Custom catalog: error body snippet from {host_label!r}: {body_snippet!r}"
                )
            except Exception as e:
                logger.debug(
                    f"Custom catalog: failed reading response body from {host_label!r}: {e}",
                    exc_info=True,
                )
    except asyncio.TimeoutError:
        logger.warning(f"Custom catalog: timeout fetching from {host_label!r}")
    except Exception as e:
        logger.warning(
            f"Custom catalog: error fetching from {host_label!r}: {e}")
    return None


# ---------------------------------------------------------------------------
# Helper: resolve custom-prefix ID -> IMDB ID
# ---------------------------------------------------------------------------

async def resolve_custom_prefix_to_imdb(
    media_type: str,
    media_id: str,
    custom_catalogs: list,
    timeout: int = 15,
) -> tuple[Optional[str], Optional[dict]]:
    """
    For a media_id whose prefix matches one of the user's customCatalogs,
    call /meta/{type}/{base_id}.json on the corresponding addon URL and
    attempt to extract an IMDB ID from the response.

    Returns ``(imdb_id, meta_dict)``. If IMDB ID is not found, imdb_id is None,
    but meta_dict might still contain title/year for fallback scraping.
    """
    matched_url: Optional[str] = None
    for entry in custom_catalogs or []:
        prefix = (entry.get("prefix") or "").strip()
        url = (entry.get("url") or "").strip().rstrip("/")
        if prefix and url and media_id.startswith(prefix):
            matched_url = url
            break

    if not matched_url:
        logger.warning(
            f"Custom catalog: no matched addon URL for media_id prefix in {media_id!r}"
        )
        return None, None

    # Stremio uses IDs like prefix123:1:2 for series streams, but custom catalogs
    # usually only respond to the base ID (e.g. prefix123) for meta endpoints.
    base_id = media_id.split(":")[0]

    meta_url = f"{matched_url}/meta/{media_type}/{base_id}.json"
    parsed_host = urlparse(meta_url).hostname or "<unknown>"
    logger.info(
        f"Custom catalog: requesting IMDB resolution from {parsed_host!r}")
    data = await _fetch_json(meta_url, timeout)
    if not data:
        logger.warning(
            f"Custom catalog: fetch returned empty/none from {parsed_host!r}"
        )
        return None, None

    # data is validated to be a dict by _fetch_json
    meta = data.get("meta") or {}
    if not isinstance(meta, dict):
        logger.warning(
            f"Custom catalog: 'meta' field is not a dict (got {type(meta).__name__!r}) "
            f"from {parsed_host!r}"
        )
        return None, None

    logger.info(f"Custom catalog: received meta keys = {list(meta.keys())!r}")

    # Try common locations for IMDB ID - adjust to actual API response structure
    for candidate in [
        meta.get("imdbId"),
        meta.get("imdb"),
        meta.get("tt"),
        (meta.get("externalIds") or {}).get("imdb"),
        (meta.get("externalIds") or {}).get("imdbId"),
        (meta.get("filmOverviewOut") or {}).get("imdbId"),
        ((meta.get("filmOverviewOut") or {}).get("externalIds") or {}).get("imdb"),
    ]:
        if candidate and str(candidate).startswith("tt"):
            return str(candidate), meta

    return None, meta


# ---------------------------------------------------------------------------
# Catalog proxy endpoints
# ---------------------------------------------------------------------------

def _parse_catalog_id(catalog_id: str) -> Optional[tuple]:
    """
    Parse a catalog ID of the form ``cstm{idx}_{prefix}_{type}``.
    Returns ``(idx, prefix, catalog_type)`` or ``None``.
    Rejects negative or obviously out-of-range idx values.
    """
    if not catalog_id.startswith("cstm"):
        return None
    rest = catalog_id[4:]  # strip "cstm"
    try:
        underscore_pos = rest.index("_")
        idx = int(rest[:underscore_pos])
        if idx < 0:
            logger.warning(
                f"Custom catalog: rejected negative catalog index {idx}")
            return None
        remainder = rest[underscore_pos + 1:]
        # remainder is "{prefix}_{type}" - split at the *last* underscore
        # because prefix itself may not contain underscores and type is
        # always the rightmost segment.
        last_underscore = remainder.rfind("_")
        if last_underscore < 0:
            return None
        prefix = remainder[:last_underscore]
        cat_type = remainder[last_underscore + 1:]
        if not prefix or not cat_type:
            return None
        return idx, prefix, cat_type
    except (ValueError, IndexError):
        return None


async def _handle_catalog(
    b64config: str,
    catalog_type: str,
    catalog_id: str,
    extra: str,
) -> JSONResponse:
    parsed = _parse_catalog_id(catalog_id)
    if not parsed:
        return JSONResponse({"metas": []})

    idx, prefix, _declared_type = parsed

    config = config_check(b64config, strict_b64config=False)
    if not config:
        return JSONResponse({"metas": []})

    custom_catalogs = config.get("customCatalogs") or []
    # Reject both out-of-range and negative indices (negative already caught above,
    # but guard again against races/edge cases in case of direct calls)
    if idx < 0 or idx >= len(custom_catalogs):
        logger.warning(
            f"Custom catalog: index {idx} out of range (len={len(custom_catalogs)})"
        )
        return JSONResponse({"metas": []})

    entry = custom_catalogs[idx]
    base_url = (entry.get("url") or "").strip().rstrip("/")
    entry_prefix = (entry.get("prefix") or "").strip()

    if not base_url or not entry_prefix:
        return JSONResponse({"metas": []})

    # Safety: verify prefix still matches what is stored in user config
    if entry_prefix != prefix:
        logger.warning(
            f"Custom catalog: prefix mismatch: config has {entry_prefix!r}, "
            f"catalog_id implies {prefix!r}"
        )
        return JSONResponse({"metas": []})

    # The original catalog ID on the remote addon is constructed from the prefix
    # and the requested catalog type.  The manifest endpoint registers catalogs
    # using the pattern ``cstm{idx}_{prefix}_{type}`` which maps to
    # ``{prefix}_{catalog_type}`` on the upstream addon.
    original_catalog_id = f"{prefix}_{catalog_type}"
    if extra:
        proxy_url = f"{base_url}/catalog/{catalog_type}/{original_catalog_id}/{extra}.json"
    else:
        proxy_url = f"{base_url}/catalog/{catalog_type}/{original_catalog_id}.json"

    data = await _fetch_json(proxy_url)
    if data and isinstance(data.get("metas"), list):
        return JSONResponse(
            content=data,
            headers={"Access-Control-Allow-Origin": "*"},
        )
    return JSONResponse(
        content={"metas": []},
        headers={"Access-Control-Allow-Origin": "*"},
    )


@router.get(
    "/{b64config}/catalog/{catalog_type}/{catalog_id}.json",
    tags=["Stremio"],
    summary="Custom Catalog Proxy",
    description="Proxies catalog requests to user-configured external Stremio catalog addons.",
)
async def catalog(b64config: str, catalog_type: str, catalog_id: str):
    return await _handle_catalog(b64config, catalog_type, catalog_id, extra="")


@router.get(
    "/{b64config}/catalog/{catalog_type}/{catalog_id}/{extra:path}.json",
    tags=["Stremio"],
    summary="Custom Catalog Proxy (with extra)",
    description="Proxies catalog requests with extra params (search, skip, genre…) to external catalog addons.",
)
async def catalog_with_extra(b64config: str, catalog_type: str, catalog_id: str, extra: str):
    return await _handle_catalog(b64config, catalog_type, catalog_id, extra=extra)
