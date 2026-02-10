import sys
from urllib import parse

import requests
import xbmc
import xbmcaddon
import xbmcgui

ADDON_HANDLE = int(sys.argv[1])
ADDON = xbmcaddon.Addon()
ADDON_PATH = sys.argv[0]
ADDON_ID = ADDON.getAddonInfo("id")

REQUEST_TIMEOUT = 20
DEFAULT_CATALOG_PROVIDER_URL = "https://v3-cinemeta.strem.io"
HTTP_SESSION = requests.Session()


def log(message: str, level=xbmc.LOGINFO):
    xbmc.log(f"[Comet] {message}", level)


def build_url(action: str, **params):
    query = parse.urlencode(params)
    return (
        f"{ADDON_PATH}?action={action}&{query}"
        if query
        else f"{ADDON_PATH}?action={action}"
    )


def fetch_data(url: str):
    try:
        response = HTTP_SESSION.get(url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        resp = exc.response
        status_code = resp.status_code if resp is not None else None
        target = parse.urlparse(url).netloc or url
        log(f"Request failed for {url}: {exc}", xbmc.LOGERROR)
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
    if get_secret_string():
        return True

    xbmcgui.Dialog().notification(
        "Comet",
        "Comet is not configured. Open addon settings.",
        xbmcgui.NOTIFICATION_INFO,
    )
    xbmc.executebuiltin(
        f"RunScript(special://home/addons/{ADDON_ID}/lib/custom_settings_window.py)"
    )
    return False
