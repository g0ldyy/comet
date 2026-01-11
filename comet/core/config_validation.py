import base64

import orjson

from comet.core.models import (ConfigModel, default_config,
                               rtn_ranking_default, rtn_settings_default,
                               settings)


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
        ):
            validated_config["debridService"] = (
                settings.PROXY_DEBRID_STREAM_DEBRID_DEFAULT_SERVICE
            )
            validated_config["debridApiKey"] = (
                settings.PROXY_DEBRID_STREAM_DEBRID_DEFAULT_APIKEY
            )

        return validated_config
    except Exception:
        return default_config  # if it doesn't pass, return default config


def is_default_config(config: dict) -> bool:
    if config is None:
        return True

    ignored_fields = {
        "debridApiKey",
        "debridStreamProxyPassword",
        "rtnSettings",
        "rtnRanking",
        "debridService",
    }

    if config.get("debridService") != "torrent":
        return False

    for field, default_value in default_config.items():
        if field in ignored_fields:
            continue

        config_value = config.get(field)

        if field == "resolutions":
            if isinstance(config_value, dict):
                for res, enabled in config_value.items():
                    if enabled is False:
                        return False
            continue

        if field == "languages":
            config_lang = config_value or {}
            default_lang = default_value or {}
            if config_lang.get("exclude", []) != default_lang.get("exclude", []):
                return False
            if config_lang.get("preferred", []) != default_lang.get("preferred", []):
                return False
            continue

        if field == "options":
            config_opts = config_value or {}
            default_opts = default_value or {}
            for key in [
                "remove_ranks_under",
                "allow_english_in_languages",
                "remove_unknown_languages",
            ]:
                if config_opts.get(key) != default_opts.get(key):
                    return False
            continue

        if config_value != default_value:
            return False

    return True
