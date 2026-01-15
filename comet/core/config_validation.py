import base64

import orjson

from comet.core.models import (ConfigModel, default_config,
                               rtn_ranking_default, rtn_settings_default,
                               settings)


def _normalize_debrid_config(validated_config: dict) -> dict:
    debrid_entries = []
    enable_torrent = False

    if validated_config.get("debridServices"):
        for entry in validated_config["debridServices"]:
            if isinstance(entry, dict):
                debrid_entries.append(
                    {"service": entry.get("service"), "apiKey": entry.get("apiKey", "")}
                )
            else:
                debrid_entries.append(
                    {
                        "service": entry.service
                        if hasattr(entry, "service")
                        else entry.get("service"),
                        "apiKey": entry.apiKey
                        if hasattr(entry, "apiKey")
                        else entry.get("apiKey", ""),
                    }
                )

        enable_torrent = validated_config.get("enableTorrent", False)
    elif validated_config.get("debridService"):
        legacy_service = validated_config["debridService"]

        if legacy_service == "torrent":
            enable_torrent = True
        else:
            debrid_entries.append(
                {
                    "service": legacy_service,
                    "apiKey": validated_config.get("debridApiKey", ""),
                }
            )

    if not debrid_entries and not enable_torrent:
        enable_torrent = True

    validated_config["_debridEntries"] = debrid_entries
    validated_config["_enableTorrent"] = enable_torrent

    return validated_config


def config_check(b64config: str):
    try:
        config = orjson.loads(base64.b64decode(b64config).decode())

        validated_config = ConfigModel(**config)
        validated_config = validated_config.model_dump()

        for key in list(validated_config["options"].keys()):
            if key not in [
                "remove_ranks_under",
                "allow_english_in_languages",
                "remove_unknown_languages",
            ]:
                validated_config["options"].pop(key)

        validated_config["options"]["remove_all_trash"] = validated_config[
            "removeTrash"
        ]

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
            and not validated_config.get("debridServices")
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
        default = default_config.copy()
        default["_debridEntries"] = []
        default["_enableTorrent"] = True
        return default
