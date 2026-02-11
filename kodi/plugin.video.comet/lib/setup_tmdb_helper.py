import os

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

ADDON_ID = "plugin.video.comet"
TMDB_HELPER_ADDON_ID = "plugin.video.themoviedb.helper"


def read_text_file(path: str):
    file_handle = xbmcvfs.File(path)
    try:
        return file_handle.read()
    finally:
        file_handle.close()


def setup_tmdb_helper_player():
    dialog = xbmcgui.Dialog()
    try:
        xbmcaddon.Addon(TMDB_HELPER_ADDON_ID)
    except Exception:
        dialog.notification(
            "Comet",
            "TMDB Helper is not installed",
            xbmcgui.NOTIFICATION_ERROR,
        )
        return

    addon = xbmcaddon.Addon(ADDON_ID)
    addon_path = addon.getAddonInfo("path")

    home_path = xbmcvfs.translatePath("special://home")
    players_path = os.path.join(
        home_path,
        "userdata/addon_data/plugin.video.themoviedb.helper/players",
    )
    player_file = os.path.join(players_path, "comet.select.json")
    source_file = os.path.join(addon_path, "resources/player", "comet.select.json")

    if not xbmcvfs.exists(players_path):
        xbmcvfs.mkdirs(players_path)

    player_exists = xbmcvfs.exists(player_file)
    if player_exists and read_text_file(source_file) == read_text_file(player_file):
        dialog.notification(
            "Comet",
            "TMDB Helper player already installed",
            xbmcgui.NOTIFICATION_INFO,
        )
        return

    if player_exists:
        xbmcvfs.delete(player_file)

    if xbmcvfs.copy(source_file, player_file):
        dialog.notification(
            "Comet",
            "TMDB Helper player updated"
            if player_exists
            else "TMDB Helper player installed",
            xbmcgui.NOTIFICATION_INFO,
        )
        return

    xbmc.log("Failed to copy TMDB Helper player file", xbmc.LOGERROR)
    dialog.notification(
        "Comet",
        "Failed to install TMDB Helper player",
        xbmcgui.NOTIFICATION_ERROR,
    )


if __name__ == "__main__":
    setup_tmdb_helper_player()
