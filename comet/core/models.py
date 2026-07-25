import math
import os
import secrets
import string
import time
from collections.abc import Awaitable, Callable

import RTN
from databases import Database
from databases.backends.sqlite import SQLiteConnection
from pydantic import BaseModel, Field, field_validator
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

from comet.core.db_router import ReplicaAwareDatabase
from comet.core.logger import logger
from comet.core.scrape import normalize_scraper_timeout_selector
from comet.core.server_settings import ServerSettings

_comet_fk_enabled = False
_SQLITE_BUSY_TIMEOUT_MS = 30000
_SECRET_ALPHABET = string.ascii_letters + string.digits


def _generate_secret() -> str:
    return "".join(secrets.choice(_SECRET_ALPHABET) for _ in range(16))


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
    "SCRAPE_TORBOX",
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
_POSITIVE_COMETNET_OPERATION_FIELDS = (
    "COMETNET_MAX_PEERS",
    "COMETNET_TIME_CHECK_TIMEOUT",
    "COMETNET_REACHABILITY_RETRIES",
    "COMETNET_REACHABILITY_TIMEOUT",
    "COMETNET_UPNP_LEASE_DURATION",
    "COMETNET_STATE_SAVE_INTERVAL",
    "COMETNET_GOSSIP_INTERVAL",
    "COMETNET_GOSSIP_MESSAGE_TTL",
    "COMETNET_GOSSIP_MAX_TORRENTS_PER_MESSAGE",
    "COMETNET_GOSSIP_TORRENT_MAX_AGE",
    "COMETNET_PEX_BATCH_SIZE",
    "COMETNET_PEER_CONNECT_BACKOFF_MAX",
    "COMETNET_PEER_MAX_FAILURES",
    "COMETNET_PEER_CLEANUP_AGE",
    "COMETNET_TRANSPORT_MAX_MESSAGE_SIZE",
    "COMETNET_TRANSPORT_MAX_CONNECTIONS_PER_IP",
    "COMETNET_TRANSPORT_PING_INTERVAL",
    "COMETNET_TRANSPORT_CONNECTION_TIMEOUT",
    "COMETNET_TRANSPORT_MAX_LATENCY_MS",
    "COMETNET_TRANSPORT_RATE_LIMIT_COUNT",
    "COMETNET_TRANSPORT_RATE_LIMIT_WINDOW",
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
    ADDON_ID: str | None = "stremio.comet.fast"
    ADDON_NAME: str | None = "Comet"
    EXECUTOR_MAX_WORKERS: int | None = 1
    ADMIN_DASHBOARD_PASSWORD: str | None = Field(default_factory=_generate_secret)
    ADMIN_DASHBOARD_SESSION_TTL: int | None = 86400
    CONFIGURE_PAGE_PASSWORD: str | None = None
    CONFIGURE_PAGE_SESSION_TTL: int | None = 86400
    PUBLIC_API_TOKEN: str | None = None
    PUBLIC_API_TOKEN_FILE: str | None = "data/public_api_token.txt"
    PUBLIC_METRICS_API: bool | None = False
    PROMETHEUS_ENABLED: bool = False
    PROMETHEUS_PATH: str = "/metrics"
    PROMETHEUS_AUTH_TOKEN: str | None = None
    PROMETHEUS_AUTH_TOKEN_FILE: str | None = None
    PROMETHEUS_MULTIPROC_DIR: str = "/tmp/comet-prometheus"
    DATABASE_TYPE: str | None = "sqlite"
    DATABASE_URL: str | None = "username:password@hostname:port"
    DATABASE_PATH: str | None = "data/comet.db"
    DATABASE_BATCH_SIZE: int | None = 20000
    DATABASE_READ_REPLICA_URLS: list[str] = Field(default_factory=list)
    DATABASE_STARTUP_CLEANUP_INTERVAL: int | None = 3600
    MEMORY_TRIM_INTERVAL: int | None = 300
    DATABASE_FORCE_IPV4_RESOLUTION: bool | None = False
    METADATA_CACHE_TTL: int | None = 2592000  # 30 days
    TORRENT_CACHE_TTL: int | None = 2592000  # 30 days
    LIVE_TORRENT_CACHE_TTL: int | None = 604800  # 7 days
    DEBRID_CACHE_TTL: int | None = 86400  # 1 day
    METRICS_CACHE_TTL: int | None = 60  # 1 minute
    DEBRID_CACHE_CHECK_RATIO: float | None = 0.0  # 0.0 to 1.0
    SCRAPE_LOCK_TTL: int | None = 300  # 5 minutes
    LIVE_SCRAPE_TIMEOUT: float = 30.0
    BACKGROUND_SCRAPE_TIMEOUT: float = 30.0
    SCRAPER_TIMEOUT_OVERRIDES: dict[str, float] = Field(default_factory=dict)
    INDEXER_MANAGER_TYPE: str | None = None
    INDEXER_MANAGER_URL: str | None = "http://127.0.0.1:9117"
    INDEXER_MANAGER_API_KEY: str | None = None
    INDEXER_MANAGER_MODE: bool | str = "both"
    INDEXER_MANAGER_TIMEOUT: int | None = 30
    INDEXER_MANAGER_INDEXERS: list[str] = Field(default_factory=list)
    INDEXER_MANAGER_UPDATE_INTERVAL: int | None = 900
    INDEXER_MANAGER_WAIT_TIMEOUT: int | None = 30
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
    GET_TORRENT_TIMEOUT: int | None = 5
    MAGNET_RESOLVE_TIMEOUT: int | None = 60
    CATALOG_TIMEOUT: int | None = 30
    DOWNLOAD_TORRENT_FILES: bool | None = False
    SCRAPE_COMET: bool | str = False
    COMET_URL: str | list[str] = "https://comet.feels.legal"
    COMET_CLEAN_TRACKER: bool | None = False
    SCRAPE_NYAA: bool | str = False
    NYAA_ANIME_ONLY: bool | None = True
    NYAA_MAX_CONCURRENT_PAGES: int | None = 5
    SCRAPE_ANIMETOSHO: bool | str = False
    ANIMETOSHO_ANIME_ONLY: bool | None = True
    ANIMETOSHO_MAX_CONCURRENT_PAGES: int | None = 10
    SCRAPE_SEADEX: bool | str = False
    SEADEX_ANIME_ONLY: bool | None = True
    SCRAPE_NEKOBT: bool | str = False
    NEKOBT_ANIME_ONLY: bool | None = True
    SCRAPE_ZILEAN: bool | str = False
    ZILEAN_URL: str | list[str] = "https://zileanfortheweebs.midnightignite.me"
    SCRAPE_STREMTHRU: bool | str = False
    STREMTHRU_SCRAPE_URL: str | list[str] = "https://stremthru.13377001.xyz"
    SCRAPE_DMM: bool | str = False
    DMM_INGEST_ENABLED: bool | None = False
    DMM_INGEST_INTERVAL: int | None = 86400
    DMM_INGEST_CONCURRENT_WORKERS: int | None = 4
    DMM_INGEST_BATCH_SIZE: int | None = 100
    SCRAPE_BITMAGNET: bool | str = False
    BITMAGNET_URL: str | list[str] = "https://bitmagnetfortheweebs.midnightignite.me"
    BITMAGNET_MAX_CONCURRENT_PAGES: int | None = 5
    BITMAGNET_MAX_OFFSET: int | None = 15000
    SCRAPE_TORRENTIO: bool | str = False
    TORRENTIO_URL: str | list[str] = "https://torrentio.strem.fun"
    SCRAPE_MEDIAFUSION: bool | str = False
    MEDIAFUSION_URL: str | list[str] = "https://mediafusion.elfhosted.com"
    MEDIAFUSION_API_PASSWORD: str | list[str] | None = None
    MEDIAFUSION_LIVE_SEARCH: bool | None = True
    SCRAPE_AIOSTREAMS: bool | str = False
    AIOSTREAMS_URL: str | list[str] | None = None
    AIOSTREAMS_USER_UUID_AND_PASSWORD: str | list[str] | None = None
    SCRAPE_JACKETTIO: bool | str = False
    JACKETTIO_URL: str | list[str] | None = None
    SCRAPE_DEBRIDIO: bool | str = False
    DEBRIDIO_API_KEY: str | None = None
    DEBRIDIO_PROVIDER: str | None = None
    DEBRIDIO_PROVIDER_KEY: str | None = None
    SCRAPE_TORBOX: bool | str = False
    TORBOX_API_KEY: str | None = None
    SCRAPE_TORRENTSDB: bool | str = False
    SCRAPE_PEERFLIX: bool | str = False
    CUSTOM_HEADER_HTML: str | None = None
    PROXY_DEBRID_STREAM: bool | None = False
    PROXY_DEBRID_STREAM_PASSWORD: str | None = Field(default_factory=_generate_secret)
    PROXY_DEBRID_STREAM_MAX_CONNECTIONS: int | None = -1
    PROXY_DEBRID_STREAM_DEBRID_DEFAULT_SERVICE: str | None = "realdebrid"
    PROXY_DEBRID_STREAM_DEBRID_DEFAULT_APIKEY: str | None = None
    PROXY_DEBRID_STREAM_INACTIVITY_THRESHOLD: int | None = 300
    DEBRID_ACCOUNT_SCRAPE_REFRESH_INTERVAL: int = 900
    DEBRID_ACCOUNT_SCRAPE_CACHE_TTL: int = 86400
    DEBRID_ACCOUNT_SCRAPE_MAX_SNAPSHOT_ITEMS: int = 5000
    DEBRID_ACCOUNT_SCRAPE_MAX_MATCH_ITEMS: int = 1500
    DEBRID_ACCOUNT_SCRAPE_INITIAL_WARM_TIMEOUT: float = 5.0
    STREMTHRU_URL: str | None = "https://stremthru.13377001.xyz"
    DISABLE_TORRENT_STREAMS: bool | None = False
    TORRENT_DISABLED_STREAM_NAME: str | None = "[INFO] Comet"
    TORRENT_DISABLED_STREAM_DESCRIPTION: str | None = (
        "Direct torrent playback is disabled on this server."
    )
    TORRENT_DISABLED_STREAM_URL: str | None = "https://comet.feels.legal"
    PUBLIC_BASE_URL: str | None = None
    REMOVE_ADULT_CONTENT: bool | None = False
    BACKGROUND_SCRAPER_ENABLED: bool | None = False
    BACKGROUND_SCRAPER_CONCURRENT_WORKERS: int | None = 1
    BACKGROUND_SCRAPER_INTERVAL: int | None = 3600
    BACKGROUND_SCRAPER_MAX_MOVIES_PER_RUN: int | None = 100
    BACKGROUND_SCRAPER_MAX_SERIES_PER_RUN: int | None = 100
    BACKGROUND_SCRAPER_SUCCESS_TTL: int | None = 604800
    BACKGROUND_SCRAPER_FAILURE_BASE_BACKOFF: int | None = 3600
    BACKGROUND_SCRAPER_FAILURE_MAX_BACKOFF: int | None = 604800
    BACKGROUND_SCRAPER_MAX_RETRIES: int | None = 6
    BACKGROUND_SCRAPER_RUN_TIME_BUDGET: int | None = 1800
    BACKGROUND_SCRAPER_DISCOVERY_MULTIPLIER: int | None = 3
    BACKGROUND_SCRAPER_MAX_EPISODES_PER_SERIES_PER_RUN: int | None = 25
    BACKGROUND_SCRAPER_EPISODE_REFRESH_TTL: int | None = 21600
    BACKGROUND_SCRAPER_ENABLE_DEMAND_PRIORITY: bool | None = True
    BACKGROUND_SCRAPER_DEMAND_LOOKBACK: int | None = 86400
    BACKGROUND_SCRAPER_DEFER_COOLDOWN: int | None = 300
    BACKGROUND_SCRAPER_MIN_PRIORITY_SCORE: float | None = 0.0
    BACKGROUND_SCRAPER_PRIORITY_DECAY_ON_MISS: float | None = 0.9
    BACKGROUND_SCRAPER_QUEUE_LOW_WATERMARK: int | None = 10000
    BACKGROUND_SCRAPER_QUEUE_HIGH_WATERMARK: int | None = 20000
    BACKGROUND_SCRAPER_QUEUE_HARD_CAP: int | None = 30000
    BACKGROUND_SCRAPER_ALERT_FAIL_RATE: float | None = 0.7
    BACKGROUND_SCRAPER_ALERT_QUEUE_AGE: int | None = 86400
    BACKGROUND_SCRAPER_RUN_RETENTION_DAYS: int | None = 30
    ANIME_MAPPING_ENABLED: bool | None = True
    ANIME_MAPPING_REFRESH_INTERVAL: int | None = 432000
    DIGITAL_RELEASE_FILTER: bool | None = False
    TMDB_READ_ACCESS_TOKEN: str | None = None
    GLOBAL_PROXY_URL: str | None = None
    PROXY_ETHOS: str | None = "always"
    RATELIMIT_MAX_RETRIES: int | None = 3
    RATELIMIT_RETRY_BASE_DELAY: float | None = 1.0
    RTN_FILTER_DEBUG: bool | None = False
    FILTER_PARSE_CACHE_SIZE: int | None = 10000
    FILTER_PARSE_CACHE_SHARDS: int | None = 8
    FILTER_PARSE_CACHE_DEDUP_INFLIGHT: bool | None = True
    HTTP_CACHE_ENABLED: bool | None = False
    HTTP_CLIENT_LIMIT: int | None = 100
    HTTP_CLIENT_LIMIT_PER_HOST: int | None = 20
    HTTP_CLIENT_TTL_DNS_CACHE: int | None = 300
    HTTP_CLIENT_KEEPALIVE_TIMEOUT: float | None = 30.0
    HTTP_CLIENT_TIMEOUT_TOTAL: float | None = 30.0
    HTTP_CACHE_STREAMS_TTL: int | None = 300
    HTTP_CACHE_STALE_WHILE_REVALIDATE: int | None = 60
    HTTP_CACHE_MANIFEST_TTL: int | None = 86400
    HTTP_CACHE_CONFIGURE_TTL: int | None = 86400
    DOWNLOAD_GENERIC_TRACKERS: bool | None = False
    SMART_LANGUAGE_DETECTION: bool | None = False

    # CometNet P2P Network Configuration
    COMETNET_ENABLED: bool | None = False
    COMETNET_LISTEN_PORT: int | None = 8765
    COMETNET_HTTP_PORT: int | None = 8766
    COMETNET_BOOTSTRAP_NODES: list[str] = Field(default_factory=list)
    COMETNET_MANUAL_PEERS: list[str] = Field(default_factory=list)
    COMETNET_MAX_PEERS: int | None = 50
    COMETNET_MIN_PEERS: int | None = 3
    COMETNET_KEYS_DIR: str | None = "data/cometnet"
    COMETNET_ADVERTISE_URL: str | None = None
    COMETNET_KEY_PASSWORD: str | None = None
    COMETNET_ALLOW_PRIVATE_PEX: bool | None = False
    COMETNET_SKIP_REACHABILITY_CHECK: bool | None = False
    COMETNET_SKIP_TIME_CHECK: bool | None = False
    COMETNET_TIME_CHECK_TOLERANCE: int | None = 60
    COMETNET_TIME_CHECK_TIMEOUT: int | None = 5
    COMETNET_REACHABILITY_RETRIES: int | None = 5
    COMETNET_REACHABILITY_RETRY_DELAY: int | None = 10
    COMETNET_REACHABILITY_TIMEOUT: int | None = 10
    COMETNET_UPNP_ENABLED: bool | None = False
    COMETNET_UPNP_LEASE_DURATION: int | None = 3600
    COMETNET_RELAY_URL: str | None = None
    COMETNET_API_KEY: str | None = Field(
        default_factory=_generate_secret
    )  # API key for standalone service auth
    COMETNET_STATE_SAVE_INTERVAL: int | None = (
        300  # Periodic state save interval in seconds (5 minutes)
    )

    # CometNet Gossip Tuning
    COMETNET_GOSSIP_FANOUT: int | None = 3
    COMETNET_GOSSIP_INTERVAL: float | None = 1.0
    COMETNET_GOSSIP_MESSAGE_TTL: int | None = 5
    COMETNET_GOSSIP_MAX_TORRENTS_PER_MESSAGE: int | None = 1000

    COMETNET_GOSSIP_VALIDATION_FUTURE_TOLERANCE: int | None = 60
    COMETNET_GOSSIP_VALIDATION_PAST_TOLERANCE: int | None = 300
    COMETNET_GOSSIP_TORRENT_MAX_AGE: int | None = 604800

    # CometNet Discovery Tuning
    COMETNET_PEX_BATCH_SIZE: int | None = 20
    COMETNET_PEER_CONNECT_BACKOFF_MAX: int | None = 300
    COMETNET_PEER_MAX_FAILURES: int | None = 5
    COMETNET_PEER_CLEANUP_AGE: int | None = 604800

    # CometNet Transport Tuning
    COMETNET_TRANSPORT_MAX_MESSAGE_SIZE: int | None = 10485760  # 10MB
    COMETNET_TRANSPORT_MAX_CONNECTIONS_PER_IP: int | None = 3
    COMETNET_TRANSPORT_PING_INTERVAL: float | None = 30.0
    COMETNET_TRANSPORT_CONNECTION_TIMEOUT: float | None = 120.0
    COMETNET_TRANSPORT_MAX_LATENCY_MS: float | None = (
        10000.0  # Max acceptable latency before disconnection
    )
    COMETNET_TRANSPORT_WEBSOCKET_COMPRESSION_ENABLED: bool | None = False
    COMETNET_TRANSPORT_RATE_LIMIT_ENABLED: bool | None = True
    COMETNET_TRANSPORT_RATE_LIMIT_COUNT: int | None = 20  # Messages per window
    COMETNET_TRANSPORT_RATE_LIMIT_WINDOW: float | None = 1.0  # Seconds

    # CometNet Reputation Tuning
    COMETNET_REPUTATION_INITIAL: float | None = 100.0
    COMETNET_REPUTATION_MIN: float | None = 0.0
    COMETNET_REPUTATION_MAX: float | None = 10000.0
    COMETNET_REPUTATION_THRESHOLD_UNTRUSTED: float | None = 50.0  # Ban threshold
    COMETNET_REPUTATION_THRESHOLD_TRUSTED: float | None = (
        1000.0  # Trust threshold (approx 1 day of heavy scraping)
    )
    COMETNET_REPUTATION_BONUS_VALID_CONTRIBUTION: float | None = (
        0.001  # 1M torrents = 1000 pts
    )
    COMETNET_REPUTATION_BONUS_PER_DAY_ANCIENNETY: float | None = 10.0
    COMETNET_REPUTATION_BONUS_MAX_ANCIENNETY: float | None = (
        1000.0  # Max 1000 pts from age (100 days)
    )
    COMETNET_REPUTATION_PENALTY_INVALID_CONTRIBUTION: float | None = 50.0
    COMETNET_REPUTATION_PENALTY_INVALID_SIGNATURE: float | None = 500.0

    # CometNet Contribution Mode
    # full: Share own torrents + receive + repropagate (default)
    # consumer: Receive + repropagate, but don't share own torrents
    # source: Share own torrents only (dedicated scraper)
    # leech: Receive only, don't repropagate (save bandwidth)
    COMETNET_CONTRIBUTION_MODE: str | None = "full"

    # CometNet Trust Pools
    # List of pool IDs to subscribe to
    # If empty: accept from everyone (open mode)
    COMETNET_TRUSTED_POOLS: list[str] = Field(default_factory=list)
    # Directory for storing pool manifests and membership data
    COMETNET_POOLS_DIR: str | None = "data/cometnet/pools"

    # CometNet Private Network
    # Isolate this node in a private network
    COMETNET_PRIVATE_NETWORK: bool | None = False
    # Network ID for private network (required if PRIVATE_NETWORK=true)
    COMETNET_NETWORK_ID: str | None = None
    # Password to join the private network (Argon2 hashed for auth)
    COMETNET_NETWORK_PASSWORD: str | None = None
    # Pools to ingest from even when in private mode
    COMETNET_INGEST_POOLS: list[str] = Field(default_factory=list)

    # CometNet Node Alias
    # Optional friendly name for this node (exchanged with other peers)
    # If not set, users will only see the Node ID
    COMETNET_NODE_ALIAS: str | None = None

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

    @staticmethod
    def _normalize_scrape_timeout(value) -> float:
        if isinstance(value, bool):
            raise ValueError("scrape timeouts must be finite numbers greater than zero")
        try:
            normalized = float(value)
        except (TypeError, ValueError):
            raise ValueError(
                "scrape timeouts must be finite numbers greater than zero"
            ) from None
        if not math.isfinite(normalized) or normalized <= 0:
            raise ValueError("scrape timeouts must be finite numbers greater than zero")
        return normalized

    @field_validator(*_SCRAPE_TIMEOUT_FIELDS, mode="before")
    def validate_scrape_timeout(cls, value):
        return cls._normalize_scrape_timeout(value)

    @field_validator("SCRAPER_TIMEOUT_OVERRIDES", mode="before")
    def normalize_scraper_timeout_overrides(cls, value):
        if not isinstance(value, dict):
            raise ValueError("SCRAPER_TIMEOUT_OVERRIDES must be a JSON object")

        normalized = {}
        for selector, timeout in value.items():
            normalized_selector = normalize_scraper_timeout_selector(selector)
            if normalized_selector in normalized:
                raise ValueError(
                    "SCRAPER_TIMEOUT_OVERRIDES contains duplicate normalized "
                    f"selector {normalized_selector!r}"
                )
            normalized[normalized_selector] = cls._normalize_scrape_timeout(timeout)
        return normalized

    @field_validator(
        *_POSITIVE_WORK_COUNT_FIELDS,
        *_POSITIVE_COMETNET_OPERATION_FIELDS,
        mode="before",
    )
    def reject_boolean_operational_numbers(cls, value):
        if isinstance(value, bool):
            raise ValueError("operational numeric values cannot be booleans")
        return value

    @field_validator(*_POSITIVE_COMETNET_OPERATION_FIELDS)
    def validate_positive_cometnet_operation_value(cls, value):
        if value is None or not math.isfinite(value) or value <= 0:
            raise ValueError(
                "CometNet operational values must be finite and greater than zero"
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
        if value is None or not math.isfinite(value) or value < 0:
            raise ValueError("HTTP operational values must be finite and non-negative")
        return value

    @field_validator(*_POSITIVE_HTTP_OPERATION_FIELDS)
    def validate_positive_http_operation_value(cls, value):
        if value is None or not math.isfinite(value) or value <= 0:
            raise ValueError(
                "HTTP operational values must be finite and greater than zero"
            )
        return value

    @field_validator("RATELIMIT_MAX_RETRIES")
    def validate_rate_limit_retry_count(cls, value):
        if value > 20:
            raise ValueError("RATELIMIT_MAX_RETRIES cannot exceed 20")
        return value

    @field_validator("EXECUTOR_MAX_WORKERS", mode="before")
    def normalize_executor_workers(cls, v):
        if v is None or v == "" or str(v).lower() == "none":
            return 1
        return v

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
        if value is None or value < 60:
            raise ValueError("session TTLs must be integers of at least 60 seconds")
        return value

    @field_validator("ADMIN_DASHBOARD_PASSWORD")
    def validate_admin_dashboard_password(cls, value):
        if type(value) is not str or not value:
            raise ValueError("ADMIN_DASHBOARD_PASSWORD must be a non-empty string")
        return value

    @field_validator(
        "CONFIGURE_PAGE_PASSWORD",
        "PUBLIC_API_TOKEN",
        "PUBLIC_API_TOKEN_FILE",
        "PROMETHEUS_AUTH_TOKEN",
        "PROMETHEUS_AUTH_TOKEN_FILE",
        mode="before",
    )
    def normalize_optional_secrets(cls, v):
        if v is None:
            return None

        normalized = str(v).strip()
        if not normalized or normalized.lower() == "none":
            return None

        return normalized

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
        if type(value) is not str or not value.strip():
            raise ValueError("PROMETHEUS_MULTIPROC_DIR must be a non-empty path")
        return value.strip()

    @field_validator("INDEXER_MANAGER_TYPE")
    def set_indexer_manager_type(cls, v, values):
        if v is not None and v.lower() == "none":
            return None
        return v

    @field_validator("DATABASE_TYPE", mode="before")
    def normalize_database_type(cls, v):
        if v is None:
            return v

        value = str(v).strip().lower()
        if value in {"postgres", "postgresql", "postgresql+asyncpg", "pgsql", "psql"}:
            return "postgresql"
        if value in {"sqlite", "sqlite3"}:
            return "sqlite"
        return value

    @field_validator(
        "INDEXER_MANAGER_INDEXERS", "JACKETT_INDEXERS", "PROWLARR_INDEXERS"
    )
    def indexer_manager_indexers_normalization(cls, v, values):
        v = [indexer.replace(" ", "").lower() for indexer in v]
        return v

    @field_validator("INDEXER_LANGUAGES")
    def normalize_indexer_languages(cls, value):
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

    @field_validator(
        "INDEXER_MANAGER_URL",
        "STREMTHRU_URL",
        "STREMTHRU_SCRAPE_URL",
        "BITMAGNET_URL",
        "COMET_URL",
        "ZILEAN_URL",
        "TORRENTIO_URL",
        "MEDIAFUSION_URL",
        "AIOSTREAMS_URL",
        "JACKETTIO_URL",
        "JACKETT_URL",
        "PROWLARR_URL",
        "PUBLIC_BASE_URL",
    )
    def normalize_urls(cls, v):
        if isinstance(v, str):
            return v.rstrip("/")
        elif isinstance(v, list):
            return [url.rstrip("/") for url in v]
        return v

    def is_scraper_enabled(self, scraper_setting: bool | str, context: str):
        if isinstance(scraper_setting, bool):
            return scraper_setting

        if isinstance(scraper_setting, str):
            scraper_setting = scraper_setting.lower()
            if scraper_setting in ["true", "both"] or scraper_setting == context:
                return True

        return False

    def format_scraper_mode(self, scraper_setting: bool | str):
        if isinstance(scraper_setting, bool):
            return "both" if scraper_setting else "False"

        if isinstance(scraper_setting, str):
            scraper_setting = scraper_setting.lower()
            if scraper_setting in ["true", "both"]:
                return "both"
            elif scraper_setting in ["live", "background"]:
                return scraper_setting

        return "False"

    def is_any_context_enabled(self, scraper_setting: bool | str):
        if isinstance(scraper_setting, bool):
            return scraper_setting

        if isinstance(scraper_setting, str):
            scraper_setting = scraper_setting.lower()
            return scraper_setting in ["true", "both", "live", "background"]

    def model_post_init(self, __context, /):
        if self.INDEXER_MANAGER_TYPE == "jackett":
            if not self.SCRAPE_JACKETT:
                self.SCRAPE_JACKETT = self.INDEXER_MANAGER_MODE
            if self.JACKETT_URL == "http://127.0.0.1:9117" and self.INDEXER_MANAGER_URL:
                self.JACKETT_URL = self.INDEXER_MANAGER_URL
            if not self.JACKETT_API_KEY and self.INDEXER_MANAGER_API_KEY:
                self.JACKETT_API_KEY = self.INDEXER_MANAGER_API_KEY
            if not self.JACKETT_INDEXERS and self.INDEXER_MANAGER_INDEXERS:
                self.JACKETT_INDEXERS = self.INDEXER_MANAGER_INDEXERS
        elif self.INDEXER_MANAGER_TYPE == "prowlarr":
            if not self.SCRAPE_PROWLARR:
                self.SCRAPE_PROWLARR = self.INDEXER_MANAGER_MODE
            if (
                self.PROWLARR_URL == "http://127.0.0.1:9696"
                and self.INDEXER_MANAGER_URL
            ):
                self.PROWLARR_URL = self.INDEXER_MANAGER_URL
            if not self.PROWLARR_API_KEY and self.INDEXER_MANAGER_API_KEY:
                self.PROWLARR_API_KEY = self.INDEXER_MANAGER_API_KEY
            if not self.PROWLARR_INDEXERS and self.INDEXER_MANAGER_INDEXERS:
                self.PROWLARR_INDEXERS = self.INDEXER_MANAGER_INDEXERS


def _resolve_persisted_token(
    configured_token: str | None,
    token_file: str | None,
    token_name: str,
):
    if configured_token:
        return configured_token, "env"

    if token_file:
        try:
            token_dir = os.path.dirname(token_file)
            if token_dir:
                os.makedirs(token_dir, exist_ok=True)

            if os.path.exists(token_file):
                with open(token_file, "r", encoding="utf-8") as file:
                    existing_token = file.read().strip()
                    if existing_token:
                        return existing_token, "file"
        except FileNotFoundError:
            pass
        except Exception as error:
            logger.warning(f"Failed to read {token_name}_FILE ({token_file}): {error}")

    generated_token = secrets.token_urlsafe(32)
    if not token_file:
        return generated_token, "generated-memory"

    try:
        fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(generated_token)
        return generated_token, "generated-file"
    except FileExistsError:
        last_read_error: Exception | None = None
        for _ in range(5):
            try:
                with open(token_file, "r", encoding="utf-8") as file:
                    existing_token = file.read().strip()
                    if existing_token:
                        return existing_token, "file"
            except Exception as error:
                last_read_error = error
                logger.warning(
                    f"Failed to read existing {token_name}_FILE ({token_file}): {error}"
                )
            time.sleep(0.05)

        error_context = f"{token_name}_FILE ({token_file}) exists but remained empty or unreadable after retries"
        if last_read_error is not None:
            raise RuntimeError(
                f"{error_context}: {last_read_error}"
            ) from last_read_error
        raise RuntimeError(error_context) from None
    except Exception as error:
        logger.error(f"Failed to persist {token_name}_FILE ({token_file}): {error}")
        raise


def _build_stremio_api_prefix():
    should_protect_api = bool(settings.CONFIGURE_PAGE_PASSWORD) or bool(
        settings.PUBLIC_API_TOKEN
    )
    if not should_protect_api:
        settings.PUBLIC_API_TOKEN_SOURCE = "disabled"
        return ""

    settings.PUBLIC_API_TOKEN, settings.PUBLIC_API_TOKEN_SOURCE = (
        _resolve_persisted_token(
            settings.PUBLIC_API_TOKEN,
            settings.PUBLIC_API_TOKEN_FILE,
            "PUBLIC_API_TOKEN",
        )
    )
    return f"/s/{settings.PUBLIC_API_TOKEN}"


settings = AppSettings()
STREMIO_API_PREFIX = _build_stremio_api_prefix()
settings.STREMIO_API_PREFIX = STREMIO_API_PREFIX

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

    options: OptionsConfig = OptionsConfig(remove_ranks_under=-10000000000)

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
rtn_settings_default_dumped = rtn_settings_default.model_dump()
# {
#     "profile":"default",
#     "require":[

#     ],
#     "exclude":[

#     ],
#     "preferred":[

#     ],
#     "resolutions":{
#         "r2160p":true,
#         "r1080p":true,
#         "r720p":true,
#         "r480p":true,
#         "r360p":true,
#         "unknown":true
#     },
#     "options":{
#         "title_similarity":0.85,
#         "remove_all_trash":true,
#         "remove_ranks_under":-10000000000,
#         "remove_unknown_languages":false,
#         "allow_english_in_languages":false,
#         "enable_fetch_speed_mode":true,
#         "remove_adult_content":true
#     },
#     "languages":{
#         "required":[

#         ],
#         "exclude":[

#         ],
#         "preferred":[

#         ]
#     },
#     "custom_ranks":{
#         "quality":{
#             "av1":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "avc":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "bluray":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "dvd":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "hdtv":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "hevc":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "mpeg":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "remux":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "vhs":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "web":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "webdl":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "webmux":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "xvid":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             }
#         },
#         "rips":{
#             "bdrip":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "brrip":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "dvdrip":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "hdrip":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "ppvrip":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "satrip":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "tvrip":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "uhdrip":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "vhsrip":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "webdlrip":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "webrip":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             }
#         },
#         "hdr":{
#             "bit10":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "dolby_vision":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "hdr":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "hdr10plus":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "sdr":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             }
#         },
#         "audio":{
#             "aac":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "ac3":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "atmos":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "dolby_digital":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "dolby_digital_plus":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "dts_lossy":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "dts_lossless":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "eac3":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "flac":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "mono":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "mp3":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "stereo":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "surround":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "truehd":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             }
#         },
#         "extras":{
#             "three_d":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "converted":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "documentary":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "dubbed":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "edition":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "hardcoded":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "network":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "proper":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "repack":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "retail":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "site":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "subbed":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "upscaled":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "scene":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             }
#         },
#         "trash":{
#             "cam":{
#                 "fetch":false,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "clean_audio":{
#                 "fetch":false,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "pdtv":{
#                 "fetch":false,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "r5":{
#                 "fetch":false,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "screener":{
#                 "fetch":false,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "size":{
#                 "fetch":false,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "telecine":{
#                 "fetch":false,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "telesync":{
#                 "fetch":false,
#                 "use_custom_rank":false,
#                 "rank":0
#             }
#         }
#     }
# }
rtn_ranking_default = DefaultRanking()


VALID_DEBRID_SERVICES = [
    "realdebrid",
    "alldebrid",
    "premiumize",
    "torbox",
    "debrider",
    "easydebrid",
    "debridlink",
    "offcloud",
    "pikpak",
]


class DebridServiceEntry(BaseModel):
    service: str
    apiKey: str = ""

    @field_validator("service")
    def check_service(cls, v):
        if v not in VALID_DEBRID_SERVICES:
            raise ValueError(f"Invalid debrid service: {v}")
        return v


class ConfigModel(BaseModel):
    cachedOnly: bool | None = False
    sortCachedUncachedTogether: bool | None = False
    removeTrash: bool | None = True
    resultFormat: list[str] | None = ["all"]
    maxResultsPerResolution: int | None = 0
    maxSize: float | None = 0

    # Legacy single-service fields
    debridService: str | None = "torrent"
    debridApiKey: str | None = ""

    # Multi-Debrid fields
    debridServices: list[DebridServiceEntry] | None = []
    enableTorrent: bool | None = False
    deduplicateStreams: bool | None = False
    scrapeDebridAccountTorrents: bool | None = False

    debridStreamProxyPassword: str | None = ""
    languages: dict | None = rtn_settings_default_dumped["languages"]
    resolutions: dict | None = rtn_settings_default_dumped["resolutions"]
    options: dict | None = rtn_settings_default_dumped["options"]
    rtnSettings: CometSettingsModel | None = rtn_settings_default
    rtnRanking: DefaultRanking | None = rtn_ranking_default

    @field_validator("maxResultsPerResolution")
    def check_max_results_per_resolution(cls, v):
        if not isinstance(v, int):
            v = 0

        v = max(v, 0)
        return v

    @field_validator("maxSize")
    def check_max_size(cls, v):
        if not isinstance(v, float):
            v = 0

        v = max(v, 0)
        return v

    @field_validator("debridService")
    def check_debrid_service(cls, v):
        if v not in VALID_DEBRID_SERVICES + ["torrent"]:
            raise ValueError("Invalid debridService")
        return v

    @field_validator("debridServices", mode="before")
    def validate_debrid_services(cls, v):
        if v is None:
            return []
        if isinstance(v, list):
            return [
                DebridServiceEntry(**entry) if isinstance(entry, dict) else entry
                for entry in v
            ]
        return v


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


def _build_database_instance(raw_url: str):
    if IS_SQLITE:
        return Database(f"sqlite:///{raw_url}")

    for scheme in ["postgresql://", "postgres://"]:
        if raw_url.startswith(scheme):
            raw_url = raw_url[len(scheme) :]
            break

    return Database(f"postgresql+asyncpg://{raw_url}")


replica_instances: list[Database] = []
force_ipv4 = False

if IS_SQLITE:
    database_url = settings.DATABASE_PATH
    if settings.DATABASE_READ_REPLICA_URLS:
        logger.log("DATABASE", "Read replicas are ignored for sqlite deployments")
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
