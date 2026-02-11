import os
import subprocess
import time
import traceback
from urllib.parse import urljoin

import requests
import xbmc
import xbmcaddon
import xbmcgui

ADDON_ID = "plugin.video.comet"
REQUEST_TIMEOUT = 20
POLL_INTERVAL_SECONDS = 3
HTTP_SESSION = requests.Session()


def normalize_base_url(url: str):
    return url.rstrip("/")


def open_configuration_page(url: str):
    os_windows = xbmc.getCondVisibility("system.platform.windows")
    os_osx = xbmc.getCondVisibility("system.platform.osx")
    os_linux = xbmc.getCondVisibility("system.platform.linux")
    os_android = xbmc.getCondVisibility("System.Platform.Android")

    try:
        if os_osx:
            subprocess.run(["open", url], check=True)
            return
        if os_windows:
            os.startfile(url)
            return
        if os_linux and not os_android:
            subprocess.run(["xdg-open", url], check=True)
            return
        if os_android:
            safe_url = url.replace('"', "%22")
            xbmc.executebuiltin(
                f'StartAndroidActivity("","android.intent.action.VIEW","","{safe_url}")'
            )
            return
    except Exception as exc:
        xbmc.log(f"Failed to open configuration page: {exc}", xbmc.LOGERROR)


def _post_json(url: str, payload: dict):
    response = HTTP_SESSION.post(url, json=payload, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def _get_json(url: str):
    response = HTTP_SESSION.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def configure_comet():
    try:
        addon = xbmcaddon.Addon(ADDON_ID)
        dialog = xbmcgui.Dialog()
        monitor = xbmc.Monitor()

        base_url = addon.getSetting("base_url")
        secret_string = addon.getSetting("secret_string")

        entered_url = dialog.input("Comet base URL", base_url)
        if not entered_url:
            return

        base_url = normalize_base_url(entered_url)
        addon.setSetting("base_url", base_url)

        entered_secret = dialog.input(
            "Comet configuration (optional)",
            secret_string,
            option=xbmcgui.ALPHANUM_HIDE_INPUT,
        )
        if entered_secret is not None:
            secret_string = entered_secret
            addon.setSetting("secret_string", secret_string)

        try:
            data = _post_json(
                urljoin(base_url + "/", "kodi/generate_setup_code"),
                {"secret_string": secret_string},
            )
        except requests.RequestException as exc:
            dialog.notification(
                "Comet",
                "Failed to generate Kodi setup code",
                xbmcgui.NOTIFICATION_ERROR,
            )
            xbmc.log(f"Failed to generate setup code: {exc}", xbmc.LOGERROR)
            return

        try:
            code = data["code"]
            configure_url = data["configure_url"]
            expires_in = int(data["expires_in"])
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError("Invalid response from /kodi/generate_setup_code") from exc

        dialog.ok(
            "Comet Kodi Setup",
            f"Setup code: {code}\nOpen the configuration page and complete setup before expiration.",
        )

        if dialog.yesno(
            "Comet Kodi Setup",
            "Open the Comet configuration page now?",
        ):
            open_configuration_page(configure_url)

        dialog.notification(
            "Comet",
            f"Waiting for setup code {code}",
            xbmcgui.NOTIFICATION_INFO,
        )

        deadline = time.time() + expires_in
        while time.time() < deadline:
            try:
                manifest_data = _get_json(
                    urljoin(base_url + "/", f"kodi/get_manifest/{code}")
                )
            except requests.HTTPError as exc:
                response = exc.response
                if response is None or response.status_code != 404:
                    xbmc.log(f"Polling setup status failed: {exc}", xbmc.LOGWARNING)
            except requests.RequestException as exc:
                xbmc.log(f"Polling setup status failed: {exc}", xbmc.LOGWARNING)
            else:
                addon.setSetting("secret_string", manifest_data["secret_string"])
                dialog.notification(
                    "Comet",
                    "Kodi setup complete",
                    xbmcgui.NOTIFICATION_INFO,
                )
                return

            if monitor.waitForAbort(POLL_INTERVAL_SECONDS):
                return

        dialog.notification(
            "Comet",
            "Setup code expired. Run setup again.",
            xbmcgui.NOTIFICATION_ERROR,
        )
    except Exception:
        xbmc.log(
            "Comet Kodi setup crashed:\n" + traceback.format_exc(),
            xbmc.LOGERROR,
        )
        xbmcgui.Dialog().notification(
            "Comet",
            "Setup failed (check Kodi log)",
            xbmcgui.NOTIFICATION_ERROR,
        )


if __name__ == "__main__":
    configure_comet()
