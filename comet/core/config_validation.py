import base64
import json
import re
from functools import lru_cache

from pydantic import ValidationError

from comet.core.credentials import api_credential
from comet.core.models import (
    ConfigModel,
    default_config,
    rtn_ranking_default,
    rtn_settings_default,
    settings,
)
from comet.core.sources import TORRENT_PROVIDER_KINDS


def _normalize_debrid_config(validated_config: dict) -> dict:
    if validated_config.get("schemaVersion") == 2:
        debrid_entries, enable_torrent = _normalize_v2_torrent_providers(
            validated_config
        )
        validated_config["_debridEntries"] = debrid_entries
        validated_config["_enableTorrent"] = enable_torrent
        return validated_config

    debrid_entries = []
    enable_torrent = False

    debrid_services = validated_config["debridServices"]
    if debrid_services:
        debrid_entries = [
            {"service": entry["service"], "apiKey": entry["apiKey"]}
            for entry in debrid_services
        ]
        enable_torrent = validated_config["enableTorrent"]
    else:
        legacy_service = validated_config["debridService"]

        if legacy_service == "torrent":
            enable_torrent = True
        else:
            debrid_entries.append(
                {
                    "service": legacy_service,
                    "apiKey": validated_config["debridApiKey"],
                }
            )

    validated_config["_debridEntries"] = debrid_entries
    validated_config["_enableTorrent"] = enable_torrent

    return validated_config


def _normalize_v2_torrent_providers(
    config: dict,
) -> tuple[list[dict[str, str]], bool]:
    """Resolve canonical v2 torrent providers from their account envelopes."""
    if "bittorrent" not in (config.get("enabledTransports") or ()):
        return [], False
    accounts = config.get("accounts") or {}
    normalized = []
    direct_enabled = False
    for provider in config.get("playbackProviders") or ():
        if not provider.get("enabled"):
            continue
        kind = provider.get("kind")
        if kind not in TORRENT_PROVIDER_KINDS:
            continue
        if kind == "direct_torrent":
            direct_enabled = True
            continue
        account_id = provider.get("accountId")
        credential = api_credential(accounts.get(account_id)) or ""
        normalized.append(
            {
                "configurationId": provider["configurationId"],
                "service": kind,
                "apiKey": credential,
            }
        )
    return normalized, direct_enabled


def _default_validated_config():
    return _DEFAULT_VALIDATED_CONFIG


_DEFAULT_VALIDATED_CONFIG = default_config.copy()
_DEFAULT_VALIDATED_CONFIG["_debridEntries"] = []
_DEFAULT_VALIDATED_CONFIG["_enableTorrent"] = True
_DEFAULT_OPTIONS = default_config["options"]

_MAX_CONFIG_SEGMENT_BYTES = 32 * 1024
_MAX_CONFIG_JSON_BYTES = 24 * 1024
_CONFIG_BASE64 = re.compile(r"^(?:[A-Za-z0-9+/]+={0,2}|[A-Za-z0-9_-]+={0,2})$")


def _reject_nonfinite_json_constant(_value):
    raise ValueError("non-finite JSON number")


def normalize_validated_config(validated_config: dict) -> dict:
    """Build the runtime representation shared by every configuration entrypoint."""
    options = _DEFAULT_OPTIONS | (validated_config["options"] or {})
    validated_config["options"] = {
        "allow_english_in_languages": options["allow_english_in_languages"],
        "remove_unknown_languages": options["remove_unknown_languages"],
        "remove_all_trash": validated_config["removeTrash"],
    }

    validated_config["rtnSettings"] = rtn_settings_default.model_copy(
        update={
            "resolutions": rtn_settings_default.resolutions.model_copy(
                update=validated_config["resolutions"]
            ),
            "options": rtn_settings_default.options.model_copy(
                update=validated_config["options"]
            ),
            "languages": rtn_settings_default.languages.model_copy(
                update=validated_config["languages"]
            ),
        }
    )
    validated_config["rtnRanking"] = rtn_ranking_default

    if (
        settings.PROXY_DEBRID_STREAM
        and settings.PROXY_DEBRID_STREAM_PASSWORD
        == validated_config["debridStreamProxyPassword"]
        and validated_config["debridApiKey"] == ""
        and not validated_config["debridServices"]
    ):
        validated_config["debridService"] = (
            settings.PROXY_DEBRID_STREAM_DEBRID_DEFAULT_SERVICE
        )
        validated_config["debridApiKey"] = (
            settings.PROXY_DEBRID_STREAM_DEBRID_DEFAULT_APIKEY
        )

    validated_config = _normalize_debrid_config(validated_config)
    if validated_config["schemaVersion"] == 2:
        validated_config["enabledTransports"] = tuple(
            transport
            for transport in ("bittorrent", "usenet")
            if transport in validated_config["enabledTransports"]
        )
    return validated_config


@lru_cache(maxsize=512)
def _parse_and_validate_config(b64config: str):
    try:
        encoded_size = len(b64config.encode("ascii"))
    except UnicodeEncodeError:
        return None
    if (
        encoded_size > _MAX_CONFIG_SEGMENT_BYTES
        or len(b64config) % 4 == 1
        or _CONFIG_BASE64.fullmatch(b64config) is None
    ):
        return None
    try:
        padded = b64config + "=" * (-len(b64config) % 4)
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
        if len(decoded) > _MAX_CONFIG_JSON_BYTES:
            return None
        config = json.loads(
            decoded.decode("utf-8"),
            parse_constant=_reject_nonfinite_json_constant,
        )
    except ValueError:
        return None
    try:
        validated_config = ConfigModel.model_validate(config).model_dump()
    except ValidationError:
        return None
    try:
        return normalize_validated_config(validated_config)
    except ValueError:
        return None


def config_check(b64config: str | None):
    if not b64config:
        return _default_validated_config()

    return _parse_and_validate_config(b64config)
