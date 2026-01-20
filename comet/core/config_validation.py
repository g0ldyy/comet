import base64
from urllib.parse import urlparse

import orjson

from comet.core.models import (ConfigModel, default_config,
                               rtn_ranking_default, rtn_settings_default,
                               settings)


def config_check(b64config: str):
    try:
        config = orjson.loads(base64.b64decode(b64config).decode())

        validated_config = ConfigModel(**config)
        validated_config = validated_config.model_dump(by_alias=True)

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

        # Validate metadata providers
        if "metadataProviders" in validated_config and validated_config["metadataProviders"]:
            providers = validated_config["metadataProviders"]
            seen_prefixes = set()
            valid_providers = []
            
            for provider in providers:
                # Validate required fields
                if not isinstance(provider, dict):
                    continue
                if "name" not in provider or "prefix" not in provider or "url" not in provider:
                    continue
                
                name = str(provider["name"]).strip()
                prefix = str(provider["prefix"]).strip()
                url = str(provider["url"]).strip()
                
                # Skip empty fields
                if not name or not prefix or not url:
                    continue
                
                # Check for unique prefixes
                if prefix in seen_prefixes:
                    continue
                seen_prefixes.add(prefix)
                
                # Validate URL format
                try:
                    parsed = urlparse(url)
                    if not parsed.scheme or not parsed.netloc:
                        continue
                    # Only allow http and https
                    if parsed.scheme not in ["http", "https"]:
                        continue
                except Exception:
                    continue
                
                # Strip trailing slashes from URL
                url = url.rstrip("/")
                
                valid_providers.append({
                    "name": name,
                    "prefix": prefix,
                    "url": url
                })
            
            validated_config["metadataProviders"] = valid_providers
        else:
            validated_config["metadataProviders"] = []

        # Validate title mappings (from -> to)
        if "titleMappings" in validated_config and validated_config["titleMappings"]:
            mappings = validated_config["titleMappings"]
            seen_from = set()
            valid_mappings = []

            for mapping in mappings:
                if not isinstance(mapping, dict):
                    continue

                from_value = str(mapping.get("from", "")).strip()
                to_value = str(mapping.get("to", "")).strip()

                if not from_value or not to_value:
                    continue

                key = from_value.lower()
                if key in seen_from:
                    continue
                seen_from.add(key)

                valid_mappings.append({"from": from_value, "to": to_value})

            validated_config["titleMappings"] = valid_mappings
        else:
            validated_config["titleMappings"] = []

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
