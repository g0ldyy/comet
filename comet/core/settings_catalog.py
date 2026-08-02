"""Backend-owned metadata for every operator-manageable setting."""

from __future__ import annotations

import types
from enum import Enum
from functools import cache
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict
from pydantic.fields import FieldInfo, PydanticUndefined

from comet.core.operator_settings import BOOTSTRAP_SETTING_KEYS
from comet.core.settings_policy import apply_mode

SENSITIVE_SETTING_KEYS = frozenset(
    {
        "DATABASE_URL",
        "DATABASE_PATH",
        "DATABASE_READ_REPLICA_URLS",
        "ADMIN_DASHBOARD_PASSWORD",
        "CONFIGURE_PAGE_PASSWORD",
        "PUBLIC_API_TOKEN",
        "PROMETHEUS_AUTH_TOKEN",
        "PROMETHEUS_QUERY_TOKEN",
        "INDEXER_MANAGER_API_KEY",
        "JACKETT_API_KEY",
        "PROWLARR_API_KEY",
        "MEDIAFUSION_API_PASSWORD",
        "AIOSTREAMS_USER_UUID_AND_PASSWORD",
        "DEBRIDIO_API_KEY",
        "DEBRIDIO_PROVIDER_KEY",
        "PROXY_DEBRID_STREAM_PASSWORD",
        "PROXY_DEBRID_STREAM_DEBRID_DEFAULT_APIKEY",
        "USENET_NATIVE_ACCESS_TOKEN",
        "USENET_NATIVE_SERVERS",
        "COMET_CAPABILITY_SECRET",
        "TMDB_READ_ACCESS_TOKEN",
        "GLOBAL_PROXY_URL",
        "AIOSTREAMS_PROXY_URL",
        "ANIMETOSHO_PROXY_URL",
        "BITMAGNET_PROXY_URL",
        "COMET_PROXY_URL",
        "DEBRIDIO_PROXY_URL",
        "JACKETT_PROXY_URL",
        "JACKETTIO_PROXY_URL",
        "MEDIAFUSION_PROXY_URL",
        "NEKOBT_PROXY_URL",
        "NYAA_PROXY_URL",
        "PEERFLIX_PROXY_URL",
        "PROWLARR_PROXY_URL",
        "SEADEX_PROXY_URL",
        "STREMTHRU_PROXY_URL",
        "TORRENTIO_PROXY_URL",
        "TORRENTSDB_PROXY_URL",
        "ZILEAN_PROXY_URL",
        "COMETNET_API_KEY",
        "COMETNET_KEY_PASSWORD",
        "COMETNET_NETWORK_PASSWORD",
        "USENET_RUNTIME_DIR",
        "USENET_LOCAL_DATA_DIR",
        "USENET_ARTIFACT_DIR",
        "COMET_USENET_ENGINE_BINARY",
        "PUBLIC_API_TOKEN_FILE",
        "PROMETHEUS_AUTH_TOKEN_FILE",
        "COMET_CAPABILITY_SECRET_FILE",
        "COMETNET_KEYS_DIR",
    }
)

_STRUCTURED_EDITORS = {
    "DATABASE_READ_REPLICA_URLS": "read_replicas",
    "SCRAPER_TIMEOUT_OVERRIDES": "scraper_timeouts",
    "USENET_NATIVE_SERVERS": "nntp_servers",
    "COMETNET_BOOTSTRAP_NODES": "network_nodes",
    "COMETNET_MANUAL_PEERS": "network_nodes",
    "COMETNET_TRUSTED_POOLS": "network_pools",
}

_SETTING_CHOICES: dict[str, tuple[Any, ...]] = {
    "DATABASE_TYPE": ("sqlite", "postgresql"),
    "INDEXER_MANAGER_TYPE": ("jackett", "prowlarr"),
    "PROXY_ETHOS": ("always", "on_failure", "never"),
    "PROXY_DEBRID_STREAM_DEBRID_DEFAULT_SERVICE": (
        "realdebrid",
        "alldebrid",
        "premiumize",
        "torbox",
        "debrider",
        "easydebrid",
        "debridlink",
        "offcloud",
        "pikpak",
    ),
    "COMETNET_CONTRIBUTION_MODE": ("full", "consumer", "source", "leech"),
}

_CATEGORY_RULES = (
    ("database", ("DATABASE_",)),
    (
        "access_security",
        (
            "ADMIN_",
            "CONFIGURE_",
            "PUBLIC_API_",
            "PUBLIC_BASE_",
            "COMET_CAPABILITY_",
        ),
    ),
    ("usenet", ("USENET_", "COMET_USENET_")),
    ("cometnet", ("COMETNET_",)),
    ("background_scraper", ("BACKGROUND_", "DMM_")),
    (
        "scrapers_proxies",
        (
            "SCRAPE_",
            "JACKETT_",
            "PROWLARR_",
            "ZILEAN_",
            "TORRENTIO_",
            "MEDIAFUSION_",
            "AIOSTREAMS_",
            "ANIMETOSHO_",
            "BITMAGNET_",
            "DEBRIDIO_",
            "JACKETTIO_",
            "NEKOBT_",
            "NYAA_",
            "PEERFLIX_",
            "SEADEX_",
            "STREMTHRU_",
            "TORRENTSDB_",
            "GLOBAL_PROXY_",
            "PROXY_ETHOS",
        ),
    ),
    (
        "debrid_stream_proxy",
        ("DEBRID_", "PROXY_DEBRID_", "DISABLE_TORRENT_", "TORRENT_DISABLED_"),
    ),
    ("anime_metadata", ("ANIME_", "TMDB_", "METADATA_", "DIGITAL_RELEASE_")),
    ("prometheus_logging", ("PROMETHEUS_", "LOG_", "NO_COLOR", "METRICS_")),
    (
        "cache_http",
        (
            "HTTP_",
            "CACHE_",
            "TORRENT_CACHE_",
            "LIVE_TORRENT_CACHE_",
            "FILTER_PARSE_CACHE_",
        ),
    ),
    (
        "discovery_indexers",
        (
            "INDEXER_",
            "DOWNLOAD_TORRENT_",
            "DOWNLOAD_GENERIC_",
            "GET_TORRENT_",
            "MAGNET_",
            "CATALOG_",
        ),
    ),
    ("server_lifecycle", ("FASTAPI_", "USE_GUNICORN", "GUNICORN_", "EXECUTOR_")),
    ("general_branding", ("ADDON_", "CUSTOM_HEADER_", "REMOVE_ADULT_")),
)


class SettingCatalogEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    category: str
    value_kind: str
    nullable: bool
    choices: tuple[Any, ...] = ()
    default: Any = None
    has_default: bool = True
    unit: str | None = None
    item_kind: str | None = None
    input_kind: str = "text"
    sensitive: bool = False
    restart_required: bool = True
    apply_mode: Literal["live", "component", "process", "deployment"] = "live"
    deployment_owned: bool = False
    structured_editor: str | None = None


def _category(key: str) -> str:
    for category, prefixes in _CATEGORY_RULES:
        if key.startswith(prefixes):
            return category
    return "advanced_tuning"


def _unit(key: str) -> str | None:
    if key.endswith(("_BYTES", "_SIZE")):
        return "bytes"
    if key.endswith(("_TIMEOUT", "_INTERVAL", "_TTL", "_AGE", "_DELAY")):
        return "seconds"
    if key.endswith("_PORT"):
        return "port"
    if key.endswith(("_RATIO", "_RATE")):
        return "ratio"
    return None


def _simple_kind(annotation: Any) -> str:
    origin = get_origin(annotation)
    if annotation is bool:
        return "boolean"
    if annotation is int:
        return "integer"
    if annotation is float:
        return "number"
    if annotation is str:
        return "string"
    if origin is list:
        return "list"
    if origin is dict:
        return "map"
    return "json"


def _type_metadata(
    annotation: Any,
) -> tuple[str, bool, tuple[Any, ...], str | None]:
    origin = get_origin(annotation)
    args = get_args(annotation)
    nullable = type(None) in args
    concrete = tuple(arg for arg in args if arg is not type(None))
    if origin in {types.UnionType, Union} and nullable and len(concrete) == 1:
        annotation = concrete[0]
        origin = get_origin(annotation)
        args = get_args(annotation)
    if origin is Literal:
        return "enum", nullable, tuple(args), None
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return "enum", nullable, tuple(member.value for member in annotation), None
    if annotation is bool:
        return "boolean", nullable, (), None
    if annotation is int:
        return "integer", nullable, (), None
    if annotation is float:
        return "number", nullable, (), None
    if annotation is str:
        return "string", nullable, (), None
    if origin is list:
        return "list", nullable, (), _simple_kind(args[0])
    if origin is dict:
        return "map", nullable, (), _simple_kind(args[1])
    if origin in {types.UnionType, Union} and set(concrete).issubset({bool, str}):
        return "enum", nullable, (False, True, "live", "background"), None
    if origin in {types.UnionType, Union} and str in concrete:
        list_types = tuple(
            candidate for candidate in concrete if get_origin(candidate) is list
        )
        if len(list_types) == 1 and len(concrete) == 2:
            return (
                "string_or_list",
                nullable,
                (),
                _simple_kind(get_args(list_types[0])[0]),
            )
    return "json", nullable, (), None


def _input_kind(key: str) -> str:
    if key == "CUSTOM_HEADER_HTML":
        return "multiline"
    if key.endswith(("_URL", "_ORIGINS")):
        return "url"
    if key.endswith(("_PATH", "_FILE", "_DIR", "_BINARY")):
        return "path"
    return "text"


def _default(field: FieldInfo) -> tuple[Any, bool]:
    if field.default is not PydanticUndefined:
        return field.default, True
    return None, field.default_factory is not None


def _entry(key: str, field: FieldInfo) -> SettingCatalogEntry:
    kind, nullable, choices, item_kind = _type_metadata(field.annotation)
    choices = _SETTING_CHOICES.get(key, choices)
    if choices:
        kind = "enum"
    default, has_default = _default(field)
    sensitive = key in SENSITIVE_SETTING_KEYS
    mode = apply_mode(key)
    return SettingCatalogEntry(
        key=key,
        category=_category(key),
        value_kind=kind,
        nullable=nullable,
        choices=choices,
        default=default,
        has_default=has_default,
        unit=_unit(key),
        item_kind=item_kind,
        input_kind=_input_kind(key),
        sensitive=sensitive,
        restart_required=mode == "process",
        apply_mode=mode,
        deployment_owned=key in BOOTSTRAP_SETTING_KEYS,
        structured_editor=_STRUCTURED_EDITORS.get(key),
    )


@cache
def build_settings_catalog() -> tuple[SettingCatalogEntry, ...]:
    from comet.core.models import AppSettings
    from comet.observability.logging import LoggingSettings

    entries = [
        _entry(key, field)
        for key, field in (
            *(
                (key, field)
                for key, field in AppSettings.model_fields.items()
                if key.isupper()
            ),
            *LoggingSettings.model_fields.items(),
        )
    ]
    return tuple(entries)
