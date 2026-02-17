import base64
from functools import lru_cache

import orjson

from comet.core.models import (ConfigModel, default_config,
                               rtn_ranking_default, rtn_settings_default,
                               settings)


def _normalize_debrid_config(validated_config: dict) -> dict:
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

    if not debrid_entries and not enable_torrent:
        enable_torrent = True

    validated_config["_debridEntries"] = debrid_entries
    validated_config["_enableTorrent"] = enable_torrent

    return validated_config


def _default_validated_config():
    return _DEFAULT_VALIDATED_CONFIG


_DEFAULT_VALIDATED_CONFIG = default_config.copy()
_DEFAULT_VALIDATED_CONFIG["_debridEntries"] = []
_DEFAULT_VALIDATED_CONFIG["_enableTorrent"] = True


@lru_cache(maxsize=4096)
def _parse_and_validate_config(b64config: str):
    try:
        config = orjson.loads(base64.b64decode(b64config).decode())

        validated_config = ConfigModel(**config)
        validated_config = validated_config.model_dump()

        options = validated_config["options"]
        validated_config["options"] = {
            "remove_ranks_under": options["remove_ranks_under"],
            "allow_english_in_languages": options["allow_english_in_languages"],
            "remove_unknown_languages": options["remove_unknown_languages"],
            "remove_all_trash": validated_config["removeTrash"],
        }

        rtn_settings = rtn_settings_default.model_copy(
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

        validated_config["rtnSettings"] = rtn_settings
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

        return validated_config
    except Exception:
        return None


def config_check(b64config: str | None, strict_b64config: bool = False):
    if not b64config:
        return _default_validated_config()

    validated_config = _parse_and_validate_config(b64config)
    if validated_config is not None:
        return validated_config

    if strict_b64config:
        return None

    return _default_validated_config()
