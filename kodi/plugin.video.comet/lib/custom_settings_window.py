import os
import re
import subprocess
import time
from urllib.parse import urljoin

import requests
import xbmc
import xbmcaddon
import xbmcgui

try:
    from .diagnostics import emit, run_boundary
    from .http_json import (
        JsonHttpError,
        normalize_api_prefix,
        request_json,
        response_status,
        validate_http_url,
    )
except ImportError:
    from diagnostics import emit, run_boundary
    from http_json import (
        JsonHttpError,
        normalize_api_prefix,
        request_json,
        response_status,
        validate_http_url,
    )

ADDON_ID = "plugin.video.comet"
REQUEST_TIMEOUT = 20
POLL_INTERVAL_SECONDS = 3
MAX_SETUP_POLL_SECONDS = 600
HTTP_SESSION = requests.Session()
_SETUP_CODE = re.compile(r"[0-9a-f]{8}", re.ASCII)
_CONFIGURATION = re.compile(r"[A-Za-z0-9_-]*", re.ASCII)


def normalize_base_url(url: str):
    return validate_http_url(url, base_only=True)


def open_configuration_page(url: str):
    os_windows = xbmc.getCondVisibility("system.platform.windows")
    os_osx = xbmc.getCondVisibility("system.platform.osx")
    os_linux = xbmc.getCondVisibility("system.platform.linux")
    os_android = xbmc.getCondVisibility("System.Platform.Android")

    try:
        if os_osx:
            subprocess.run(
                ["open", url],
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            return
        if os_windows:
            os.startfile(url)
            return
        if os_linux and not os_android:
            subprocess.run(
                ["xdg-open", url],
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            return
        if os_android:
            safe_url = url.replace('"', "%22")
            xbmc.executebuiltin(
                f'StartAndroidActivity("","android.intent.action.VIEW","","{safe_url}")'
            )
            return
    except Exception as exc:
        emit("kodi.setup.failed", error=exc, outcome="failed")


def _post_json(url: str, payload: dict):
    return request_json(
        HTTP_SESSION,
        "POST",
        url,
        timeout=REQUEST_TIMEOUT,
        payload=payload,
    )


def _get_json(url: str):
    return request_json(
        HTTP_SESSION,
        "GET",
        url,
        timeout=REQUEST_TIMEOUT,
    )


def _parse_setup_code_response(data):
    if not isinstance(data, dict):
        raise ValueError("Invalid response from /kodi/generate_setup_code")

    code = data.get("code")
    configure_url = data.get("configure_url")
    expires_in = data.get("expires_in")
    stremio_api_prefix = data.get("stremio_api_prefix", "")
    if (
        not isinstance(code, str)
        or _SETUP_CODE.fullmatch(code) is None
        or not isinstance(configure_url, str)
        or type(expires_in) is not int
        or not 0 < expires_in <= MAX_SETUP_POLL_SECONDS
        or not isinstance(stremio_api_prefix, str)
    ):
        raise ValueError("Invalid response from /kodi/generate_setup_code")

    try:
        configure_url = validate_http_url(configure_url)
        stremio_api_prefix = normalize_api_prefix(stremio_api_prefix)
    except JsonHttpError as exc:
        raise ValueError("Invalid response from /kodi/generate_setup_code") from exc
    return code, configure_url, expires_in, stremio_api_prefix


def _parse_manifest_response(data):
    if not isinstance(data, dict):
        raise ValueError("Invalid response from /kodi/get_manifest")
    secret_string = data.get("secret_string")
    stremio_api_prefix = data.get("stremio_api_prefix", "")
    if (
        not isinstance(secret_string, str)
        or _CONFIGURATION.fullmatch(secret_string) is None
        or not isinstance(stremio_api_prefix, str)
    ):
        raise ValueError("Invalid response from /kodi/get_manifest")
    try:
        stremio_api_prefix = normalize_api_prefix(stremio_api_prefix)
    except JsonHttpError as exc:
        raise ValueError("Invalid response from /kodi/get_manifest") from exc
    return secret_string, stremio_api_prefix


def configure_comet():
    poll_degraded = False
    try:
        addon = xbmcaddon.Addon(ADDON_ID)
        dialog = xbmcgui.Dialog()
        monitor = xbmc.Monitor()

        base_url = addon.getSetting("base_url")
        secret_string = addon.getSetting("secret_string")

        entered_url = dialog.input("Comet base URL", base_url)
        if not entered_url:
            return

        try:
            base_url = normalize_base_url(entered_url)
        except JsonHttpError:
            dialog.notification(
                "Comet",
                "Enter a valid HTTP(S) Comet base URL",
                xbmcgui.NOTIFICATION_ERROR,
            )
            return
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
        except (requests.RequestException, JsonHttpError) as exc:
            dialog.notification(
                "Comet",
                "Failed to generate Kodi setup code",
                xbmcgui.NOTIFICATION_ERROR,
            )
            emit("kodi.setup.failed", error=exc, outcome="failed")
            return

        code, configure_url, expires_in, stremio_api_prefix = (
            _parse_setup_code_response(data)
        )

        addon.setSetting("stremio_api_prefix", stremio_api_prefix)

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
            except (requests.RequestException, JsonHttpError) as exc:
                status = response_status(exc)
                if status != 404 and not poll_degraded:
                    emit(
                        "kodi.setup.poll.degraded",
                        xbmc.LOGWARNING,
                        error=exc,
                        status=status,
                        outcome="failed",
                    )
                    poll_degraded = True
            else:
                try:
                    paired_secret, paired_prefix = _parse_manifest_response(
                        manifest_data
                    )
                except ValueError as exc:
                    if not poll_degraded:
                        emit(
                            "kodi.setup.poll.degraded",
                            xbmc.LOGWARNING,
                            error=exc,
                            outcome="failed",
                        )
                        poll_degraded = True
                else:
                    if poll_degraded:
                        emit(
                            "kodi.setup.poll.recovered",
                            xbmc.LOGINFO,
                            outcome="ok",
                        )
                        poll_degraded = False
                    addon.setSetting("secret_string", paired_secret)
                    addon.setSetting("stremio_api_prefix", paired_prefix)
                    dialog.notification(
                        "Comet",
                        "Kodi setup complete",
                        xbmcgui.NOTIFICATION_INFO,
                    )
                    emit(
                        "kodi.setup.completed",
                        xbmc.LOGINFO,
                        outcome="ok",
                    )
                    return

            if monitor.waitForAbort(POLL_INTERVAL_SECONDS):
                return

        dialog.notification(
            "Comet",
            "Setup code expired. Run setup again.",
            xbmcgui.NOTIFICATION_ERROR,
        )
        emit("kodi.setup.failed", outcome="timeout")
    except Exception as exc:
        emit("kodi.setup.failed", error=exc, outcome="failed")
        xbmcgui.Dialog().notification(
            "Comet",
            "Setup failed (check Kodi log)",
            xbmcgui.NOTIFICATION_ERROR,
        )


if __name__ == "__main__":
    run_boundary("kodi.setup.failed", configure_comet)
