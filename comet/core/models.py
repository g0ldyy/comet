import os
import re
import secrets
import stat
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from urllib.parse import urlsplit

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

from comet.core.db_router import ReplicaAwareDatabase
from comet.core.operator_settings import (
    LiveSettings,
    effective_model_inputs,
    effective_settings_payload,
    validate_effective_setting_keys,
)
from comet.core.scrape import ScraperMode
from comet.core.server_settings import ServerSettings
from comet.core.sources import USENET_PLAYBACK_PROVIDER_KINDS
from comet.usenet.nntp_config import parse_instance_servers, parse_personal_servers
from comet.usenet.stremio_nntp_config import validate_handoff_config

_comet_fk_enabled = False
_SQLITE_BUSY_TIMEOUT_MS = 30000
_MAX_PERSISTED_TOKEN_BYTES = 4_096
_PUBLIC_API_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,256}")
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
    candidate = value if "://" in value else f"postgresql://{value}"
    return make_url(candidate)


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
    INDEXER_MANAGER_MODE: ScraperMode = "both"
    INDEXER_MANAGER_TIMEOUT: int = 30
    INDEXER_MANAGER_INDEXERS: list[str] = Field(default_factory=list)
    INDEXER_MANAGER_UPDATE_INTERVAL: int = 900
    INDEXER_MANAGER_WAIT_TIMEOUT: int = 30
    INDEXER_INCLUDE_CANONICAL_TITLE: bool = True
    INDEXER_INCLUDE_ORIGINAL_TITLE: bool = True
    INDEXER_LANGUAGES: list[str] = Field(default_factory=list)
    SCRAPE_JACKETT: ScraperMode = False
    JACKETT_URL: str | None = "http://127.0.0.1:9117"
    JACKETT_API_KEY: str | None = None
    JACKETT_INDEXERS: list[str] = Field(default_factory=list)
    SCRAPE_PROWLARR: ScraperMode = False
    PROWLARR_URL: str | None = "http://127.0.0.1:9696"
    PROWLARR_API_KEY: str | None = None
    PROWLARR_INDEXERS: list[str] = Field(default_factory=list)
    GET_TORRENT_TIMEOUT: int = 5
    MAGNET_RESOLVE_TIMEOUT: int = 60
    CATALOG_TIMEOUT: int = 30
    DOWNLOAD_TORRENT_FILES: bool = False
    SCRAPE_COMET: ScraperMode = False
    COMET_URL: str | list[str] = "https://comet.feels.legal"
    COMET_CLEAN_TRACKER: bool = False
    SCRAPE_NYAA: ScraperMode = False
    NYAA_ANIME_ONLY: bool = True
    NYAA_MAX_CONCURRENT_PAGES: int = 5
    SCRAPE_ANIMETOSHO: ScraperMode = False
    SCRAPE_ANIMETOSHO_USENET: bool = False
    ANIMETOSHO_ANIME_ONLY: bool = True
    ANIMETOSHO_MAX_CONCURRENT_PAGES: int = 10
    SCRAPE_SEADEX: ScraperMode = False
    SEADEX_ANIME_ONLY: bool = True
    SCRAPE_NEKOBT: ScraperMode = False
    NEKOBT_ANIME_ONLY: bool = True
    SCRAPE_ZILEAN: ScraperMode = False
    ZILEAN_URL: str | list[str] = "https://zileanfortheweebs.midnightignite.me"
    SCRAPE_STREMTHRU: ScraperMode = False
    STREMTHRU_SCRAPE_URL: str | list[str] = "https://stremthru.13377001.xyz"
    SCRAPE_DMM: ScraperMode = False
    DMM_INGEST_ENABLED: bool = False
    DMM_INGEST_INTERVAL: int = 86400
    DMM_INGEST_CONCURRENT_WORKERS: int = 4
    DMM_INGEST_BATCH_SIZE: int = 100
    SCRAPE_BITMAGNET: ScraperMode = False
    BITMAGNET_URL: str | list[str] = "https://bitmagnetfortheweebs.midnightignite.me"
    BITMAGNET_MAX_CONCURRENT_PAGES: int = 5
    BITMAGNET_MAX_OFFSET: int = 15000
    SCRAPE_TORRENTIO: ScraperMode = False
    TORRENTIO_URL: str | list[str] = "https://torrentio.strem.fun"
    SCRAPE_MEDIAFUSION: ScraperMode = False
    MEDIAFUSION_URL: str | list[str] = "https://mediafusion.elfhosted.com"
    MEDIAFUSION_API_PASSWORD: str | list[str] | None = None
    MEDIAFUSION_LIVE_SEARCH: bool = True
    SCRAPE_AIOSTREAMS: ScraperMode = False
    AIOSTREAMS_URL: str | list[str] | None = None
    AIOSTREAMS_USER_UUID_AND_PASSWORD: str | list[str] | None = None
    SCRAPE_JACKETTIO: ScraperMode = False
    JACKETTIO_URL: str | list[str] | None = None
    SCRAPE_DEBRIDIO: ScraperMode = False
    DEBRIDIO_API_KEY: str | None = None
    DEBRIDIO_PROVIDER: str | None = None
    DEBRIDIO_PROVIDER_KEY: str | None = None
    SCRAPE_TORRENTSDB: ScraperMode = False
    SCRAPE_PEERFLIX: ScraperMode = False
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

    @field_validator(
        "COMET_USENET_ENGINE_BINARY",
        "USENET_RUNTIME_DIR",
        "USENET_LOCAL_DATA_DIR",
        "USENET_ARTIFACT_DIR",
    )
    def validate_usenet_directory(cls, value):
        return _bounded_path(value, field="Usenet directory")

    @field_validator("EXECUTOR_MAX_WORKERS", mode="before")
    def normalize_executor_workers(cls, value):
        if value is None or value == "" or str(value).lower() == "none":
            return 1
        return value

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
    def normalize_optional_secrets(cls, value):
        if value is None:
            return None
        normalized = str(value).strip()
        return None if not normalized or normalized.lower() == "none" else normalized

    @field_validator("PUBLIC_API_TOKEN")
    def validate_public_api_token(cls, value):
        if value is not None and _PUBLIC_API_TOKEN_PATTERN.fullmatch(value) is None:
            raise ValueError("PUBLIC_API_TOKEN must be URL-safe ASCII")
        return value

    @field_validator(
        "PUBLIC_API_TOKEN_FILE",
        "PROMETHEUS_AUTH_TOKEN_FILE",
        "COMET_CAPABILITY_SECRET_FILE",
    )
    def validate_secret_file_path(cls, value):
        return None if value is None else _bounded_path(value, field="secret file path")

    @field_validator("INDEXER_MANAGER_TYPE")
    def set_indexer_manager_type(cls, value):
        if value is not None and value.lower() == "none":
            return None
        return value

    @field_validator("DATABASE_TYPE", mode="before")
    def normalize_database_type(cls, value):
        if value is None:
            return value
        normalized = str(value).strip().lower()
        if normalized in {
            "postgres",
            "postgresql",
            "postgresql+asyncpg",
            "pgsql",
            "psql",
        }:
            return "postgresql"
        if normalized in {"sqlite", "sqlite3"}:
            return "sqlite"
        return normalized

    @field_validator("DATABASE_URL", "DATABASE_PATH", mode="before")
    def normalize_optional_database_location(cls, value):
        return None if value is None or value == "" else value

    @field_validator(
        "INDEXER_MANAGER_INDEXERS", "JACKETT_INDEXERS", "PROWLARR_INDEXERS"
    )
    def normalize_indexer_names(cls, value):
        return [normalize_indexer_name(indexer) for indexer in value]

    @field_validator("INDEXER_LANGUAGES")
    def normalize_indexer_languages(cls, value):
        return list(dict.fromkeys(language.strip().lower() for language in value))

    @field_validator(
        *_SCRAPER_URL_FIELDS,
        "STREMTHRU_URL",
        "PUBLIC_BASE_URL",
        "PROMETHEUS_QUERY_URL",
        "USENET_EXPORT_BASE_URL",
        "TORRENT_DISABLED_STREAM_URL",
    )
    def normalize_urls(cls, value):
        if isinstance(value, str):
            return value.rstrip("/")
        if isinstance(value, list):
            return [url.rstrip("/") for url in value]
        return value

    @field_validator(
        "COMETNET_ADVERTISE_URL",
        "COMETNET_RELAY_URL",
        "COMETNET_NETWORK_ID",
        "COMETNET_NODE_ALIAS",
        mode="before",
    )
    def normalize_optional_cometnet_text(cls, value):
        return None if value is None or value == "" else value

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


