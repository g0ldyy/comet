import sys
from urllib import parse

import requests
import xbmc
import xbmcaddon
import xbmcgui

from .diagnostics import emit
from .http_json import (
    JsonHttpError,
    normalize_api_prefix,
    origin_label,
    request_json,
    response_status,
)

ADDON_HANDLE = int(sys.argv[1])
ADDON = xbmcaddon.Addon()
ADDON_PATH = sys.argv[0]
ADDON_ID = ADDON.getAddonInfo("id")

REQUEST_TIMEOUT = 20
DEFAULT_CATALOG_PROVIDER_URL = "https://v3-cinemeta.strem.io"
HTTP_SESSION = requests.Session()


def build_url(action: str, **params):
    query = parse.urlencode(params)
    return (
        f"{ADDON_PATH}?action={action}&{query}"
        if query
        else f"{ADDON_PATH}?action={action}"
    )


def fetch_data(url: str):
    try:
        return request_json(
            HTTP_SESSION,
            "GET",
            url,
            timeout=REQUEST_TIMEOUT,
        )
    except (requests.RequestException, JsonHttpError) as exc:
        status_code = response_status(exc)
        target = origin_label(url)
        emit(
            "kodi.http.failed",
            error=exc,
            status=status_code,
        )
        xbmcgui.Dialog().notification(
            "Comet",
            f"Request failed ({status_code}) on {target}"
            if status_code
            else f"Request failed on {target}",
            xbmcgui.NOTIFICATION_ERROR,
        )
        return None


def convert_info_hash_to_magnet(
    info_hash: str,
    trackers: list[str],
    display_name: str = "",
):
    magnet_parts = [f"magnet:?xt=urn:btih:{info_hash.strip()}"]
    if display_name:
        magnet_parts.append(f"dn={parse.quote(display_name, safe='')}")

    seen = set()
    for source in trackers:
        if source.startswith("tracker:"):
            stype, svalue = "tr", source[8:]
        elif source.startswith("dht:"):
            stype, svalue = "dht", source[4:]
        else:
            stype, svalue = "tr", source

        svalue = svalue.strip()
        if not svalue:
            continue

        key = (stype, svalue)
        if key in seen:
            continue
        seen.add(key)

        magnet_parts.append(f"{stype}={parse.quote(svalue, safe='')}")

    return "&".join(magnet_parts)


def get_base_url():
    return ADDON.getSetting("base_url").rstrip("/")


def get_secret_string():
    return ADDON.getSetting("secret_string")


def get_stremio_api_prefix():
    try:
        prefix = normalize_api_prefix(ADDON.getSetting("stremio_api_prefix"))
    except JsonHttpError:
        return ""
    return f"{prefix}/" if prefix else ""


def get_config_prefix():
    api_prefix = get_stremio_api_prefix()
    secret = get_secret_string()
    return f"{api_prefix}{secret}/" if secret else api_prefix


def get_catalog_provider_url():
    configured = ADDON.getSetting("catalog_provider_url").strip()
    if not configured:
        return DEFAULT_CATALOG_PROVIDER_URL
    if "://" not in configured:
        configured = "https://" + configured
    return configured.rstrip("/")


def is_elementum_installed_and_enabled():
    try:
        xbmcaddon.Addon("plugin.video.elementum")
        return True
    except Exception:
        return False


def ensure_configured():
    if get_base_url():
        return True

    xbmcgui.Dialog().notification(
        "Comet",
        "Comet is not configured. Open add-on settings.",
        xbmcgui.NOTIFICATION_INFO,
    )
    xbmc.executebuiltin(
        f"RunScript(special://home/addons/{ADDON_ID}/lib/custom_settings_window.py)"
    )
    return False
