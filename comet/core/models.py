import math
import os
import re
import secrets
import stat
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from urllib.parse import unquote_to_bytes, urlsplit

import RTN
from databases import Database
from databases.backends.sqlite import SQLiteConnection
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)
from pydantic_settings import SettingsConfigDict
from RTN import DefaultRanking, SettingsModel
from RTN.models import (
    AudioRankModel,
    CustomRank,
    CustomRanksConfig,
    ExtrasRankModel,
    HdrRankModel,
    LanguagesConfig,
    OptionsConfig,
    QualityRankModel,
    ResolutionConfig,
    RipsRankModel,
    TrashRankModel,
)
from sqlalchemy.engine import URL, make_url

from comet.core.build_metadata import (
    normalize_branch,
    normalize_build_date,
    normalize_commit,
)
from comet.core.db_router import ReplicaAwareDatabase
from comet.core.operator_settings import (
    LiveSettings,
    effective_model_inputs,
    effective_settings_payload,
    validate_effective_setting_keys,
)
from comet.core.scrape import normalize_scraper_timeout_selector
from comet.core.server_settings import ServerSettings
from comet.core.sources import USENET_PLAYBACK_PROVIDER_KINDS
from comet.usenet.nntp_config import parse_instance_servers, parse_personal_servers
from comet.usenet.stremio_nntp_config import validate_handoff_config
from comet.utils.parsing import parse_url_scrape_mode
from comet.utils.proxy import SUPPORTED_PROXY_SCHEMES

_comet_fk_enabled = False
_SQLITE_BUSY_TIMEOUT_MS = 30000
_MAX_PERSISTED_TOKEN_BYTES = 4_096
_PUBLIC_API_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,256}")
_MAX_DATABASE_URL_BYTES = 4_096
_MAX_DATABASE_REPLICAS = 64
VALID_DEBRID_SERVICES = (
    "realdebrid",
    "alldebrid",
    "premiumize",
    "torbox",
    "debrider",
    "easydebrid",
    "debridlink",
    "offcloud",
    "pikpak",
)
_POSTGRES_DRIVERS = frozenset({"postgres", "postgresql", "postgresql+asyncpg"})
_OPERATOR_CREDENTIAL_FIELDS = (
    "PROMETHEUS_AUTH_TOKEN",
    "PROMETHEUS_QUERY_TOKEN",
    "INDEXER_MANAGER_API_KEY",
    "JACKETT_API_KEY",
    "PROWLARR_API_KEY",
    "DEBRIDIO_API_KEY",
    "DEBRIDIO_PROVIDER_KEY",
    "PROXY_DEBRID_STREAM_PASSWORD",
    "PROXY_DEBRID_STREAM_DEBRID_DEFAULT_APIKEY",
    "TMDB_READ_ACCESS_TOKEN",
    "COMETNET_API_KEY",
    "COMETNET_KEY_PASSWORD",
    "COMETNET_NETWORK_PASSWORD",
)
_SCRAPER_PROXY_FIELDS = (
    "AIOSTREAMS_PROXY_URL",
    "ANIMETOSHO_PROXY_URL",
    "BITMAGNET_PROXY_URL",
    "COMET_PROXY_URL",
    "DEBRIDIO_PROXY_URL",
    "DMM_PROXY_URL",
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
)
_SCRAPER_URL_FIELDS = (
    "INDEXER_MANAGER_URL",
    "JACKETT_URL",
    "PROWLARR_URL",
    "STREMTHRU_SCRAPE_URL",
    "BITMAGNET_URL",
    "COMET_URL",
    "ZILEAN_URL",
    "TORRENTIO_URL",
    "MEDIAFUSION_URL",
    "AIOSTREAMS_URL",
    "JACKETTIO_URL",
)
_OPTIONAL_URL_FIELDS = frozenset(
    {
        "AIOSTREAMS_URL",
        "JACKETTIO_URL",
        "PUBLIC_BASE_URL",
        "PROMETHEUS_QUERY_URL",
        "USENET_EXPORT_BASE_URL",
    }
)
_SCRAPER_ENDPOINT_REQUIREMENTS = {
    "SCRAPE_AIOSTREAMS": "AIOSTREAMS_URL",
    "SCRAPE_BITMAGNET": "BITMAGNET_URL",
    "SCRAPE_COMET": "COMET_URL",
    "SCRAPE_JACKETT": "JACKETT_URL",
    "SCRAPE_JACKETTIO": "JACKETTIO_URL",
    "SCRAPE_MEDIAFUSION": "MEDIAFUSION_URL",
    "SCRAPE_PROWLARR": "PROWLARR_URL",
    "SCRAPE_STREMTHRU": "STREMTHRU_SCRAPE_URL",
    "SCRAPE_TORRENTIO": "TORRENTIO_URL",
    "SCRAPE_ZILEAN": "ZILEAN_URL",
}
_SCRAPER_CREDENTIAL_REQUIREMENTS = {
    "SCRAPE_JACKETT": ("JACKETT_API_KEY",),
    "SCRAPE_PROWLARR": ("PROWLARR_API_KEY",),
    "SCRAPE_DEBRIDIO": (
        "DEBRIDIO_API_KEY",
        "DEBRIDIO_PROVIDER",
        "DEBRIDIO_PROVIDER_KEY",
    ),
}


def _generate_secret() -> str:
    return secrets.token_urlsafe(32)


def normalize_indexer_name(value: str) -> str:
    return value.replace(" ", "").casefold()


def _bounded_path(value: object, *, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty bounded path")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError(f"{field} must be valid UTF-8") from None
    if size > 4_096 or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ValueError(f"{field} must be a non-empty bounded path")
    return value


def _parse_postgres_url(value: object) -> URL:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError("database URL must be a bounded PostgreSQL URL")
    try:
        if len(value.encode("utf-8")) > _MAX_DATABASE_URL_BYTES:
            raise ValueError
    except (UnicodeEncodeError, ValueError):
        raise ValueError("database URL must be a bounded PostgreSQL URL") from None
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("database URL must be a bounded PostgreSQL URL")

    candidate = value if "://" in value else f"postgresql://{value}"
    try:
        parsed = make_url(candidate)
        port = parsed.port
    except (TypeError, ValueError):
        raise ValueError("database URL must be a valid PostgreSQL URL") from None
    if parsed.drivername not in _POSTGRES_DRIVERS:
        raise ValueError("database URL must use PostgreSQL")
    socket_host = parsed.query.get("host")
    if (
        (not parsed.host and not socket_host)
        or not parsed.database
        or (port is not None and not 1 <= port <= 65_535)
    ):
        raise ValueError("database URL requires a host and database name")
    for component in (
        parsed.username,
        parsed.password,
        parsed.host,
        parsed.database,
        socket_host,
    ):
        if isinstance(component, str) and (
            not component
            or any(
                ord(character) < 32 or ord(character) == 127 for character in component
            )
        ):
            raise ValueError("database URL contains an invalid component")
    return parsed


def _canonical_postgres_url(value: str) -> str:
    return _parse_postgres_url(value).render_as_string(hide_password=False)


def _normalize_http_url(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError("configured URL must be a bounded HTTP(S) URL")
    try:
        if len(value.encode("utf-8")) > 4_096:
            raise ValueError
    except (UnicodeEncodeError, ValueError):
        raise ValueError("configured URL must be a bounded HTTP(S) URL") from None
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("configured URL must be a bounded HTTP(S) URL")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("configured URL has an invalid port") from None
    host = parsed.hostname
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65_535)
        or not host.isascii()
        or any(character.isspace() or ord(character) < 33 for character in host)
    ):
        raise ValueError("configured URL must be a bounded HTTP(S) URL")
    return value.rstrip("/")


