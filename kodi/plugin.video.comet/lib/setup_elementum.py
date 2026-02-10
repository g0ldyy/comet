import xbmcaddon
import xbmcgui

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
    check_elementum()
