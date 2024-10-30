from typing import Any

from comet.debrid.stremthru import StremThru
from comet.utils.models import settings


def is_proxy_stream_enabled(config: dict[str, Any]):
    return (
        bool(settings.PROXY_DEBRID_STREAM) and config["debridStreamProxyPassword"] != ""
    )


def is_proxy_stream_authed(config: dict[str, Any]):
    return settings.PROXY_DEBRID_STREAM_PASSWORD == config["debridStreamProxyPassword"]


def should_use_stremthru(config: dict[str, Any]):
    return config["stremthruUrl"] and StremThru.is_supported_store(
        config["debridService"]
    )


def should_skip_proxy_stream(config: dict[str, Any]):
    return config["stremthruUrl"] and config["debridService"] == "stremthru"


def should_use_fallback_debrid_config(config: dict[str, Any]):
    if is_proxy_stream_authed(config) and config["debridApiKey"] == "":
        return True

    return False


def prepare_debrid_config(config: dict[str, Any]):
    if should_use_fallback_debrid_config(config):
        config["debridService"] = settings.PROXY_DEBRID_STREAM_DEBRID_DEFAULT_SERVICE
        config["debridApiKey"] = settings.PROXY_DEBRID_STREAM_DEBRID_DEFAULT_APIKEY

    if not config["stremthruUrl"]:
        config["stremthruUrl"] = settings.STREMTHRU_DEFAULT_URL