def _normalize_websocket_url(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError("configured peer URL must be a bounded WebSocket URL")
    try:
        if len(value.encode("utf-8")) > 4_096:
            raise ValueError
        parsed = urlsplit(value)
        port = parsed.port
    except (UnicodeEncodeError, ValueError):
        raise ValueError(
            "configured peer URL must be a bounded WebSocket URL"
        ) from None
    host = parsed.hostname
    if (
        parsed.scheme not in {"ws", "wss"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65_535)
        or not host.isascii()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or any(character.isspace() or ord(character) < 33 for character in host)
    ):
        raise ValueError("configured peer URL must be a bounded WebSocket URL")
    return value


def _normalize_cometnet_pool_id(value: object) -> str:
    if (
        type(value) is not str
        or value != value.strip().lower()
        or not 2 <= len(value) <= 64
        or not value.isascii()
        or not value.replace("-", "").replace("_", "").isalnum()
    ):
        raise ValueError("CometNet pool IDs must use canonical bounded text")
    return value


def _bounded_credential(value: object) -> str:
    if type(value) is not str or not value:
        raise ValueError("configured credential must be non-empty text")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("configured credential must be valid UTF-8") from None
    if size > 4_096 or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ValueError("configured credential must be bounded text")
    return value


def _is_bounded_opaque_credential(value: object, maximum_bytes: int) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return len(encoded) <= maximum_bytes and not any(
        character.isspace() or ord(character) < 33 or ord(character) == 127
        for character in value
    )


def _bounded_display_text(
    value: object,
    *,
    field: str,
    maximum_bytes: int,
) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{field} must be bounded display text")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError(f"{field} must be valid UTF-8") from None
    if size > maximum_bytes or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ValueError(f"{field} must be bounded display text")
    return value


def _normalize_optional_text(value: object) -> object:
    if value is None:
        return None
    if value == "":
        return None
    return value


def _normalize_proxy_url(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError("proxy URL must use a supported HTTP or SOCKS scheme")
    try:
        if len(value.encode("utf-8")) > 4_096:
            raise ValueError
        parsed = urlsplit(value)
        port = parsed.port
        for credential in (parsed.username, parsed.password):
            if credential is None:
                continue
            if re.search(r"%(?![0-9A-Fa-f]{2})", credential):
                raise ValueError
            _bounded_credential(unquote_to_bytes(credential).decode("utf-8"))
    except (UnicodeEncodeError, ValueError):
        raise ValueError(
            "proxy URL must use a supported HTTP or SOCKS scheme"
        ) from None
    host = parsed.hostname
    if (
        parsed.scheme not in SUPPORTED_PROXY_SCHEMES
        or not host
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65_535)
        or not host.isascii()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or any(character.isspace() or ord(character) < 33 for character in host)
    ):
        raise ValueError("proxy URL must use a supported HTTP or SOCKS scheme")
    return value.rstrip("/")


def _normalize_scraper_url(value: object) -> str:
    if type(value) is not str or value != value.strip():
        raise ValueError("scraper URL must be a bounded HTTP(S) URL")
    base_url, mode = parse_url_scrape_mode(value)
    normalized = _normalize_http_url(base_url)
    return normalized if mode == "both" else f"{normalized}:{mode}"


_SCRAPER_MODE_FIELDS = (
    "INDEXER_MANAGER_MODE",
    "SCRAPE_JACKETT",
    "SCRAPE_PROWLARR",
    "SCRAPE_COMET",
    "SCRAPE_NYAA",
    "SCRAPE_ANIMETOSHO",
    "SCRAPE_SEADEX",
    "SCRAPE_NEKOBT",
    "SCRAPE_ZILEAN",
    "SCRAPE_STREMTHRU",
    "SCRAPE_DMM",
    "SCRAPE_BITMAGNET",
    "SCRAPE_TORRENTIO",
    "SCRAPE_MEDIAFUSION",
    "SCRAPE_AIOSTREAMS",
    "SCRAPE_JACKETTIO",
    "SCRAPE_DEBRIDIO",
    "SCRAPE_TORRENTSDB",
    "SCRAPE_PEERFLIX",
)
_POSITIVE_WORK_COUNT_FIELDS = (
    "EXECUTOR_MAX_WORKERS",
    "DATABASE_BATCH_SIZE",
    "NYAA_MAX_CONCURRENT_PAGES",
    "ANIMETOSHO_MAX_CONCURRENT_PAGES",
    "DMM_INGEST_CONCURRENT_WORKERS",
    "DMM_INGEST_BATCH_SIZE",
    "BITMAGNET_MAX_CONCURRENT_PAGES",
    "BACKGROUND_SCRAPER_CONCURRENT_WORKERS",
    "FILTER_PARSE_CACHE_SHARDS",
)
_SCRAPE_TIMEOUT_FIELDS = (
    "LIVE_SCRAPE_TIMEOUT",
    "BACKGROUND_SCRAPE_TIMEOUT",
)
_NONNEGATIVE_HTTP_OPERATION_FIELDS = (
    "MEMORY_TRIM_INTERVAL",
    "RATELIMIT_MAX_RETRIES",
    "HTTP_CLIENT_TTL_DNS_CACHE",
    "HTTP_CACHE_STREAMS_TTL",
    "HTTP_CACHE_STALE_WHILE_REVALIDATE",
    "HTTP_CACHE_MANIFEST_TTL",
    "HTTP_CACHE_CONFIGURE_TTL",
)
_POSITIVE_HTTP_OPERATION_FIELDS = (
    "RATELIMIT_RETRY_BASE_DELAY",
    "HTTP_CLIENT_LIMIT",
    "HTTP_CLIENT_LIMIT_PER_HOST",
    "HTTP_CLIENT_KEEPALIVE_TIMEOUT",
    "HTTP_CLIENT_TIMEOUT_TOTAL",
)
_GENERAL_INTEGER_OPERATION_BOUNDS = {
    "DATABASE_STARTUP_CLEANUP_INTERVAL": (-1, 31_536_000),
    "METADATA_CACHE_TTL": (0, 315_360_000),
    "TORRENT_CACHE_TTL": (-1, 315_360_000),
    "LIVE_TORRENT_CACHE_TTL": (-1, 315_360_000),
    "DEBRID_CACHE_TTL": (0, 315_360_000),
    "METRICS_CACHE_TTL": (0, 31_536_000),
    "SCRAPE_LOCK_TTL": (1, 86_400),
    "INDEXER_MANAGER_TIMEOUT": (1, 3_600),
    "INDEXER_MANAGER_UPDATE_INTERVAL": (1, 31_536_000),
    "INDEXER_MANAGER_WAIT_TIMEOUT": (1, 3_600),
    "GET_TORRENT_TIMEOUT": (1, 3_600),
    "MAGNET_RESOLVE_TIMEOUT": (1, 3_600),
    "CATALOG_TIMEOUT": (1, 3_600),
    "DMM_INGEST_INTERVAL": (1, 31_536_000),
    "BITMAGNET_MAX_OFFSET": (0, 1_000_000),
    "PROXY_DEBRID_STREAM_INACTIVITY_THRESHOLD": (0, 31_536_000),
    "DEBRID_ACCOUNT_SCRAPE_REFRESH_INTERVAL": (1, 31_536_000),
    "DEBRID_ACCOUNT_SCRAPE_CACHE_TTL": (1, 315_360_000),
    "DEBRID_ACCOUNT_SCRAPE_MAX_SNAPSHOT_ITEMS": (1, 100_000),
    "DEBRID_ACCOUNT_SCRAPE_MAX_MATCH_ITEMS": (1, 100_000),
    "ANIME_MAPPING_REFRESH_INTERVAL": (0, 31_536_000),
    "FILTER_PARSE_CACHE_SIZE": (0, 1_000_000),
}
_GENERAL_OPERATION_MAXIMA = {
    "EXECUTOR_MAX_WORKERS": 256,
    "NYAA_MAX_CONCURRENT_PAGES": 64,
    "ANIMETOSHO_MAX_CONCURRENT_PAGES": 64,
    "DMM_INGEST_CONCURRENT_WORKERS": 64,
    "DMM_INGEST_BATCH_SIZE": 10_000,
    "BITMAGNET_MAX_CONCURRENT_PAGES": 64,
    "BACKGROUND_SCRAPER_CONCURRENT_WORKERS": 64,
    "FILTER_PARSE_CACHE_SHARDS": 64,
    "LIVE_SCRAPE_TIMEOUT": 3_600,
    "BACKGROUND_SCRAPE_TIMEOUT": 3_600,
    "MEMORY_TRIM_INTERVAL": 31_536_000,
    "RATELIMIT_RETRY_BASE_DELAY": 3_600,
    "HTTP_CLIENT_LIMIT": 100_000,
    "HTTP_CLIENT_LIMIT_PER_HOST": 100_000,
    "HTTP_CLIENT_TTL_DNS_CACHE": 31_536_000,
    "HTTP_CLIENT_KEEPALIVE_TIMEOUT": 3_600,
    "HTTP_CLIENT_TIMEOUT_TOTAL": 3_600,
    "HTTP_CACHE_STREAMS_TTL": 315_360_000,
    "HTTP_CACHE_STALE_WHILE_REVALIDATE": 315_360_000,
    "HTTP_CACHE_MANIFEST_TTL": 315_360_000,
    "HTTP_CACHE_CONFIGURE_TTL": 315_360_000,
}
_BACKGROUND_INTEGER_OPERATION_BOUNDS = {
    "BACKGROUND_SCRAPER_INTERVAL": (1, 31_536_000),
    "BACKGROUND_SCRAPER_MAX_MOVIES_PER_RUN": (0, 10_000),
    "BACKGROUND_SCRAPER_MAX_SERIES_PER_RUN": (0, 10_000),
    "BACKGROUND_SCRAPER_SUCCESS_TTL": (0, 315_360_000),
    "BACKGROUND_SCRAPER_FAILURE_BASE_BACKOFF": (1, 31_536_000),
    "BACKGROUND_SCRAPER_FAILURE_MAX_BACKOFF": (1, 315_360_000),
    "BACKGROUND_SCRAPER_MAX_RETRIES": (-1, 100),
    "BACKGROUND_SCRAPER_RUN_TIME_BUDGET": (0, 604_800),
    "BACKGROUND_SCRAPER_DISCOVERY_MULTIPLIER": (1, 100),
    "BACKGROUND_SCRAPER_MAX_EPISODES_PER_SERIES_PER_RUN": (0, 10_000),
    "BACKGROUND_SCRAPER_EPISODE_REFRESH_TTL": (0, 315_360_000),
    "BACKGROUND_SCRAPER_DEMAND_LOOKBACK": (0, 315_360_000),
    "BACKGROUND_SCRAPER_DEFER_COOLDOWN": (0, 31_536_000),
    "BACKGROUND_SCRAPER_QUEUE_LOW_WATERMARK": (0, 10_000_000),
    "BACKGROUND_SCRAPER_QUEUE_HIGH_WATERMARK": (0, 10_000_000),
    "BACKGROUND_SCRAPER_QUEUE_HARD_CAP": (0, 10_000_000),
    "BACKGROUND_SCRAPER_ALERT_QUEUE_AGE": (0, 315_360_000),
    "BACKGROUND_SCRAPER_RUN_RETENTION_DAYS": (0, 3_650),
}
_BACKGROUND_FLOAT_OPERATION_BOUNDS = {
    "BACKGROUND_SCRAPER_MIN_PRIORITY_SCORE": (0.0, 1_000_000.0),
    "BACKGROUND_SCRAPER_PRIORITY_DECAY_ON_MISS": (0.0, 1.0),
    "BACKGROUND_SCRAPER_ALERT_FAIL_RATE": (0.0, 1.0),
}
_COMETNET_INTEGER_OPERATION_BOUNDS = {
    "COMETNET_LISTEN_PORT": (1, 65_535),
    "COMETNET_HTTP_PORT": (1, 65_535),
    "COMETNET_MAX_PEERS": (1, 1_000),
    "COMETNET_MIN_PEERS": (1, 1_000),
    "COMETNET_TIME_CHECK_TOLERANCE": (0, 86_400),
    "COMETNET_TIME_CHECK_TIMEOUT": (1, 300),
    "COMETNET_REACHABILITY_RETRIES": (1, 100),
    "COMETNET_REACHABILITY_RETRY_DELAY": (0, 3_600),
    "COMETNET_REACHABILITY_TIMEOUT": (1, 300),
    "COMETNET_UPNP_LEASE_DURATION": (1, 604_800),
    "COMETNET_STATE_SAVE_INTERVAL": (1, 86_400),
    "COMETNET_GOSSIP_FANOUT": (1, 1_000),
    "COMETNET_GOSSIP_MESSAGE_TTL": (1, 64),
    "COMETNET_GOSSIP_MAX_TORRENTS_PER_MESSAGE": (1, 10_000),
    "COMETNET_GOSSIP_VALIDATION_FUTURE_TOLERANCE": (0, 86_400),
    "COMETNET_GOSSIP_VALIDATION_PAST_TOLERANCE": (0, 604_800),
    "COMETNET_GOSSIP_TORRENT_MAX_AGE": (1, 315_360_000),
    "COMETNET_PEX_BATCH_SIZE": (1, 1_000),
    "COMETNET_PEER_CONNECT_BACKOFF_MAX": (1, 86_400),
    "COMETNET_PEER_MAX_FAILURES": (1, 100),
    "COMETNET_PEER_CLEANUP_AGE": (1, 315_360_000),
    "COMETNET_TRANSPORT_MAX_MESSAGE_SIZE": (1, 67_108_864),
    "COMETNET_TRANSPORT_MAX_CONNECTIONS_PER_IP": (1, 1_000),
    "COMETNET_TRANSPORT_RATE_LIMIT_COUNT": (1, 1_000_000),
}
_COMETNET_FLOAT_OPERATION_BOUNDS = {
    "COMETNET_GOSSIP_INTERVAL": (0.001, 3_600.0),
    "COMETNET_TRANSPORT_PING_INTERVAL": (0.001, 86_400.0),
    "COMETNET_TRANSPORT_CONNECTION_TIMEOUT": (0.001, 3_600.0),
    "COMETNET_TRANSPORT_MAX_LATENCY_MS": (0.001, 3_600_000.0),
    "COMETNET_TRANSPORT_RATE_LIMIT_WINDOW": (0.001, 3_600.0),
}
_COMETNET_REPUTATION_FIELDS = (
    "COMETNET_REPUTATION_INITIAL",
    "COMETNET_REPUTATION_MIN",
    "COMETNET_REPUTATION_MAX",
    "COMETNET_REPUTATION_THRESHOLD_UNTRUSTED",
    "COMETNET_REPUTATION_THRESHOLD_TRUSTED",
    "COMETNET_REPUTATION_BONUS_VALID_CONTRIBUTION",
    "COMETNET_REPUTATION_BONUS_PER_DAY_ANCIENNETY",
    "COMETNET_REPUTATION_BONUS_MAX_ANCIENNETY",
    "COMETNET_REPUTATION_PENALTY_INVALID_CONTRIBUTION",
    "COMETNET_REPUTATION_PENALTY_INVALID_SIGNATURE",
)


def set_comet_foreign_keys_enabled(enabled: bool) -> None:
    global _comet_fk_enabled
    _comet_fk_enabled = enabled


async def apply_sqlite_connection_pragmas(
    execute: Callable[[str], Awaitable[object]],
    *,
    foreign_keys_enabled: bool,
) -> None:
    await execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
    await execute(f"PRAGMA foreign_keys={'ON' if foreign_keys_enabled else 'OFF'}")


if not getattr(SQLiteConnection, "_comet_pragmas_patched", False):
    _original_sqlite_acquire = SQLiteConnection.acquire

    async def _comet_sqlite_acquire(self):
        await _original_sqlite_acquire(self)
        assert self._connection is not None
        await apply_sqlite_connection_pragmas(
            self._connection.execute,
            foreign_keys_enabled=_comet_fk_enabled,
        )

    SQLiteConnection.acquire = _comet_sqlite_acquire
    SQLiteConnection._comet_pragmas_patched = True


class AppSettings(ServerSettings):
    model_config = SettingsConfigDict(**ServerSettings.model_config, frozen=True)

    _public_api_token_source: str = PrivateAttr(default="disabled")
    _comet_capability_secret_source: str = PrivateAttr(default="disabled")
    _stremio_api_prefix: str = PrivateAttr(default="")
    _applied_settings_revision: int = PrivateAttr(default=0)

    ADDON_ID: str = "stremio.comet.fast"
    ADDON_NAME: str = "Comet"
    COMET_COMMIT_HASH: str | None = None
    COMET_BUILD_DATE: str | None = None
    COMET_BRANCH: str = "main"
    EXECUTOR_MAX_WORKERS: int = 1
    RUNTIME_INSTANCE_ALIAS: str | None = None
    RUNTIME_HEARTBEAT_INTERVAL: float = 10.0
    RUNTIME_STALE_SECONDS: int = 60
    ADMIN_DASHBOARD_PASSWORD: str | None = Field(default_factory=_generate_secret)
    admin_dashboard_password_source: str = Field(
        default="generated_memory",
        exclude=True,
        repr=False,
    )
    ADMIN_DASHBOARD_SESSION_TTL: int = 86400
    CONFIGURE_PAGE_PASSWORD: str | None = None
    CONFIGURE_PAGE_SESSION_TTL: int = 86400
    PUBLIC_API_TOKEN: str | None = None
    PUBLIC_API_TOKEN_FILE: str | None = "data/public_api_token.txt"
    PROMETHEUS_ENABLED: bool = False
    PROMETHEUS_PATH: str = "/metrics"
    PROMETHEUS_AUTH_TOKEN: str | None = None
    PROMETHEUS_AUTH_TOKEN_FILE: str | None = None
    PROMETHEUS_MULTIPROC_DIR: str = "/tmp/comet-prometheus"
    PROMETHEUS_QUERY_URL: str | None = None
    PROMETHEUS_QUERY_TOKEN: str | None = None
    DATABASE_TYPE: str = "sqlite"
    DATABASE_URL: str | None = None
    DATABASE_PATH: str | None = "data/comet.db"
    DATABASE_BATCH_SIZE: int = 20000
    DATABASE_READ_REPLICA_URLS: list[str] = Field(default_factory=list)
    DATABASE_STARTUP_CLEANUP_INTERVAL: int = 3600
    MEMORY_TRIM_INTERVAL: int = 300
    DATABASE_FORCE_IPV4_RESOLUTION: bool = False
    METADATA_CACHE_TTL: int = 2592000  # 30 days
    TORRENT_CACHE_TTL: int = 2592000  # 30 days
    LIVE_TORRENT_CACHE_TTL: int = 604800  # 7 days
    DEBRID_CACHE_TTL: int = 86400  # 1 day
    METRICS_CACHE_TTL: int = 60  # 1 minute
    DEBRID_CACHE_CHECK_RATIO: float = 0.0  # 0.0 to 1.0
    SCRAPE_LOCK_TTL: int = 300  # 5 minutes
    LIVE_SCRAPE_TIMEOUT: float = 30.0
    BACKGROUND_SCRAPE_TIMEOUT: float = 30.0
    SCRAPER_TIMEOUT_OVERRIDES: dict[str, float] = Field(default_factory=dict)
    INDEXER_MANAGER_TYPE: str | None = None
    INDEXER_MANAGER_URL: str | None = "http://127.0.0.1:9117"
    INDEXER_MANAGER_API_KEY: str | None = None
    INDEXER_MANAGER_MODE: bool | str = "both"
    INDEXER_MANAGER_TIMEOUT: int = 30
    INDEXER_MANAGER_INDEXERS: list[str] = Field(default_factory=list)
    INDEXER_MANAGER_UPDATE_INTERVAL: int = 900
    INDEXER_MANAGER_WAIT_TIMEOUT: int = 30
    INDEXER_INCLUDE_CANONICAL_TITLE: bool = True
    INDEXER_INCLUDE_ORIGINAL_TITLE: bool = True
    INDEXER_LANGUAGES: list[str] = Field(default_factory=list)
    SCRAPE_JACKETT: bool | str = False
    JACKETT_URL: str | None = "http://127.0.0.1:9117"
    JACKETT_API_KEY: str | None = None
    JACKETT_INDEXERS: list[str] = Field(default_factory=list)
    SCRAPE_PROWLARR: bool | str = False
    PROWLARR_URL: str | None = "http://127.0.0.1:9696"
    PROWLARR_API_KEY: str | None = None
    PROWLARR_INDEXERS: list[str] = Field(default_factory=list)
    GET_TORRENT_TIMEOUT: int = 5
    MAGNET_RESOLVE_TIMEOUT: int = 60
    CATALOG_TIMEOUT: int = 30
    DOWNLOAD_TORRENT_FILES: bool = False
    SCRAPE_COMET: bool | str = False
    COMET_URL: str | list[str] = "https://comet.feels.legal"
    COMET_CLEAN_TRACKER: bool = False
    SCRAPE_NYAA: bool | str = False
    NYAA_ANIME_ONLY: bool = True
    NYAA_MAX_CONCURRENT_PAGES: int = 5
    SCRAPE_ANIMETOSHO: bool | str = False
    SCRAPE_ANIMETOSHO_USENET: bool = False
    ANIMETOSHO_ANIME_ONLY: bool = True
    ANIMETOSHO_MAX_CONCURRENT_PAGES: int = 10
    SCRAPE_SEADEX: bool | str = False
    SEADEX_ANIME_ONLY: bool = True
    SCRAPE_NEKOBT: bool | str = False
    NEKOBT_ANIME_ONLY: bool = True
    SCRAPE_ZILEAN: bool | str = False
    ZILEAN_URL: str | list[str] = "https://zileanfortheweebs.midnightignite.me"
    SCRAPE_STREMTHRU: bool | str = False
    STREMTHRU_SCRAPE_URL: str | list[str] = "https://stremthru.13377001.xyz"
    SCRAPE_DMM: bool | str = False
    DMM_INGEST_ENABLED: bool = False
    DMM_INGEST_INTERVAL: int = 86400
    DMM_INGEST_CONCURRENT_WORKERS: int = 4
    DMM_INGEST_BATCH_SIZE: int = 100
    SCRAPE_BITMAGNET: bool | str = False
    BITMAGNET_URL: str | list[str] = "https://bitmagnetfortheweebs.midnightignite.me"
    BITMAGNET_MAX_CONCURRENT_PAGES: int = 5
    BITMAGNET_MAX_OFFSET: int = 15000
    SCRAPE_TORRENTIO: bool | str = False
    TORRENTIO_URL: str | list[str] = "https://torrentio.strem.fun"
    SCRAPE_MEDIAFUSION: bool | str = False
    MEDIAFUSION_URL: str | list[str] = "https://mediafusion.elfhosted.com"
    MEDIAFUSION_API_PASSWORD: str | list[str] | None = None
    MEDIAFUSION_LIVE_SEARCH: bool = True
    SCRAPE_AIOSTREAMS: bool | str = False
    AIOSTREAMS_URL: str | list[str] | None = None
    AIOSTREAMS_USER_UUID_AND_PASSWORD: str | list[str] | None = None
    SCRAPE_JACKETTIO: bool | str = False
    JACKETTIO_URL: str | list[str] | None = None
    SCRAPE_DEBRIDIO: bool | str = False
    DEBRIDIO_API_KEY: str | None = None
    DEBRIDIO_PROVIDER: str | None = None
    DEBRIDIO_PROVIDER_KEY: str | None = None
    SCRAPE_TORRENTSDB: bool | str = False
    SCRAPE_PEERFLIX: bool | str = False
    CUSTOM_HEADER_HTML: str | None = None
    PROXY_DEBRID_STREAM: bool = False
    PROXY_DEBRID_STREAM_PASSWORD: str | None = Field(default_factory=_generate_secret)
    proxy_debrid_stream_password_source: str = Field(
        default="generated_memory",
        exclude=True,
        repr=False,
    )
    PROXY_DEBRID_STREAM_MAX_CONNECTIONS: int = -1
    PROXY_DEBRID_STREAM_DEBRID_DEFAULT_SERVICE: str = "realdebrid"
    PROXY_DEBRID_STREAM_DEBRID_DEFAULT_APIKEY: str | None = None
    PROXY_DEBRID_STREAM_INACTIVITY_THRESHOLD: int = 300
    DEBRID_ACCOUNT_SCRAPE_REFRESH_INTERVAL: int = 900
    DEBRID_ACCOUNT_SCRAPE_CACHE_TTL: int = 86400
    DEBRID_ACCOUNT_SCRAPE_MAX_SNAPSHOT_ITEMS: int = 5000
    DEBRID_ACCOUNT_SCRAPE_MAX_MATCH_ITEMS: int = 1500
    DEBRID_ACCOUNT_SCRAPE_INITIAL_WARM_TIMEOUT: float = 5.0
    STREMTHRU_URL: str | None = "https://stremthru.13377001.xyz"
    DISABLE_TORRENT_STREAMS: bool = False
    TORRENT_DISABLED_STREAM_NAME: str | None = "[INFO] Comet"
    TORRENT_DISABLED_STREAM_DESCRIPTION: str | None = (
        "Direct torrent playback is disabled on this server."
    )
    TORRENT_DISABLED_STREAM_URL: str | None = "https://comet.feels.legal"
    PUBLIC_BASE_URL: str | None = None
    # Usenet is deliberately opt-in.  These values stay in the normal deployment
    # settings; user credentials continue to live only in the addon URL.
    USENET_ENABLED: bool = False
    USENET_ENGINE_ENABLED: bool = False
    USENET_ENGINE_REQUIRED: bool = False
    USENET_NATIVE_ACCESS_TOKEN: str | None = None
    usenet_native_access_token_source: str = Field(
        default="disabled",
        exclude=True,
        repr=False,
    )
    USENET_NATIVE_SERVERS: list[dict] = Field(default_factory=list)
    USENET_NATIVE_ALLOW_USER_SERVERS: bool = False
    USENET_PRIVATE_UPSTREAM_ORIGINS: list[str] = Field(default_factory=list)
    USENET_EXPORT_BASE_URL: str | None = None
    COMET_CAPABILITY_SECRET: str | None = None
    COMET_CAPABILITY_SECRET_FILE: str | None = "data/comet_capability_secret.txt"
    USENET_REPLICA_COUNT: int = 1
    COMET_USENET_ENGINE_BINARY: str = "/app/native/usenet-engine"
    USENET_RUNTIME_DIR: str = "/run/comet/usenet"
    USENET_LOCAL_DATA_DIR: str = "data/usenet-local"
    USENET_ARTIFACT_DIR: str = "data/nzb-artifacts"
    USENET_NATIVE_MAX_STREAMS: int = 32
    USENET_MEMORY_CACHE_BYTES: int = 268435456
    USENET_DISK_CACHE_BYTES: int = 2147483648
    USENET_SPOOL_MAX_BYTES: int = 107374182400
    USENET_MIN_FREE_DISK_BYTES: int = 5368709120
    USENET_ARCHIVE_JOBS: int = 2
    USENET_REPAIR_JOBS: int = 1
    USENET_DEGRADED_PLAYBACK_ENABLED: bool = True
    USENET_START_TIMEOUT_SECONDS: int = 30
    USENET_DRAIN_TIMEOUT_SECONDS: int = 30
    REMOVE_ADULT_CONTENT: bool = False
    BACKGROUND_SCRAPER_ENABLED: bool = False
    BACKGROUND_SCRAPER_CONCURRENT_WORKERS: int = 1
    BACKGROUND_SCRAPER_INTERVAL: int = 3600
    BACKGROUND_SCRAPER_MAX_MOVIES_PER_RUN: int = 100
    BACKGROUND_SCRAPER_MAX_SERIES_PER_RUN: int = 100
    BACKGROUND_SCRAPER_SUCCESS_TTL: int = 604800
    BACKGROUND_SCRAPER_FAILURE_BASE_BACKOFF: int = 3600
    BACKGROUND_SCRAPER_FAILURE_MAX_BACKOFF: int = 604800
    BACKGROUND_SCRAPER_MAX_RETRIES: int = 6
    BACKGROUND_SCRAPER_RUN_TIME_BUDGET: int = 1800
    BACKGROUND_SCRAPER_DISCOVERY_MULTIPLIER: int = 3
    BACKGROUND_SCRAPER_MAX_EPISODES_PER_SERIES_PER_RUN: int = 25
    BACKGROUND_SCRAPER_EPISODE_REFRESH_TTL: int = 21600
    BACKGROUND_SCRAPER_ENABLE_DEMAND_PRIORITY: bool = True
    BACKGROUND_SCRAPER_DEMAND_LOOKBACK: int = 86400
    BACKGROUND_SCRAPER_DEFER_COOLDOWN: int = 300
    BACKGROUND_SCRAPER_MIN_PRIORITY_SCORE: float = 0.0
    BACKGROUND_SCRAPER_PRIORITY_DECAY_ON_MISS: float = 0.9
    BACKGROUND_SCRAPER_QUEUE_LOW_WATERMARK: int = 10000
    BACKGROUND_SCRAPER_QUEUE_HIGH_WATERMARK: int = 20000
    BACKGROUND_SCRAPER_QUEUE_HARD_CAP: int = 30000
    BACKGROUND_SCRAPER_ALERT_FAIL_RATE: float = 0.7
    BACKGROUND_SCRAPER_ALERT_QUEUE_AGE: int = 86400
    BACKGROUND_SCRAPER_RUN_RETENTION_DAYS: int = 30
    ANIME_MAPPING_ENABLED: bool = True
    ANIME_MAPPING_REFRESH_INTERVAL: int = 432000
    DIGITAL_RELEASE_FILTER: bool = False
    TMDB_READ_ACCESS_TOKEN: str | None = None
    GLOBAL_PROXY_URL: str | None = None
    USER_PROVIDED_PROXY_URL: str | None = None
    PROXY_ETHOS: str = "always"
    AIOSTREAMS_PROXY_URL: str | None = None
    ANIMETOSHO_PROXY_URL: str | None = None
    BITMAGNET_PROXY_URL: str | None = None
    COMET_PROXY_URL: str | None = None
    DEBRIDIO_PROXY_URL: str | None = None
    DMM_PROXY_URL: str | None = None
    JACKETT_PROXY_URL: str | None = None
    JACKETTIO_PROXY_URL: str | None = None
    MEDIAFUSION_PROXY_URL: str | None = None
    NEKOBT_PROXY_URL: str | None = None
    NYAA_PROXY_URL: str | None = None
    PEERFLIX_PROXY_URL: str | None = None
    PROWLARR_PROXY_URL: str | None = None
    SEADEX_PROXY_URL: str | None = None
    STREMTHRU_PROXY_URL: str | None = None
    TORRENTIO_PROXY_URL: str | None = None
    TORRENTSDB_PROXY_URL: str | None = None
    ZILEAN_PROXY_URL: str | None = None
    RATELIMIT_MAX_RETRIES: int = 3
    RATELIMIT_RETRY_BASE_DELAY: float = 1.0
    FILTER_PARSE_CACHE_SIZE: int = 10000
    FILTER_PARSE_CACHE_SHARDS: int = 8
    FILTER_PARSE_CACHE_DEDUP_INFLIGHT: bool = True
    HTTP_CACHE_ENABLED: bool = False
    HTTP_CLIENT_LIMIT: int = 100
    HTTP_CLIENT_LIMIT_PER_HOST: int = 20
    HTTP_CLIENT_TTL_DNS_CACHE: int = 300
    HTTP_CLIENT_KEEPALIVE_TIMEOUT: float = 30.0
    HTTP_CLIENT_TIMEOUT_TOTAL: float = 30.0
    HTTP_CACHE_STREAMS_TTL: int = 300
    HTTP_CACHE_STALE_WHILE_REVALIDATE: int = 60
    HTTP_CACHE_MANIFEST_TTL: int = 86400
    HTTP_CACHE_CONFIGURE_TTL: int = 86400
    DOWNLOAD_GENERIC_TRACKERS: bool = False
    SMART_LANGUAGE_DETECTION: bool = False
    RTN_FILTER_DEBUG: bool = False

    # CometNet P2P Network Configuration
    COMETNET_ENABLED: bool = False
    COMETNET_LISTEN_PORT: int = 8765
    COMETNET_HTTP_PORT: int = 8766
    COMETNET_BOOTSTRAP_NODES: list[str] = Field(default_factory=list)
    COMETNET_MANUAL_PEERS: list[str] = Field(default_factory=list)
    COMETNET_MAX_PEERS: int = 50
    COMETNET_MIN_PEERS: int = 3
    COMETNET_KEYS_DIR: str | None = "data/cometnet"
    COMETNET_ADVERTISE_URL: str | None = None
    COMETNET_KEY_PASSWORD: str | None = None
    COMETNET_ALLOW_PRIVATE_PEX: bool = False
    COMETNET_SKIP_REACHABILITY_CHECK: bool = False
    COMETNET_SKIP_TIME_CHECK: bool = False
    COMETNET_TIME_CHECK_TOLERANCE: int = 60
    COMETNET_TIME_CHECK_TIMEOUT: int = 5
    COMETNET_REACHABILITY_RETRIES: int = 5
    COMETNET_REACHABILITY_RETRY_DELAY: int = 10
    COMETNET_REACHABILITY_TIMEOUT: int = 10
    COMETNET_UPNP_ENABLED: bool = False
    COMETNET_UPNP_LEASE_DURATION: int = 3600
    COMETNET_RELAY_URL: str | None = None
    COMETNET_API_KEY: str | None = Field(default_factory=_generate_secret)
    cometnet_api_key_source: str = Field(
        default="generated_memory",
        exclude=True,
        repr=False,
    )
    COMETNET_STATE_SAVE_INTERVAL: int = (
        300  # Periodic state save interval in seconds (5 minutes)
    )

    # CometNet Gossip Tuning
    COMETNET_GOSSIP_FANOUT: int = 3
    COMETNET_GOSSIP_INTERVAL: float = 1.0
    COMETNET_GOSSIP_MESSAGE_TTL: int = 5
    COMETNET_GOSSIP_MAX_TORRENTS_PER_MESSAGE: int = 1000

    COMETNET_GOSSIP_VALIDATION_FUTURE_TOLERANCE: int = 60
    COMETNET_GOSSIP_VALIDATION_PAST_TOLERANCE: int = 300
    COMETNET_GOSSIP_TORRENT_MAX_AGE: int = 604800

    # CometNet Discovery Tuning
    COMETNET_PEX_BATCH_SIZE: int = 20
    COMETNET_PEER_CONNECT_BACKOFF_MAX: int = 300
    COMETNET_PEER_MAX_FAILURES: int = 5
    COMETNET_PEER_CLEANUP_AGE: int = 604800

    # CometNet Transport Tuning
    COMETNET_TRANSPORT_MAX_MESSAGE_SIZE: int = 10485760  # 10MB
    COMETNET_TRANSPORT_MAX_CONNECTIONS_PER_IP: int = 3
    COMETNET_TRANSPORT_PING_INTERVAL: float = 30.0
    COMETNET_TRANSPORT_CONNECTION_TIMEOUT: float = 120.0
    COMETNET_TRANSPORT_MAX_LATENCY_MS: float = (
        10000.0  # Max acceptable latency before disconnection
    )
    COMETNET_TRANSPORT_WEBSOCKET_COMPRESSION_ENABLED: bool = False
    COMETNET_TRANSPORT_RATE_LIMIT_ENABLED: bool = True
    COMETNET_TRANSPORT_RATE_LIMIT_COUNT: int = 20  # Messages per window
    COMETNET_TRANSPORT_RATE_LIMIT_WINDOW: float = 1.0  # Seconds

    # CometNet Reputation Tuning
    COMETNET_REPUTATION_INITIAL: float = 100.0
    COMETNET_REPUTATION_MIN: float = 0.0
    COMETNET_REPUTATION_MAX: float = 10000.0
    COMETNET_REPUTATION_THRESHOLD_UNTRUSTED: float = 50.0  # Ban threshold
    COMETNET_REPUTATION_THRESHOLD_TRUSTED: float = (
        1000.0  # Trust threshold (approx 1 day of heavy scraping)
    )
    COMETNET_REPUTATION_BONUS_VALID_CONTRIBUTION: float = (
        0.001  # 1M torrents = 1000 pts
    )
    COMETNET_REPUTATION_BONUS_PER_DAY_ANCIENNETY: float = 10.0
    COMETNET_REPUTATION_BONUS_MAX_ANCIENNETY: float = (
        1000.0  # Max 1000 pts from age (100 days)
    )
    COMETNET_REPUTATION_PENALTY_INVALID_CONTRIBUTION: float = 50.0
    COMETNET_REPUTATION_PENALTY_INVALID_SIGNATURE: float = 500.0

    # CometNet Contribution Mode
    # full: Share own torrents + receive + repropagate (default)
    # consumer: Receive + repropagate, but don't share own torrents
    # source: Share own torrents only (dedicated scraper)
    # leech: Receive only, don't repropagate (save bandwidth)
    COMETNET_CONTRIBUTION_MODE: str = "full"

    # CometNet Trust Pools
    # List of pool IDs to subscribe to
    # If empty: accept from everyone (open mode)
    COMETNET_TRUSTED_POOLS: list[str] = Field(default_factory=list)
    # Directory for storing pool manifests and membership data
    COMETNET_POOLS_DIR: str | None = "data/cometnet/pools"

    # CometNet Private Network
    # Isolate this node in a private network
    COMETNET_PRIVATE_NETWORK: bool = False
    # Network ID for private network (required if PRIVATE_NETWORK=true)
    COMETNET_NETWORK_ID: str | None = None
    # Password to join the private network (Argon2 hashed for auth)
    COMETNET_NETWORK_PASSWORD: str | None = None
    # CometNet Node Alias
    # Optional friendly name for this node (exchanged with other peers)
    # If not set, users will only see the Node ID
    COMETNET_NODE_ALIAS: str | None = None

    @model_validator(mode="before")
    @classmethod
    def resolve_generated_memory_secrets(cls, values):
        if not isinstance(values, dict):
            return values
        values = dict(values)
        shared_generated = effective_settings_payload().generated_keys
        for field, source_field in (
            (
                "ADMIN_DASHBOARD_PASSWORD",
                "admin_dashboard_password_source",
            ),
            (
                "PROXY_DEBRID_STREAM_PASSWORD",
                "proxy_debrid_stream_password_source",
            ),
            ("COMETNET_API_KEY", "cometnet_api_key_source"),
        ):
            if field not in values:
                values[source_field] = "generated_memory"
                continue
            raw_value = values[field]
            if raw_value is None or raw_value == "":
                values[field] = _generate_secret()
                values[source_field] = "generated_memory"
            else:
                values[source_field] = (
                    "generated_shared" if field in shared_generated else "configured"
                )
        return values

    @field_validator(*_SCRAPER_MODE_FIELDS, mode="before")
    def validate_scraper_mode(cls, value):
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized == "false":
                return False
            if normalized in {"true", "both"}:
                return True
            if normalized in {"live", "background"}:
                return normalized
        raise ValueError("scraper mode must be false, true, both, live, or background")

    @field_validator(*_POSITIVE_WORK_COUNT_FIELDS)
    def validate_positive_work_count(cls, value):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("work count must be a positive integer")
        return value

    @field_validator("RUNTIME_INSTANCE_ALIAS")
    @classmethod
    def validate_runtime_instance_alias(cls, value):
        if value is None:
            return None
        if (
            type(value) is not str
            or not value
            or value != value.strip()
            or len(value.encode("utf-8")) > 96
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("RUNTIME_INSTANCE_ALIAS must be bounded display text")
        return value

    @field_validator("RUNTIME_HEARTBEAT_INTERVAL")
    @classmethod
    def validate_runtime_heartbeat_interval(cls, value):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 2 <= value <= 60
        ):
            raise ValueError(
                "RUNTIME_HEARTBEAT_INTERVAL must be between 2 and 60 seconds"
            )
        return float(value)

    @field_validator("RUNTIME_STALE_SECONDS")
    @classmethod
    def validate_runtime_stale_seconds(cls, value):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 15 <= value <= 3600
        ):
            raise ValueError("RUNTIME_STALE_SECONDS must be between 15 and 3600")
        return value

    @field_validator("DATABASE_BATCH_SIZE")
    def validate_database_batch_size(cls, value):
        if value > 100_000:
            raise ValueError("DATABASE_BATCH_SIZE must be at most 100000")
        return value

    @field_validator(*_GENERAL_INTEGER_OPERATION_BOUNDS, mode="before")
    def reject_boolean_general_integer_operations(cls, value):
        if isinstance(value, bool):
            raise ValueError("operational integer values cannot be booleans")
        return value

    @field_validator(*_GENERAL_INTEGER_OPERATION_BOUNDS)
    def validate_general_integer_operation(cls, value, info):
        minimum, maximum = _GENERAL_INTEGER_OPERATION_BOUNDS[info.field_name]
        if not minimum <= value <= maximum:
            raise ValueError(
                f"{info.field_name} must be between {minimum} and {maximum}"
            )
        return value

    @field_validator("PROXY_DEBRID_STREAM_MAX_CONNECTIONS", mode="before")
    def reject_boolean_proxy_connection_limit(cls, value):
        if isinstance(value, bool):
            raise ValueError("proxy connection limit cannot be a boolean")
        return value

    @field_validator("PROXY_DEBRID_STREAM_MAX_CONNECTIONS")
    def validate_proxy_connection_limit(cls, value):
        if value != -1 and not 1 <= value <= 100_000:
            raise ValueError(
                "PROXY_DEBRID_STREAM_MAX_CONNECTIONS must be -1 or between 1 and 100000"
            )
        return value

    @field_validator(
        "DEBRID_CACHE_CHECK_RATIO",
        "DEBRID_ACCOUNT_SCRAPE_INITIAL_WARM_TIMEOUT",
        mode="before",
    )
    def reject_boolean_general_float_operations(cls, value):
        if isinstance(value, bool):
            raise ValueError("operational floating-point values cannot be booleans")
        return value

    @field_validator("DEBRID_CACHE_CHECK_RATIO")
    def validate_debrid_cache_check_ratio(cls, value):
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("DEBRID_CACHE_CHECK_RATIO must be between zero and one")
        return value

    @field_validator("DEBRID_ACCOUNT_SCRAPE_INITIAL_WARM_TIMEOUT")
    def validate_debrid_account_warm_timeout(cls, value):
        if not math.isfinite(value) or not 0 <= value <= 300:
            raise ValueError(
                "DEBRID_ACCOUNT_SCRAPE_INITIAL_WARM_TIMEOUT must be between 0 and 300"
            )
        return value

    @field_validator(
        *_BACKGROUND_INTEGER_OPERATION_BOUNDS,
        *_BACKGROUND_FLOAT_OPERATION_BOUNDS,
        mode="before",
    )
    def reject_boolean_background_operations(cls, value):
        if isinstance(value, bool):
            raise ValueError("background operational values cannot be booleans")
        return value

    @field_validator(*_BACKGROUND_INTEGER_OPERATION_BOUNDS)
    def validate_background_integer_operation(cls, value, info):
        minimum, maximum = _BACKGROUND_INTEGER_OPERATION_BOUNDS[info.field_name]
        if not minimum <= value <= maximum:
            raise ValueError(
                f"{info.field_name} must be between {minimum} and {maximum}"
            )
        return value

    @field_validator(*_BACKGROUND_FLOAT_OPERATION_BOUNDS)
    def validate_background_float_operation(cls, value, info):
        minimum, maximum = _BACKGROUND_FLOAT_OPERATION_BOUNDS[info.field_name]
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError(
                f"{info.field_name} must be finite and between {minimum} and {maximum}"
            )
        return value

    @staticmethod
    def _normalize_scrape_timeout(value) -> float:
        if isinstance(value, bool):
            raise ValueError(
                "scrape timeouts must be finite numbers greater than zero "
                "and at most 3600"
            )
        try:
            normalized = float(value)
        except (TypeError, ValueError):
            raise ValueError(
                "scrape timeouts must be finite numbers greater than zero "
                "and at most 3600"
            ) from None
        if not math.isfinite(normalized) or not 0 < normalized <= 3_600:
            raise ValueError(
                "scrape timeouts must be finite numbers greater than zero "
                "and at most 3600"
            )
        return normalized

    @field_validator(*_SCRAPE_TIMEOUT_FIELDS, mode="before")
    def validate_scrape_timeout(cls, value):
        return cls._normalize_scrape_timeout(value)

    @field_validator("SCRAPER_TIMEOUT_OVERRIDES", mode="before")
    def normalize_scraper_timeout_overrides(cls, value):
        if not isinstance(value, dict):
            raise ValueError("SCRAPER_TIMEOUT_OVERRIDES must be a JSON object")
        if len(value) > 64:
            raise ValueError(
                "SCRAPER_TIMEOUT_OVERRIDES may contain at most 64 selectors"
            )

        normalized = {}
        for selector, timeout in value.items():
            normalized_selector = normalize_scraper_timeout_selector(selector)
            normalized[normalized_selector] = cls._normalize_scrape_timeout(timeout)
        return normalized

    @field_validator(*_POSITIVE_WORK_COUNT_FIELDS, mode="before")
    def reject_boolean_operational_numbers(cls, value):
        if isinstance(value, bool):
            raise ValueError("operational numeric values cannot be booleans")
        return value

    @field_validator(
        *_COMETNET_INTEGER_OPERATION_BOUNDS,
        *_COMETNET_FLOAT_OPERATION_BOUNDS,
        *_COMETNET_REPUTATION_FIELDS,
        mode="before",
    )
    def reject_boolean_cometnet_operations(cls, value):
        if isinstance(value, bool):
            raise ValueError("CometNet operational values cannot be booleans")
        return value

    @field_validator(*_COMETNET_INTEGER_OPERATION_BOUNDS)
    def validate_cometnet_integer_operation(cls, value, info):
        minimum, maximum = _COMETNET_INTEGER_OPERATION_BOUNDS[info.field_name]
        if not minimum <= value <= maximum:
            raise ValueError(
                f"{info.field_name} must be between {minimum} and {maximum}"
            )
        return value

    @field_validator(*_COMETNET_FLOAT_OPERATION_BOUNDS)
    def validate_cometnet_float_operation(cls, value, info):
        minimum, maximum = _COMETNET_FLOAT_OPERATION_BOUNDS[info.field_name]
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError(
                f"{info.field_name} must be finite and between {minimum} and {maximum}"
            )
        return value

    @field_validator(*_COMETNET_REPUTATION_FIELDS)
    def validate_cometnet_reputation_value(cls, value):
        if not math.isfinite(value) or not -1e9 <= value <= 1e9:
            raise ValueError("CometNet reputation values must be finite and bounded")
        return value

    @field_validator(
        "COMETNET_REPUTATION_BONUS_VALID_CONTRIBUTION",
        "COMETNET_REPUTATION_BONUS_PER_DAY_ANCIENNETY",
        "COMETNET_REPUTATION_BONUS_MAX_ANCIENNETY",
        "COMETNET_REPUTATION_PENALTY_INVALID_CONTRIBUTION",
        "COMETNET_REPUTATION_PENALTY_INVALID_SIGNATURE",
    )
    def validate_nonnegative_cometnet_reputation_delta(cls, value):
        if value < 0:
            raise ValueError(
                "CometNet reputation bonuses and penalties cannot be negative"
            )
        return value

    @field_validator(
        *_NONNEGATIVE_HTTP_OPERATION_FIELDS,
        *_POSITIVE_HTTP_OPERATION_FIELDS,
        mode="before",
    )
    def reject_boolean_http_operation_values(cls, value):
        if isinstance(value, bool):
            raise ValueError("HTTP operational numeric values cannot be booleans")
        return value

    @field_validator(*_NONNEGATIVE_HTTP_OPERATION_FIELDS)
    def validate_nonnegative_http_operation_value(cls, value):
        if not math.isfinite(value) or value < 0:
            raise ValueError("HTTP operational values must be finite and non-negative")
        return value

    @field_validator(*_POSITIVE_HTTP_OPERATION_FIELDS)
    def validate_positive_http_operation_value(cls, value):
        if not math.isfinite(value) or value <= 0:
            raise ValueError(
                "HTTP operational values must be finite and greater than zero"
            )
        return value

    @field_validator("RATELIMIT_MAX_RETRIES")
    def validate_rate_limit_retry_count(cls, value):
        if value > 20:
            raise ValueError("RATELIMIT_MAX_RETRIES cannot exceed 20")
        return value

    @field_validator(*_GENERAL_OPERATION_MAXIMA)
    def validate_general_operation_maximum(cls, value, info):
        maximum = _GENERAL_OPERATION_MAXIMA[info.field_name]
        if value > maximum:
            raise ValueError(f"{info.field_name} cannot exceed {maximum}")
        return value

    @field_validator(
        "ADMIN_DASHBOARD_SESSION_TTL",
        "CONFIGURE_PAGE_SESSION_TTL",
        mode="before",
    )
    def reject_boolean_session_ttls(cls, value):
        if isinstance(value, bool):
            raise ValueError("session TTLs cannot be booleans")
        return value

    @field_validator("ADMIN_DASHBOARD_SESSION_TTL", "CONFIGURE_PAGE_SESSION_TTL")
    def validate_session_ttls(cls, value):
        if not 60 <= value <= 31_536_000:
            raise ValueError("session TTLs must be between 60 and 31536000 seconds")
        return value

    @field_validator("ADMIN_DASHBOARD_PASSWORD", "CONFIGURE_PAGE_PASSWORD")
    def validate_dashboard_passwords(cls, value):
        if value is None:
            return None
        return _bounded_credential(value)

    @field_validator(
        "CONFIGURE_PAGE_PASSWORD",
        "PUBLIC_API_TOKEN",
        "PUBLIC_API_TOKEN_FILE",
        "PROMETHEUS_AUTH_TOKEN",
        "PROMETHEUS_AUTH_TOKEN_FILE",
        "PROMETHEUS_QUERY_TOKEN",
        "COMET_CAPABILITY_SECRET",
        "COMET_CAPABILITY_SECRET_FILE",
        *_OPERATOR_CREDENTIAL_FIELDS,
        mode="before",
    )
    def normalize_optional_secrets(cls, v):
        if v is None:
            return None
        if type(v) is not str:
            raise ValueError("configured secrets must be strings")
        return None if v == "" else v

    @field_validator(*_OPERATOR_CREDENTIAL_FIELDS)
    def validate_operator_credential(cls, value):
        if value is None:
            return None
        return _bounded_credential(value)

    @field_validator(
        "GLOBAL_PROXY_URL",
        "USER_PROVIDED_PROXY_URL",
        *_SCRAPER_PROXY_FIELDS,
        mode="before",
    )
    def normalize_optional_proxy_urls(cls, value):
        return _normalize_optional_text(value)

    @field_validator(
        "GLOBAL_PROXY_URL", "USER_PROVIDED_PROXY_URL", *_SCRAPER_PROXY_FIELDS
    )
    def validate_proxy_urls(cls, value):
        if value is None:
            return None
        return _normalize_proxy_url(value)

    @field_validator("PROXY_ETHOS", mode="before")
    def validate_proxy_ethos(cls, value):
        if type(value) is not str:
            raise ValueError("PROXY_ETHOS must be always, on_failure, or never")
        normalized = value.strip().lower()
        if normalized not in {"always", "on_failure", "never"}:
            raise ValueError("PROXY_ETHOS must be always, on_failure, or never")
        return normalized

    @field_validator("PROXY_DEBRID_STREAM_DEBRID_DEFAULT_SERVICE")
    def validate_proxy_default_debrid_service(cls, value):
        if value not in VALID_DEBRID_SERVICES:
            raise ValueError(
                "PROXY_DEBRID_STREAM_DEBRID_DEFAULT_SERVICE is unsupported"
            )
        return value

    @field_validator("PROMETHEUS_AUTH_TOKEN")
    def validate_prometheus_auth_token(cls, value):
        if value is not None and (
            not value.isascii()
            or any(ord(character) < 33 or ord(character) == 127 for character in value)
        ):
            raise ValueError("PROMETHEUS_AUTH_TOKEN must contain visible ASCII")
        return value

    @field_validator(
        "MEDIAFUSION_API_PASSWORD",
        "AIOSTREAMS_USER_UUID_AND_PASSWORD",
        mode="before",
    )
    def validate_scraper_credential_lists(cls, value):
        if value is None:
            return None
        values = value if isinstance(value, list) else [value]
        if len(values) > 64:
            raise ValueError("scraper credential lists may contain at most 64 values")
        normalized = []
        for credential in values:
            if credential is None or credential == "":
                normalized.append("")
                continue
            normalized.append(_bounded_credential(credential))
        return normalized if isinstance(value, list) else normalized[0]

    @field_validator("DEBRIDIO_PROVIDER")
    def validate_debridio_provider(cls, value):
        if value is None:
            return None
        if (
            type(value) is not str
            or not 1 <= len(value) <= 64
            or not value.isascii()
            or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None
        ):
            raise ValueError("DEBRIDIO_PROVIDER must be a bounded identifier")
        return value

    @field_validator("PUBLIC_API_TOKEN")
    def validate_public_api_token(cls, value):
        if value is None:
            return None
        if _PUBLIC_API_TOKEN_PATTERN.fullmatch(value) is None:
            raise ValueError("PUBLIC_API_TOKEN must be 1-256 URL-safe ASCII characters")
        return value

    @field_validator(
        "PUBLIC_API_TOKEN_FILE",
        "PROMETHEUS_AUTH_TOKEN_FILE",
        "COMET_CAPABILITY_SECRET_FILE",
    )
    def validate_secret_file_path(cls, value):
        if value is None:
            return None
        return _bounded_path(value, field="secret file path")

    @field_validator("USENET_PRIVATE_UPSTREAM_ORIGINS")
    def validate_usenet_private_upstream_origins(cls, value):
        if len(value) > 64:
            raise ValueError(
                "USENET_PRIVATE_UPSTREAM_ORIGINS may contain at most 64 origins"
            )
        normalized = []
        for origin in value:
            if not isinstance(origin, str):
                raise ValueError(
                    "USENET_PRIVATE_UPSTREAM_ORIGINS values must be strings"
                )
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError(
                    "USENET_PRIVATE_UPSTREAM_ORIGINS values must be exact HTTP(S) origins"
                )
            try:
                port = parsed.port
            except ValueError as exc:
                raise ValueError(
                    "USENET_PRIVATE_UPSTREAM_ORIGINS contains an invalid port"
                ) from exc
            host = parsed.hostname.lower()
            if any(character.isspace() or ord(character) < 33 for character in host):
                raise ValueError(
                    "USENET_PRIVATE_UPSTREAM_ORIGINS contains an invalid host"
                )
            rendered_host = f"[{host}]" if ":" in host else host
            effective_port = port or (443 if parsed.scheme == "https" else 80)
            canonical = f"{parsed.scheme}://{rendered_host}:{effective_port}"
            if canonical not in normalized:
                normalized.append(canonical)
        return normalized

    @field_validator("USENET_NATIVE_ACCESS_TOKEN")
    def validate_usenet_native_access_token(cls, value):
        if value is None:
            return None
        if not _is_bounded_opaque_credential(value, 256):
            raise ValueError(
                "USENET_NATIVE_ACCESS_TOKEN must be a non-empty opaque value "
                "of at most 256 bytes"
            )
        return value

    @field_validator("COMET_CAPABILITY_SECRET")
    def validate_comet_capability_secret(cls, value):
        if value is None:
            return None
        return _bounded_credential(value)

    @field_validator("USENET_NATIVE_SERVERS")
    def validate_usenet_native_servers(cls, value):
        if not value:
            return []
        return [asdict(server) for server in parse_instance_servers(value)]

    @field_validator("USENET_REPLICA_COUNT")
    def validate_usenet_replica_count(cls, value):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= 64
        ):
            raise ValueError("USENET_REPLICA_COUNT must be between 1 and 64")
        return value

    @field_validator("USENET_NATIVE_MAX_STREAMS")
    def validate_usenet_native_max_streams(cls, value):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= 1024
        ):
            raise ValueError("USENET_NATIVE_MAX_STREAMS must be between 1 and 1024")
        return value

    @field_validator("USENET_MEMORY_CACHE_BYTES")
    def validate_usenet_memory_cache_bytes(cls, value):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 16 * 1024**2 <= value <= 2 * 1024**3
        ):
            raise ValueError(
                "USENET_MEMORY_CACHE_BYTES must be between 16 MiB and 2 GiB"
            )
        return value

    @field_validator("USENET_DISK_CACHE_BYTES")
    def validate_usenet_disk_cache_bytes(cls, value):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= 1024**4
        ):
            raise ValueError("USENET_DISK_CACHE_BYTES must be between zero and 1 TiB")
        return value

    @field_validator("USENET_SPOOL_MAX_BYTES")
    def validate_usenet_spool_max_bytes(cls, value):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1024**3 <= value <= 2 * 1024**4
        ):
            raise ValueError("USENET_SPOOL_MAX_BYTES must be between 1 GiB and 2 TiB")
        return value

    @field_validator("USENET_MIN_FREE_DISK_BYTES")
    def validate_usenet_min_free_disk_bytes(cls, value):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("USENET_MIN_FREE_DISK_BYTES must be non-negative")
        return value

    @field_validator("USENET_ARCHIVE_JOBS")
    def validate_usenet_archive_jobs(cls, value):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("USENET_ARCHIVE_JOBS must be positive")
        return value

    @field_validator("USENET_REPAIR_JOBS")
    def validate_usenet_repair_jobs(cls, value):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("USENET_REPAIR_JOBS must be positive")
        return value

    @field_validator("USENET_START_TIMEOUT_SECONDS")
    def validate_usenet_start_timeout(cls, value):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 5 <= value <= 120
        ):
            raise ValueError("USENET_START_TIMEOUT_SECONDS must be between 5 and 120")
        return value

    @field_validator("USENET_DRAIN_TIMEOUT_SECONDS")
    def validate_usenet_drain_timeout(cls, value):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 5 <= value <= 300
        ):
            raise ValueError("USENET_DRAIN_TIMEOUT_SECONDS must be between 5 and 300")
        return value

    @model_validator(mode="after")
    def validate_usenet_topology(self):
        if self.SCRAPE_ANIMETOSHO_USENET and not self.USENET_ENABLED:
            raise ValueError("Usenet discovery scrapers require USENET_ENABLED")
        if (
            self.USENET_ENGINE_ENABLED or self.USENET_ENGINE_REQUIRED
        ) and not self.USENET_ENABLED:
            raise ValueError(
                "USENET_ENGINE_ENABLED and USENET_ENGINE_REQUIRED require USENET_ENABLED"
            )
        if self.USENET_REPLICA_COUNT > 1 and self.DATABASE_TYPE == "sqlite":
            raise ValueError(
                "USENET_REPLICA_COUNT greater than one requires PostgreSQL"
            )
        if self.USENET_NATIVE_ACCESS_TOKEN is not None:
            object.__setattr__(
                self,
                "usenet_native_access_token_source",
                (
                    "generated_shared"
                    if "USENET_NATIVE_ACCESS_TOKEN"
                    in effective_settings_payload().generated_keys
                    else "configured"
                ),
            )
        if self.USENET_ENABLED:
            if self.USENET_NATIVE_ACCESS_TOKEN is None:
                object.__setattr__(
                    self,
                    "USENET_NATIVE_ACCESS_TOKEN",
                    _generate_secret(),
                )
                object.__setattr__(
                    self,
                    "usenet_native_access_token_source",
                    "generated_memory",
                )
            if (
                self.COMET_CAPABILITY_SECRET is None
                and self.COMET_CAPABILITY_SECRET_FILE is None
            ):
                raise ValueError(
                    "Usenet requires COMET_CAPABILITY_SECRET or "
                    "COMET_CAPABILITY_SECRET_FILE"
                )
            if self.USENET_NATIVE_MAX_STREAMS < self.USENET_REPLICA_COUNT:
                raise ValueError(
                    "USENET_NATIVE_MAX_STREAMS must provide at least one local slot "
                    "per replica"
                )
        return self

    @model_validator(mode="after")
    def validate_general_operation_relationships(self):
        if (
            self.DEBRID_ACCOUNT_SCRAPE_MAX_MATCH_ITEMS
            > self.DEBRID_ACCOUNT_SCRAPE_MAX_SNAPSHOT_ITEMS
        ):
            raise ValueError(
                "DEBRID_ACCOUNT_SCRAPE_MAX_MATCH_ITEMS cannot exceed "
                "DEBRID_ACCOUNT_SCRAPE_MAX_SNAPSHOT_ITEMS"
            )
        fields = self.__dict__
        for scraper_field, url_field in _SCRAPER_ENDPOINT_REQUIREMENTS.items():
            if (
                self.is_any_context_enabled(fields[scraper_field])
                and not fields[url_field]
            ):
                raise ValueError(f"{scraper_field} requires {url_field}")
        for (
            scraper_field,
            credential_fields,
        ) in _SCRAPER_CREDENTIAL_REQUIREMENTS.items():
            if not self.is_any_context_enabled(fields[scraper_field]):
                continue
            missing = [field for field in credential_fields if not fields[field]]
            if missing:
                raise ValueError(f"{scraper_field} requires {', '.join(missing)}")
        for url_field, credential_field in (
            ("MEDIAFUSION_URL", "MEDIAFUSION_API_PASSWORD"),
            ("AIOSTREAMS_URL", "AIOSTREAMS_USER_UUID_AND_PASSWORD"),
        ):
            urls = fields[url_field]
            credentials = fields[credential_field]
            if isinstance(credentials, list) and len(credentials) != (
                len(urls) if isinstance(urls, list) else 1
            ):
                raise ValueError(
                    f"{credential_field} must contain one value per {url_field}"
                )
        return self

    @model_validator(mode="after")
    def validate_background_operation_relationships(self):
        if (
            self.BACKGROUND_SCRAPER_FAILURE_BASE_BACKOFF
            > self.BACKGROUND_SCRAPER_FAILURE_MAX_BACKOFF
        ):
            raise ValueError(
                "BACKGROUND_SCRAPER_FAILURE_BASE_BACKOFF cannot exceed "
                "BACKGROUND_SCRAPER_FAILURE_MAX_BACKOFF"
            )

        low = self.BACKGROUND_SCRAPER_QUEUE_LOW_WATERMARK
        high = self.BACKGROUND_SCRAPER_QUEUE_HIGH_WATERMARK
        hard = self.BACKGROUND_SCRAPER_QUEUE_HARD_CAP
        if (low, high, hard) != (0, 0, 0) and not 0 < low < high <= hard:
            raise ValueError(
                "background queue limits must all be zero or satisfy "
                "0 < low < high <= hard"
            )
        return self

    @model_validator(mode="after")
    def validate_cometnet_relationships(self):
        if self.COMETNET_LISTEN_PORT == self.COMETNET_HTTP_PORT:
            raise ValueError("CometNet WebSocket and HTTP ports must be distinct")
        if self.COMETNET_MIN_PEERS > self.COMETNET_MAX_PEERS:
            raise ValueError("COMETNET_MIN_PEERS cannot exceed COMETNET_MAX_PEERS")
        if self.COMETNET_GOSSIP_FANOUT > self.COMETNET_MAX_PEERS:
            raise ValueError("COMETNET_GOSSIP_FANOUT cannot exceed COMETNET_MAX_PEERS")
        if self.COMETNET_PEX_BATCH_SIZE > self.COMETNET_MAX_PEERS:
            raise ValueError("COMETNET_PEX_BATCH_SIZE cannot exceed COMETNET_MAX_PEERS")
        if not (
            self.COMETNET_REPUTATION_MIN
            <= self.COMETNET_REPUTATION_INITIAL
            <= self.COMETNET_REPUTATION_MAX
        ):
            raise ValueError("CometNet initial reputation must be within its range")
        if not (
            self.COMETNET_REPUTATION_MIN
            <= self.COMETNET_REPUTATION_THRESHOLD_UNTRUSTED
            < self.COMETNET_REPUTATION_THRESHOLD_TRUSTED
            <= self.COMETNET_REPUTATION_MAX
        ):
            raise ValueError(
                "CometNet reputation thresholds must be ordered within its range"
            )
        if self.COMETNET_PRIVATE_NETWORK and (
            self.COMETNET_NETWORK_ID is None or self.COMETNET_NETWORK_PASSWORD is None
        ):
            raise ValueError(
                "private CometNet requires COMETNET_NETWORK_ID and "
                "COMETNET_NETWORK_PASSWORD"
            )
        if (
            self.COMETNET_ENABLED
            and self.COMETNET_RELAY_URL is None
            and not self.COMETNET_PRIVATE_NETWORK
            and not self.COMETNET_ALLOW_PRIVATE_PEX
            and self.COMETNET_ADVERTISE_URL is None
            and not self.COMETNET_UPNP_ENABLED
        ):
            raise ValueError(
                "public CometNet requires an advertise URL, UPnP, "
                "or an explicitly private peer mode"
            )
        return self

    @field_validator("PROMETHEUS_PATH")
    def validate_prometheus_path(cls, value):
        if (
            type(value) is not str
            or not value.startswith("/")
            or value.startswith("//")
            or value == "/"
            or any(character.isspace() for character in value)
            or "{" in value
            or "}" in value
            or "?" in value
            or "#" in value
        ):
            raise ValueError(
                "PROMETHEUS_PATH must be a static absolute path other than '/'"
            )
        return value.rstrip("/")

    @field_validator("PROMETHEUS_MULTIPROC_DIR")
    def validate_prometheus_multiproc_dir(cls, value):
        return _bounded_path(value, field="PROMETHEUS_MULTIPROC_DIR")

    @field_validator(
        "COMET_USENET_ENGINE_BINARY",
        "USENET_RUNTIME_DIR",
        "USENET_LOCAL_DATA_DIR",
        "USENET_ARTIFACT_DIR",
    )
    def validate_usenet_directory(cls, value):
        return _bounded_path(value, field="Usenet directory")

    @field_validator("COMETNET_KEYS_DIR", "COMETNET_POOLS_DIR")
    def validate_optional_cometnet_directory(cls, value):
        if value is None:
            return None
        return _bounded_path(value, field="CometNet directory")

    @field_validator("COMETNET_BOOTSTRAP_NODES", "COMETNET_MANUAL_PEERS", mode="before")
    def validate_cometnet_peer_lists(cls, value):
        if type(value) is not list:
            raise ValueError("CometNet peer lists must be JSON arrays")
        if len(value) > 64:
            raise ValueError("CometNet peer lists may contain at most 64 URLs")
        normalized = [_normalize_websocket_url(url) for url in value]
        return list(dict.fromkeys(normalized))

    @field_validator(
        "COMETNET_ADVERTISE_URL",
        "COMETNET_RELAY_URL",
        "COMETNET_NETWORK_ID",
        "COMETNET_NODE_ALIAS",
        mode="before",
    )
    def normalize_optional_cometnet_text(cls, value):
        if value is None:
            return None
        if value == "":
            return None
        return value

    @field_validator("COMETNET_ADVERTISE_URL")
    def validate_cometnet_advertise_url(cls, value):
        if value is None:
            return None
        return _normalize_websocket_url(value)

    @field_validator("COMETNET_RELAY_URL")
    def validate_cometnet_relay_url(cls, value):
        if value is None:
            return None
        normalized = _normalize_http_url(value)
        if urlsplit(normalized).query:
            raise ValueError("COMETNET_RELAY_URL cannot contain a query")
        return normalized

    @field_validator("COMETNET_CONTRIBUTION_MODE", mode="before")
    def validate_cometnet_contribution_mode(cls, value):
        if type(value) is not str:
            raise ValueError("COMETNET_CONTRIBUTION_MODE must be a supported mode")
        normalized = value.strip().lower()
        if normalized not in {"full", "consumer", "source", "leech"}:
            raise ValueError("COMETNET_CONTRIBUTION_MODE must be a supported mode")
        return normalized

    @field_validator("COMETNET_TRUSTED_POOLS", mode="before")
    def validate_cometnet_pool_lists(cls, value):
        if type(value) is not list:
            raise ValueError("CometNet pool lists must be JSON arrays")
        if len(value) > 64:
            raise ValueError("CometNet pool lists may contain at most 64 IDs")
        normalized = [_normalize_cometnet_pool_id(pool_id) for pool_id in value]
        return list(dict.fromkeys(normalized))

    @field_validator("COMETNET_NETWORK_ID")
    def validate_cometnet_network_id(cls, value):
        if value is None:
            return None
        if (
            type(value) is not str
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value) is None
        ):
            raise ValueError("COMETNET_NETWORK_ID must be a bounded identifier")
        return value

    @field_validator("COMETNET_NODE_ALIAS")
    def validate_cometnet_node_alias(cls, value):
        if value is None:
            return None
        if type(value) is not str or not value or value != value.strip():
            raise ValueError("COMETNET_NODE_ALIAS must be bounded display text")
        try:
            size = len(value.encode("utf-8"))
        except UnicodeEncodeError:
            raise ValueError("COMETNET_NODE_ALIAS must be valid UTF-8") from None
        if size > 128 or any(
            ord(character) < 32 or ord(character) == 127 for character in value
        ):
            raise ValueError("COMETNET_NODE_ALIAS must be bounded display text")
        return value

    @field_validator("ADDON_ID")
    def validate_addon_id(cls, value):
        if (
            type(value) is not str
            or not 1 <= len(value) <= 128
            or not value.isascii()
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value) is None
        ):
            raise ValueError("ADDON_ID must be a bounded ASCII identifier")
        return value

    @field_validator("ADDON_NAME")
    def validate_addon_name(cls, value):
        return _bounded_display_text(
            value,
            field="ADDON_NAME",
            maximum_bytes=256,
        )

    @field_validator("COMET_COMMIT_HASH", "COMET_BUILD_DATE", mode="before")
    def normalize_optional_build_metadata(cls, value):
        return _normalize_optional_text(value)

    @field_validator("COMET_COMMIT_HASH")
    def validate_build_commit(cls, value):
        if value is None:
            return None
        if (normalized := normalize_commit(value)) is None:
            raise ValueError(
                "COMET_COMMIT_HASH must be a 7-to-40 character hexadecimal Git SHA"
            )
        return normalized

    @field_validator("COMET_BUILD_DATE")
    def validate_build_date(cls, value):
        if value is None:
            return None
        if normalize_build_date(value) is None:
            raise ValueError("COMET_BUILD_DATE must be a timezone-aware ISO date")
        return value

    @field_validator("COMET_BRANCH", mode="before")
    def validate_build_branch(cls, value):
        if value is None or value == "":
            return "main"
        if normalize_branch(value) is None:
            raise ValueError("COMET_BRANCH must be a bounded Git branch name")
        return value

    @field_validator(
        "TORRENT_DISABLED_STREAM_NAME",
        "TORRENT_DISABLED_STREAM_DESCRIPTION",
    )
    def validate_disabled_stream_text(cls, value, info):
        if value is None:
            return None
        return _bounded_display_text(
            value,
            field=info.field_name,
            maximum_bytes=(
                256 if info.field_name == "TORRENT_DISABLED_STREAM_NAME" else 1_024
            ),
        )

    @field_validator("CUSTOM_HEADER_HTML", mode="before")
    def normalize_custom_header_html(cls, value):
        return _normalize_optional_text(value)

    @field_validator("CUSTOM_HEADER_HTML")
    def validate_custom_header_html(cls, value):
        if value is None:
            return None
        if type(value) is not str:
            raise ValueError("CUSTOM_HEADER_HTML must be bounded UTF-8")
        try:
            size = len(value.encode("utf-8"))
        except UnicodeEncodeError:
            raise ValueError("CUSTOM_HEADER_HTML must be bounded UTF-8") from None
        if size > 262_144 or "\x00" in value:
            raise ValueError("CUSTOM_HEADER_HTML must be bounded UTF-8")
        return value

    @field_validator("INDEXER_MANAGER_TYPE", mode="before")
    def normalize_indexer_manager_type(cls, value):
        if value is None:
            return None
        if type(value) is not str:
            raise ValueError("INDEXER_MANAGER_TYPE must be jackett or prowlarr")
        normalized = value.strip().lower()
        if not normalized or normalized == "none":
            return None
        if normalized not in {"jackett", "prowlarr"}:
            raise ValueError("INDEXER_MANAGER_TYPE must be jackett or prowlarr")
        return normalized

    @field_validator("DATABASE_TYPE", mode="before")
    def normalize_database_type(cls, v):
        value = str(v).strip().lower()
        if value in {"postgres", "postgresql", "postgresql+asyncpg", "pgsql", "psql"}:
            return "postgresql"
        if value in {"sqlite", "sqlite3"}:
            return "sqlite"
        raise ValueError("DATABASE_TYPE must be sqlite or postgresql")

    @field_validator("DATABASE_URL", mode="before")
    def normalize_database_url(cls, value):
        if value is None or value == "":
            return None
        if type(value) is not str:
            raise ValueError("DATABASE_URL must be a string")
        return value

    @field_validator("DATABASE_PATH", mode="before")
    def validate_database_path(cls, value):
        if value is None or value == "":
            return None
        path = _bounded_path(value, field="DATABASE_PATH")
        if path == ":memory:":
            raise ValueError("DATABASE_PATH must reference a persistent file")
        return path

    @field_validator("DATABASE_READ_REPLICA_URLS")
    def validate_database_replica_urls(cls, value):
        if len(value) > _MAX_DATABASE_REPLICAS:
            raise ValueError(
                f"DATABASE_READ_REPLICA_URLS may contain at most {_MAX_DATABASE_REPLICAS} URLs"
            )
        normalized = []
        seen = set()
        for replica in value:
            canonical = _canonical_postgres_url(replica)
            if canonical not in seen:
                seen.add(canonical)
                normalized.append(replica)
        return normalized

    @model_validator(mode="after")
    def validate_database_configuration(self):
        if self.DATABASE_TYPE == "sqlite":
            if self.DATABASE_PATH is None:
                raise ValueError("SQLite requires DATABASE_PATH")
            if self.DATABASE_READ_REPLICA_URLS:
                raise ValueError("SQLite does not support read replicas")
        elif self.DATABASE_URL is None:
            raise ValueError("PostgreSQL requires DATABASE_URL")
        else:
            _parse_postgres_url(self.DATABASE_URL)
        return self

    @field_validator(
        "INDEXER_MANAGER_INDEXERS", "JACKETT_INDEXERS", "PROWLARR_INDEXERS"
    )
    def indexer_manager_indexers_normalization(cls, v, values):
        if len(v) > 64:
            raise ValueError("indexer lists may contain at most 64 entries")
        normalized = []
        seen = set()
        for indexer in v:
            if not isinstance(indexer, str):
                raise ValueError("indexer names must be strings")
            indexer = normalize_indexer_name(indexer)
            try:
                size = len(indexer.encode("utf-8"))
            except UnicodeEncodeError:
                raise ValueError("indexer names must be valid UTF-8") from None
            if (
                not indexer
                or size > 128
                or any(
                    ord(character) < 32 or ord(character) == 127
                    for character in indexer
                )
            ):
                raise ValueError("indexer names must be bounded text")
            if indexer not in seen:
                seen.add(indexer)
                normalized.append(indexer)
        return normalized

    @field_validator("INDEXER_LANGUAGES")
    def normalize_indexer_languages(cls, value):
        if len(value) > 32:
            raise ValueError("INDEXER_LANGUAGES may contain at most 32 entries")
        languages = []
        seen = set()
        for language in value:
            if (
                not isinstance(language, str)
                or len(normalized := language.strip().lower()) != 2
                or not normalized.isascii()
                or not normalized.isalpha()
            ):
                raise ValueError(
                    "INDEXER_LANGUAGES entries must be ISO 639-1 language codes"
                )
            if normalized not in seen:
                seen.add(normalized)
                languages.append(normalized)
        return languages

    @field_validator(*_OPTIONAL_URL_FIELDS, mode="before")
    def normalize_optional_urls(cls, value):
        return _normalize_optional_text(value)

    @field_validator(*_SCRAPER_URL_FIELDS)
    def normalize_scraper_urls(cls, value):
        if isinstance(value, str):
            return _normalize_scraper_url(value)
        if isinstance(value, list):
            if len(value) > 64:
                raise ValueError("configured URL lists may contain at most 64 entries")
            normalized = []
            for url in value:
                url = _normalize_scraper_url(url)
                if url not in normalized:
                    normalized.append(url)
            return normalized
        return value

    @field_validator(
        "STREMTHRU_URL",
        "PUBLIC_BASE_URL",
        "PROMETHEUS_QUERY_URL",
        "USENET_EXPORT_BASE_URL",
    )
    def normalize_service_urls(cls, value):
        if value is None:
            return None
        return _normalize_http_url(value)

    @field_validator(
        "PUBLIC_BASE_URL",
        "PROMETHEUS_QUERY_URL",
        "USENET_EXPORT_BASE_URL",
    )
    def validate_service_base_urls(cls, value):
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.query:
            raise ValueError("service base URLs cannot contain a query")
        return value

    @field_validator("TORRENT_DISABLED_STREAM_URL")
    def validate_disabled_stream_url(cls, value):
        if value is None:
            return None
        return _normalize_http_url(value)

    def is_scraper_enabled(self, scraper_setting: bool | str, context: str):
        return scraper_setting is True or scraper_setting == context

    def format_scraper_mode(self, scraper_setting: bool | str):
        if scraper_setting is True:
            return "both"
        if scraper_setting is False:
            return "False"
        return scraper_setting

    def is_any_context_enabled(self, scraper_setting: bool | str):
        return scraper_setting is not False

    @property
    def PUBLIC_API_TOKEN_SOURCE(self) -> str:
        return self._public_api_token_source

    @PUBLIC_API_TOKEN_SOURCE.setter
    def PUBLIC_API_TOKEN_SOURCE(self, value: str) -> None:
        self._public_api_token_source = value

    @property
    def COMET_CAPABILITY_SECRET_SOURCE(self) -> str:
        return self._comet_capability_secret_source

    @COMET_CAPABILITY_SECRET_SOURCE.setter
    def COMET_CAPABILITY_SECRET_SOURCE(self, value: str) -> None:
        self._comet_capability_secret_source = value

    @property
    def STREMIO_API_PREFIX(self) -> str:
        return self._stremio_api_prefix

    @STREMIO_API_PREFIX.setter
    def STREMIO_API_PREFIX(self, value: str) -> None:
        self._stremio_api_prefix = value

    @property
    def APPLIED_SETTINGS_REVISION(self) -> int:
        return self._applied_settings_revision

    def model_post_init(self, __context, /):
        if self.INDEXER_MANAGER_TYPE == "jackett":
            if not self.SCRAPE_JACKETT:
                object.__setattr__(
                    self,
                    "SCRAPE_JACKETT",
                    self.INDEXER_MANAGER_MODE,
                )
            if self.JACKETT_URL == "http://127.0.0.1:9117" and self.INDEXER_MANAGER_URL:
                object.__setattr__(self, "JACKETT_URL", self.INDEXER_MANAGER_URL)
            if not self.JACKETT_API_KEY and self.INDEXER_MANAGER_API_KEY:
                object.__setattr__(
                    self,
                    "JACKETT_API_KEY",
                    self.INDEXER_MANAGER_API_KEY,
                )
            if not self.JACKETT_INDEXERS and self.INDEXER_MANAGER_INDEXERS:
                object.__setattr__(
                    self,
                    "JACKETT_INDEXERS",
                    self.INDEXER_MANAGER_INDEXERS,
                )
        elif self.INDEXER_MANAGER_TYPE == "prowlarr":
            if not self.SCRAPE_PROWLARR:
                object.__setattr__(
                    self,
                    "SCRAPE_PROWLARR",
                    self.INDEXER_MANAGER_MODE,
                )
            if (
                self.PROWLARR_URL == "http://127.0.0.1:9696"
                and self.INDEXER_MANAGER_URL
            ):
                object.__setattr__(self, "PROWLARR_URL", self.INDEXER_MANAGER_URL)
            if not self.PROWLARR_API_KEY and self.INDEXER_MANAGER_API_KEY:
                object.__setattr__(
                    self,
                    "PROWLARR_API_KEY",
                    self.INDEXER_MANAGER_API_KEY,
                )
            if not self.PROWLARR_INDEXERS and self.INDEXER_MANAGER_INDEXERS:
                object.__setattr__(
                    self,
                    "PROWLARR_INDEXERS",
                    self.INDEXER_MANAGER_INDEXERS,
                )