USER_USENET_DISCOVERY_SOURCE_KINDS = (
    "newznab",
    "nzbhydra2",
    "prowlarr_usenet",
    "easynews",
    "stremio_addon",
)
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

    @model_validator(mode="after")
    def validate_usenet_provider_options(self):
        if not self.enabled or self.kind not in USENET_PLAYBACK_PROVIDER_KINDS:
            return self
        try:
            if str(uuid.UUID(self.configurationId)) != self.configurationId:
                raise ValueError
        except ValueError as exc:
            raise ValueError(
                "provider configurationId must be a canonical UUID"
            ) from exc
        _validate_config_display_name(self.displayName, "provider displayName")
        if self.accountId is not None:
            _validate_config_account_id(self.accountId, "provider accountId")
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

    @field_validator("schemaVersion", mode="before")
    def validate_schema_version(cls, value):
        if type(value) is not int or value not in {1, 2}:
            raise ValueError("unsupported configuration schema version")
        return value

    @field_validator("maxResultsPerResolution")
    def check_max_results_per_resolution(cls, v):
        return max(v, 0)

    @field_validator("maxSize")
    def check_max_size(cls, v):
        return max(float(v), 0)

    @field_validator("debridService")
    def check_debrid_service(cls, v):
        if v not in (*VALID_DEBRID_SERVICES, "torrent"):
            raise ValueError("Invalid debridService")
        return v

    @field_validator("debridServices", mode="before")
    def validate_debrid_services(cls, v):
        if v is None:
            return []
        return v

    @model_validator(mode="after")
    def validate_v2_configuration(self):
        if self.schemaVersion == 1:
            return self

        providers = [
            provider
            for provider in self.playbackProviders or ()
            if provider.enabled and provider.kind in USENET_PLAYBACK_PROVIDER_KINDS
        ]
        sources = [source for source in self.discoverySources or () if source.enabled]
        accounts = self.accounts or {}

        entries = (
            *(
                (provider, _PROVIDER_ACCOUNT_KINDS.get(provider.kind))
                for provider in providers
            ),
            *((source, _SOURCE_ACCOUNT_KINDS.get(source.kind)) for source in sources),
        )
        for entry, expected_kind in entries:
            if entry.accountId is None:
                continue
            account = accounts.get(entry.accountId)
            if account is None:
                raise ValueError("Usenet account reference is unavailable")
            _validate_config_account_id(entry.accountId, "Usenet account ID")
            if account.get("kind") != expected_kind:
                raise ValueError("Usenet account kind does not match its binding")

        for provider in providers:
            if provider.kind != "stremio_nntp" or provider.accountId is None:
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

        if self.nativeAccessToken is not None and not _is_bounded_opaque_credential(
            self.nativeAccessToken, 256
        ):
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
