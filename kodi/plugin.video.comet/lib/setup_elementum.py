import xbmcaddon
import xbmcgui

try:
    from .diagnostics import run_boundary
except ImportError:
    from diagnostics import run_boundary

ELEMENTUM_ADDON_ID = "plugin.video.elementum"


def check_elementum():
    try:
        addon = xbmcaddon.Addon(ELEMENTUM_ADDON_ID)
    except Exception:
        xbmcgui.Dialog().notification(
            "Comet",
            "Elementum is not installed",
            xbmcgui.NOTIFICATION_ERROR,
        )
        return

    xbmcgui.Dialog().notification(
        "Comet",
        f"Elementum detected (v{addon.getAddonInfo('version')})",
        xbmcgui.NOTIFICATION_INFO,
    )


if __name__ == "__main__":
    run_boundary("kodi.setup_elementum.failed", check_elementum)