def _resolve_persisted_token(
    configured_token: str | None,
    token_file: str | None,
    token_name: str,
    *,
    allow_generate: bool = True,
):
    if configured_token:
        return configured_token, "env"

    if token_file:
        try:
            token_dir = os.path.dirname(token_file)
            if token_dir:
                os.makedirs(token_dir, mode=0o700, exist_ok=True)

            if os.path.exists(token_file):
                existing_token = _read_persisted_token(token_file)
                if existing_token:
                    return existing_token, "file"
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            raise RuntimeError(f"{token_name}_FILE is unreadable") from None

    if not allow_generate:
        raise RuntimeError(f"{token_name} is required") from None

    generated_token = secrets.token_urlsafe(32)
    if not token_file:
        return generated_token, "generated_memory"

    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(token_file, flags, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(generated_token)
                file.flush()
                os.fsync(file.fileno())
        except Exception:
            try:
                os.unlink(token_file)
            except FileNotFoundError:
                pass
            raise
        _fsync_parent_directory(token_file)
        return generated_token, "generated_file"
    except FileExistsError:
        for _ in range(5):
            try:
                existing_token = _read_persisted_token(token_file)
                if existing_token:
                    return existing_token, "file"
            except (OSError, ValueError):
                pass
            time.sleep(0.05)

        error_context = (
            f"{token_name}_FILE exists but remained empty or unreadable after retries"
        )
        raise RuntimeError(error_context) from None


def _read_persisted_token(token_file: str) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    file_descriptor = os.open(token_file, flags)
    try:
        file_stat = os.fstat(file_descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_mode & 0o077:
            raise ValueError("persisted token permissions are unsafe")
        with os.fdopen(file_descriptor, "rb", closefd=False) as file:
            document = file.read(_MAX_PERSISTED_TOKEN_BYTES + 1)
    finally:
        os.close(file_descriptor)
    if len(document) > _MAX_PERSISTED_TOKEN_BYTES:
        raise ValueError("persisted token is too large")
    try:
        return document.decode("utf-8").rstrip("\r\n")
    except UnicodeDecodeError:
        raise ValueError("persisted token is not UTF-8") from None


def _fsync_parent_directory(path: str) -> None:
    parent = os.path.dirname(path) or "."
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    directory_fd = os.open(parent, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _build_stremio_api_prefix(
    config: AppSettings,
    generated_keys: frozenset[str],
):
    should_protect_api = bool(config.CONFIGURE_PAGE_PASSWORD) or bool(
        config.PUBLIC_API_TOKEN
    )
    if not should_protect_api:
        object.__setattr__(config, "_public_api_token_source", "disabled")
        return ""

    token, source = _resolve_persisted_token(
        config.PUBLIC_API_TOKEN,
        config.PUBLIC_API_TOKEN_FILE,
        "PUBLIC_API_TOKEN",
    )
    object.__setattr__(config, "_public_api_token_source", source)
    object.__setattr__(
        config,
        "PUBLIC_API_TOKEN",
        AppSettings.validate_public_api_token(token),
    )
    if "PUBLIC_API_TOKEN" in generated_keys:
        object.__setattr__(
            config,
            "_public_api_token_source",
            "generated_shared",
        )
    return f"/s/{config.PUBLIC_API_TOKEN}"


_effective_settings = effective_settings_payload()
validate_effective_setting_keys(frozenset(AppSettings.model_fields))
_initial_settings = AppSettings(
    **effective_model_inputs(frozenset(AppSettings.model_fields)),
)


def resolve_standalone_cometnet_api_key() -> tuple[str, str]:
    api_key = _bounded_credential(settings.COMETNET_API_KEY)
    source = settings.cometnet_api_key_source
    return api_key, source


def _build_capability_secret(
    config: AppSettings,
    generated_keys: frozenset[str],
) -> None:
    if not config.USENET_ENABLED:
        object.__setattr__(
            config,
            "_comet_capability_secret_source",
            "disabled",
        )
        return

    secret, source = _resolve_persisted_token(
        config.COMET_CAPABILITY_SECRET,
        config.COMET_CAPABILITY_SECRET_FILE,
        "COMET_CAPABILITY_SECRET",
    )
    object.__setattr__(
        config,
        "COMET_CAPABILITY_SECRET",
        AppSettings.validate_comet_capability_secret(secret),
    )
    object.__setattr__(
        config,
        "_comet_capability_secret_source",
        ("generated_shared" if "COMET_CAPABILITY_SECRET" in generated_keys else source),
    )


def finalize_app_settings(
    config: AppSettings,
    *,
    generated_keys: frozenset[str],
    revision: int,
) -> AppSettings:
    object.__setattr__(config, "_applied_settings_revision", revision)
    _build_capability_secret(config, generated_keys)
    object.__setattr__(
        config,
        "_stremio_api_prefix",
        _build_stremio_api_prefix(config, generated_keys),
    )
    return config


finalize_app_settings(
    _initial_settings,
    generated_keys=_effective_settings.generated_keys,
    revision=_effective_settings.revision,
)
STREMIO_API_PREFIX = _initial_settings.STREMIO_API_PREFIX
settings = LiveSettings(_initial_settings)

IS_POSTGRES = settings.DATABASE_TYPE == "postgresql"
IS_SQLITE = settings.DATABASE_TYPE == "sqlite"

JSON_FUNC = "json_array_elements_text" if IS_POSTGRES else "json_each"


class CometSettingsModel(SettingsModel):
    model_config = SettingsConfigDict()

    resolutions: ResolutionConfig = ResolutionConfig(
        r2160p=True,
        r576p=True,
        r480p=True,
        r360p=True,
        r240p=True,
    )

    options: OptionsConfig = OptionsConfig(remove_ranks_under=-10_000_000_000)

    languages: LanguagesConfig = LanguagesConfig(exclude=[])

    custom_ranks: CustomRanksConfig = CustomRanksConfig(
        quality=QualityRankModel(
            av1=CustomRank(fetch=True),
            dvd=CustomRank(fetch=True),
            mpeg=CustomRank(fetch=True),
            remux=CustomRank(fetch=True),
            vhs=CustomRank(fetch=True),
            webmux=CustomRank(fetch=True),
            xvid=CustomRank(fetch=True),
        ),
        rips=RipsRankModel(
            bdrip=CustomRank(fetch=True),
            dvdrip=CustomRank(fetch=True),
            ppvrip=CustomRank(fetch=True),
            satrip=CustomRank(fetch=True),
            tvrip=CustomRank(fetch=True),
            uhdrip=CustomRank(fetch=True),
            vhsrip=CustomRank(fetch=True),
            webdlrip=CustomRank(fetch=True),
        ),
        hdr=HdrRankModel(
            dolby_vision=CustomRank(fetch=True),
        ),
        audio=AudioRankModel(
            mono=CustomRank(fetch=True),
            mp3=CustomRank(fetch=True),
        ),
        extras=ExtrasRankModel(
            three_d=CustomRank(fetch=True),
            converted=CustomRank(fetch=True),
            documentary=CustomRank(fetch=True),
            site=CustomRank(fetch=True),
            upscaled=CustomRank(fetch=True),
        ),
        trash=TrashRankModel(size=CustomRank(fetch=True)),
    )


rtn_settings_default = CometSettingsModel()
rtn_ranking_default = DefaultRanking()


class UserRtnOptions(BaseModel):
    """RTN options exposed through user configuration."""

    model_config = ConfigDict(strict=True, extra="forbid")

    allow_english_in_languages: bool = (
        rtn_settings_default.options.allow_english_in_languages
    )
    remove_unknown_languages: bool = (
        rtn_settings_default.options.remove_unknown_languages
    )


class DebridServiceEntry(BaseModel):
    model_config = ConfigDict(strict=True)

    service: str
    apiKey: str = ""

    @field_validator("service")
    def check_service(cls, v):
        if v not in VALID_DEBRID_SERVICES:
            raise ValueError(f"Invalid debrid service: {v}")
        return v

    @field_validator("apiKey")
    def validate_api_key(cls, value):
        value = _validate_config_secret(value, "debrid apiKey")
        if not value:
            raise ValueError("debrid apiKey is required")
        return value


PLAYBACK_PROVIDER_KINDS = USENET_PLAYBACK_PROVIDER_KINDS | {
    "direct_torrent",
    *VALID_DEBRID_SERVICES,
}
USER_USENET_DISCOVERY_SOURCE_KINDS = (
    "newznab",
    "nzbhydra2",
    "prowlarr_usenet",
    "easynews",
    "stremio_addon",
)
ACCOUNT_KINDS = {
    *VALID_DEBRID_SERVICES,
    "altmount",
    "easynews",
    "indexer",
    "nntp",
    "nzbdav",
    "stremio_addon",
    "stremthru_newz",
}
_PROVIDER_ACCOUNT_KINDS = {
    **{kind: kind for kind in VALID_DEBRID_SERVICES},
    "altmount": "altmount",
    "easynews": "easynews",
    "nzbdav": "nzbdav",
    "stremio_nntp": "nntp",
    "stremthru_newz": "stremthru_newz",
    "torbox_usenet": "torbox",
}
_SOURCE_ACCOUNT_KINDS = {
    "easynews": "easynews",
    "newznab": "indexer",
    "nzbhydra2": "indexer",
    "prowlarr_usenet": "indexer",
    "stremio_addon": "stremio_addon",
}


def _validate_config_secret(value: object, description: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{description} must be bounded text")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError(f"{description} must be valid UTF-8") from None
    if size > 4_096 or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ValueError(f"{description} must be bounded text")
    return value


def _validate_config_account_id(value: object, description: str) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= 128
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{description} must be a bounded identifier")
    return value


def _validate_config_display_name(value: object, description: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{description} must be bounded display text")
    value = value.strip()
    if not value:
        raise ValueError(f"{description} must be bounded display text")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError(f"{description} must be valid UTF-8") from None
    if (
        len(value) > 64
        or size > 256
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{description} must be bounded display text")
    return value


class PlaybackProviderEntry(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    configurationId: str
    displayName: str
    kind: str
    enabled: bool = True
    accountId: str | None = None
    options: dict = Field(default_factory=dict)

    @field_validator("configurationId")
    def validate_configuration_id(cls, value):
        if not isinstance(value, str):
            raise ValueError("provider configurationId must be a UUID")
        try:
            parsed = uuid.UUID(value)
        except ValueError as exc:
            raise ValueError("provider configurationId must be a UUID") from exc
        if str(parsed) != value:
            raise ValueError("provider configurationId must be a canonical UUID")
        return value

    @field_validator("accountId")
    def validate_account_identifier(cls, value):
        if value is None:
            return None
        return _validate_config_account_id(value, "provider accountId")

    @field_validator("displayName")
    def validate_display_name(cls, value):
        return _validate_config_display_name(value, "provider displayName")

    @field_validator("kind")
    def validate_kind(cls, value):
        if value not in PLAYBACK_PROVIDER_KINDS:
            raise ValueError("invalid playback provider kind")
        return value

    @model_validator(mode="after")
    def validate_usenet_provider_options(self):
        if not isinstance(self.options, dict):
            raise ValueError("provider options must be an object")
        if not self.enabled:
            return self
        if self.kind == "stremio_nntp" and self.accountId is None:
            try:
                validate_handoff_config(self.options)
            except ValueError as exc:
                raise ValueError("Stremio NNTP options are invalid") from exc
            return self
        if self.kind != "comet_native_usenet":
            return self
        source = self.options.get("source")
        if source == "instance_pool":
            return self
        if source == "personal_servers" and "servers" in self.options:
            try:
                parse_personal_servers(self.options["servers"])
            except ValueError as exc:
                raise ValueError("native personal NNTP servers are invalid") from exc
            return self
        raise ValueError("native Usenet options must select one valid server source")


class DiscoverySourceEntry(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    configurationId: str
    displayName: str | None = None
    kind: str
    enabled: bool = True
    accountId: str | None = None
    options: dict = Field(default_factory=dict)

    @field_validator("configurationId")
    def validate_configuration_id(cls, value):
        if not isinstance(value, str):
            raise ValueError("discovery configurationId must be a UUID")
        try:
            parsed = uuid.UUID(value)
        except ValueError as exc:
            raise ValueError("discovery configurationId must be a UUID") from exc
        if str(parsed) != value:
            raise ValueError("discovery configurationId must be a canonical UUID")
        return value

    @field_validator("kind")
    def validate_kind(cls, value):
        if value not in USER_USENET_DISCOVERY_SOURCE_KINDS:
            raise ValueError("invalid discovery source kind")
        return value

    @field_validator("displayName")
    def validate_display_name(cls, value):
        if value is None:
            return None
        return _validate_config_display_name(value, "discovery displayName")

    @field_validator("accountId")
    def validate_account_identifier(cls, value):
        if value is None:
            return None
        return _validate_config_account_id(value, "source accountId")

    @model_validator(mode="after")
    def validate_source_options(self):
        if self.kind == "stremio_addon":
            base_url = self.options.get("baseUrl")
            manifest_url = self.options.get("manifestUrl")
            configured_url = manifest_url or base_url
            if not isinstance(configured_url, str) or not configured_url:
                raise ValueError("Stremio addon requires a configured URL")
            maximum = self.options.get("maxResults", 3)
            if (
                isinstance(maximum, bool)
                or not isinstance(maximum, int)
                or not 1 <= maximum <= 10
            ):
                raise ValueError("Stremio addon result limit is invalid")
            authorization = self.options.get("authorization")
            if authorization is not None and (
                not isinstance(authorization, str)
                or not authorization
                or authorization != authorization.strip()
                or len(authorization) > 2_048
                or any(
                    ord(character) < 32 or ord(character) == 127
                    for character in authorization
                )
            ):
                raise ValueError("Stremio addon credential is invalid")
        return self


_V2_CONFIG_FIELDS = {
    "schemaVersion",
    "enabledTransports",
    "discoverySources",
    "playbackProviders",
    "accounts",
    "nativeAccessToken",
    "cachedOnly",
    "removeTrash",
    "resultFormat",
    "maxResultsPerResolution",
    "maxSize",
    "scrapeDebridAccountTorrents",
    "debridStreamProxyPassword",
    "languages",
    "resolutions",
    "options",
}


class ConfigModel(BaseModel):
    model_config = ConfigDict(validate_default=True)

    schemaVersion: int = 1
    enabledTransports: list[str] | None = None
    discoverySources: list[DiscoverySourceEntry] | None = None
    playbackProviders: list[PlaybackProviderEntry] | None = None
    accounts: dict[str, dict] | None = None
    nativeAccessToken: str | None = None
    cachedOnly: bool | None = False
    removeTrash: bool | None = True
    resultFormat: list[str] | None = Field(default_factory=lambda: ["all"])
    maxResultsPerResolution: int | None = 0
    maxSize: float | None = 0

    # Legacy single-service fields
    debridService: str | None = "torrent"
    debridApiKey: str | None = ""

    # Multi-Debrid fields
    debridServices: list[DebridServiceEntry] | None = Field(default_factory=list)
    enableTorrent: bool | None = False
    scrapeDebridAccountTorrents: bool | None = False

    debridStreamProxyPassword: str | None = ""
    languages: dict | None = Field(
        default_factory=lambda: rtn_settings_default.languages.model_dump()
    )
    resolutions: dict | None = Field(
        default_factory=lambda: rtn_settings_default.resolutions.model_dump()
    )
    options: UserRtnOptions | None = Field(default_factory=UserRtnOptions)

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_rank_threshold(cls, value):
        if not isinstance(value, dict) or value.get("schemaVersion", 1) != 1:
            return value
        options = value.get("options")
        if not isinstance(options, dict) or "remove_ranks_under" not in options:
            return value
        normalized = dict(value)
        normalized["options"] = {
            key: option
            for key, option in options.items()
            if key != "remove_ranks_under"
        }
        return normalized

    @model_validator(mode="before")
    @classmethod
    def validate_v2_closed_shape(cls, value):
        if not isinstance(value, dict) or value.get("schemaVersion", 1) != 2:
            return value
        if type(value.get("schemaVersion")) is not int:
            raise ValueError("schemaVersion must be an integer")
        unexpected = value.keys() - _V2_CONFIG_FIELDS
        if unexpected:
            raise ValueError("schema version 2 contains unsupported fields")
        if "enabledTransports" not in value:
            raise ValueError("schema version 2 requires enabledTransports")
        for name in ("enabledTransports", "discoverySources", "playbackProviders"):
            if name in value and type(value[name]) is not list:
                raise ValueError(f"{name} must be a list")
        if "accounts" in value and type(value["accounts"]) is not dict:
            raise ValueError("accounts must be an object")
        for name in ("discoverySources", "playbackProviders"):
            if len(value.get(name) or ()) > 64:
                raise ValueError(f"{name} may contain at most 64 entries")
        if len(value.get("accounts") or {}) > 64:
            raise ValueError("accounts may contain at most 64 entries")
        for name in (
            "cachedOnly",
            "removeTrash",
            "scrapeDebridAccountTorrents",
        ):
            if name in value and type(value[name]) is not bool:
                raise ValueError(f"{name} must be a boolean")
        return value

    @field_validator("schemaVersion", mode="before")
    def validate_schema_version(cls, value):
        if type(value) is not int or value not in {1, 2}:
            raise ValueError("unsupported configuration schema version")
        return value

    @field_validator("maxResultsPerResolution", mode="before")
    def check_max_results_per_resolution(cls, v):
        if type(v) is not int or not 0 <= v <= 1_000:
            raise ValueError("maxResultsPerResolution must be between 0 and 1000")
        return v

    @field_validator("maxSize", mode="before")
    def check_max_size(cls, v):
        if (
            type(v) not in (int, float)
            or not math.isfinite(v)
            or not 0 <= v <= 2**63 - 1
        ):
            raise ValueError("maxSize must be a finite signed-64-bit byte count")
        return float(v)

    @field_validator("resultFormat", mode="before")
    def validate_result_format(cls, value):
        allowed = {
            "all",
            "title",
            "video_info",
            "audio_info",
            "quality_info",
            "release_group",
            "seeders",
            "size",
            "tracker",
            "languages",
        }
        if (
            type(value) is not list
            or not 1 <= len(value) <= 64
            or any(type(item) is not str or item not in allowed for item in value)
        ):
            raise ValueError("resultFormat must contain supported fields")
        return list(dict.fromkeys(value))

    @field_validator("enabledTransports")
    def validate_enabled_transports(cls, value):
        if value is None:
            return None
        if any(transport not in {"bittorrent", "usenet"} for transport in value):
            raise ValueError("enabledTransports contains an unsupported transport")
        return list(dict.fromkeys(value))

    @field_validator(
        "debridApiKey",
        "debridStreamProxyPassword",
    )
    def validate_legacy_config_secrets(cls, value, info):
        return _validate_config_secret(value, info.field_name)

    @field_validator("debridService")
    def check_debrid_service(cls, v):
        if v not in (*VALID_DEBRID_SERVICES, "torrent"):
            raise ValueError("Invalid debridService")
        return v

    @field_validator("debridServices", mode="before")
    def validate_debrid_services(cls, v):
        if v is None:
            return []
        if type(v) is not list or len(v) > 64:
            raise ValueError("debridServices must be a bounded list")
        return v

    @field_validator("debridServices")
    def validate_unique_debrid_services(cls, value):
        if len(value) != len({entry.service for entry in value}):
            raise ValueError("debrid services must be unique")
        return value

    @model_validator(mode="after")
    def validate_v2_configuration(self):
        if self.schemaVersion == 1:
            if self.debridService != "torrent" and not self.debridApiKey:
                raise ValueError("debrid apiKey is required")
            return self
        providers = self.playbackProviders or []
        provider_ids = [provider.configurationId for provider in providers]
        if len(provider_ids) != len(set(provider_ids)):
            raise ValueError("playback provider identifiers must be unique")
        playback_services = [
            provider.kind
            for provider in providers
            if provider.kind == "direct_torrent"
            or provider.kind in VALID_DEBRID_SERVICES
        ]
        if len(playback_services) != len(set(playback_services)):
            raise ValueError("playback services must be unique")
        source_ids = [source.configurationId for source in self.discoverySources or []]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("discovery source identifiers must be unique")
        accounts = self.accounts or {}
        for account_id, account in accounts.items():
            if not isinstance(account, dict):
                raise ValueError("account envelope is invalid")
            _validate_config_account_id(account_id, "account ID")
            account_kind = account.get("kind")
            if account_kind not in ACCOUNT_KINDS:
                raise ValueError("account kind is invalid")

        for entry, expected in (
            *(
                (provider, _PROVIDER_ACCOUNT_KINDS.get(provider.kind))
                for provider in providers
            ),
            *(
                (source, _SOURCE_ACCOUNT_KINDS.get(source.kind))
                for source in self.discoverySources or []
            ),
        ):
            account_id = entry.accountId
            if account_id is None:
                continue
            if expected is None:
                raise ValueError("this binding cannot reference an account")
            account = accounts.get(account_id)
            if account is None:
                raise ValueError("account reference is unavailable")
            if account["kind"] != expected:
                raise ValueError("account kind does not match its binding")
        for provider in providers:
            if provider.enabled and provider.kind in VALID_DEBRID_SERVICES:
                if provider.accountId is None:
                    raise ValueError("debrid provider requires an account")
                api_key = accounts[provider.accountId].get("apiKey")
                _validate_config_secret(api_key, "debrid apiKey")
                if not api_key:
                    raise ValueError("debrid apiKey is required")
            if (
                not provider.enabled
                or provider.kind != "stremio_nntp"
                or provider.accountId is None
            ):
                continue
            merged_options = {
                key: value
                for key, value in accounts[provider.accountId].items()
                if key != "kind"
            }
            merged_options.update(provider.options)
            try:
                validate_handoff_config(merged_options)
            except ValueError as exc:
                raise ValueError("Stremio NNTP options are invalid") from exc
        if self.nativeAccessToken is not None:
            if not _is_bounded_opaque_credential(self.nativeAccessToken, 256):
                raise ValueError(
                    "nativeAccessToken must be an opaque value of at most 256 bytes"
                )
        return self


default_config = ConfigModel().model_dump()
default_config["rtnSettings"] = rtn_settings_default
default_config["rtnRanking"] = rtn_ranking_default


web_config = {
    "resolutions": [resolution.value for resolution in RTN.extras.Resolution],
    "resultFormat": [
        "title",
        "video_info",
        "audio_info",
        "quality_info",
        "release_group",
        "seeders",
        "size",
        "tracker",
        "languages",
    ],
}


def native_usenet_sources(configuration: AppSettings) -> tuple[str, ...]:
    return (
        *(("instance_pool",) if configuration.USENET_NATIVE_SERVERS else ()),
        *(
            ("personal_servers",)
            if configuration.USENET_NATIVE_ALLOW_USER_SERVERS
            else ()
        ),
    )


def native_usenet_offered(configuration: AppSettings) -> bool:
    return bool(
        configuration.USENET_ENABLED
        and configuration.USENET_ENGINE_ENABLED
        and configuration.USENET_NATIVE_ACCESS_TOKEN
        and native_usenet_sources(configuration)
    )


def _build_database_instance(raw_url: str):
    if IS_SQLITE:
        return Database(f"sqlite:///{raw_url}")

    parsed = _parse_postgres_url(raw_url).set(drivername="postgresql+asyncpg")
    return Database(parsed.render_as_string(hide_password=False))


replica_instances: list[Database] = []
force_ipv4 = False

if IS_SQLITE:
    database_url = settings.DATABASE_PATH
else:
    database_url = settings.DATABASE_URL
    replica_instances = [
        _build_database_instance(url)
        for url in settings.DATABASE_READ_REPLICA_URLS
        if url
    ]
    force_ipv4 = settings.DATABASE_FORCE_IPV4_RESOLUTION and IS_POSTGRES


database = ReplicaAwareDatabase(
    _build_database_instance(database_url),
    replicas=replica_instances,
    force_ipv4=force_ipv4,
)
