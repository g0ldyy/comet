"""Easynews direct HTTP playback provider."""

from urllib.parse import quote

from comet.playback.base import (
    Actionability,
    BytePath,
    ProviderDescriptor,
    ProviderStatus,
    Readiness,
)
from comet.usenet.easynews import credential
from comet.usenet.outbound import http_url_with_basic_auth

_MEMBER_DOWNLOAD_BASE = "https://members.easynews.com/dl"


def _path_token(value: object, maximum: int, field: str) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise ValueError(f"Easynews {field} is invalid")
    return value


def direct_target(payload: dict) -> str:
    """Build the authenticated member download URL represented by a search row."""
    if not isinstance(payload, dict):
        raise ValueError("Easynews locator is invalid")
    farm = _path_token(payload.get("dlFarm"), 128, "farm")
    port = _path_token(payload.get("dlPort"), 128, "port")
    content_hash = _path_token(payload.get("hash"), 256, "hash")
    identifier = payload.get("id", "")
    if identifier != "":
        identifier = _path_token(identifier, 256, "identifier")
    extension = _path_token(
        payload.get("extension"),
        32,
        "extension",
    )
    filename = payload.get("filename")
    if not isinstance(filename, str) or not 1 <= len(filename) <= 512:
        raise ValueError("Easynews filename is invalid")
    encoded_extension = quote(extension, safe="")
    return (
        f"{_MEMBER_DOWNLOAD_BASE}/{quote(farm, safe='')}/{quote(port, safe='')}/"
        f"{quote(content_hash + identifier, safe='')}.{encoded_extension}/"
        f"{quote(filename, safe='')}.{encoded_extension}"
    )


class EasynewsProvider:
    descriptor = ProviderDescriptor(
        kind="easynews",
        label="Easynews",
        accepted_locator_kinds=frozenset({"easynews_http"}),
        byte_paths=frozenset({BytePath.CLOUD_REDIRECT}),
        mutates_upstream=False,
    )

    def __init__(self, _session, username: str, password: str):
        self._username = credential(username)
        self._password = credential(password)

    @staticmethod
    def has_valid_options(options: object) -> bool:
        if not isinstance(options, dict):
            return False
        try:
            credential(options.get("username"))
            credential(options.get("password"))
        except ValueError:
            return False
        return True

    def playback_url(self, payload: dict) -> str:
        return http_url_with_basic_auth(
            direct_target(payload),
            self._username,
            self._password,
        )

    async def validate_config(self, config: dict) -> ProviderStatus:
        try:
            credential(config.get("username", self._username))
            credential(config.get("password", self._password))
        except ValueError:
            return ProviderStatus(
                Readiness.TERMINAL_FAILURE,
                Actionability.NONE,
                code="credentials_required",
                auth_failed=True,
            )
        return ProviderStatus(
            Readiness.UNKNOWN,
            Actionability.SERVER_ON_DEMAND,
        )
