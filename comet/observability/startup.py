"""Readable startup configuration and one-time generated-secret delivery."""

from __future__ import annotations

from urllib.parse import urlsplit

import orjson

from comet.core.operator_settings import (
    deployment_setting_keys,
    effective_settings_payload,
)
from comet.core.settings_catalog import build_settings_catalog
from comet.observability.logging import current_settings, log

_SCRAPERS = (
    ("Jackett", "SCRAPE_JACKETT", "JACKETT_PROXY_URL"),
    ("Prowlarr", "SCRAPE_PROWLARR", "PROWLARR_PROXY_URL"),
    ("Comet", "SCRAPE_COMET", "COMET_PROXY_URL"),
    ("Nyaa", "SCRAPE_NYAA", "NYAA_PROXY_URL"),
    ("AnimeTosho", "SCRAPE_ANIMETOSHO", "ANIMETOSHO_PROXY_URL"),
    ("SeaDex", "SCRAPE_SEADEX", "SEADEX_PROXY_URL"),
    ("NekoBT", "SCRAPE_NEKOBT", "NEKOBT_PROXY_URL"),
    ("Zilean", "SCRAPE_ZILEAN", "ZILEAN_PROXY_URL"),
    ("StremThru", "SCRAPE_STREMTHRU", "STREMTHRU_PROXY_URL"),
    ("DMM", "SCRAPE_DMM", "DMM_PROXY_URL"),
    ("Bitmagnet", "SCRAPE_BITMAGNET", "BITMAGNET_PROXY_URL"),
    ("Torrentio", "SCRAPE_TORRENTIO", "TORRENTIO_PROXY_URL"),
    ("MediaFusion", "SCRAPE_MEDIAFUSION", "MEDIAFUSION_PROXY_URL"),
    ("AIOStreams", "SCRAPE_AIOSTREAMS", "AIOSTREAMS_PROXY_URL"),
    ("Jackettio", "SCRAPE_JACKETTIO", "JACKETTIO_PROXY_URL"),
    ("Debridio", "SCRAPE_DEBRIDIO", "DEBRIDIO_PROXY_URL"),
    ("TorrentsDB", "SCRAPE_TORRENTSDB", "TORRENTSDB_PROXY_URL"),
    ("Peerflix", "SCRAPE_PEERFLIX", "PEERFLIX_PROXY_URL"),
)


def _endpoint(value: str | None) -> str:
    if not value:
        return "disabled"
    candidate = value if "://" in value else f"//{value}"
    parsed = urlsplit(candidate)
    host = parsed.hostname
    port = parsed.port
    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        authority = f"{authority}:{port}"
    return f"{parsed.scheme}://{authority}" if parsed.scheme else authority


def _configured(value: str | None, *, explicit: bool = True) -> str:
    if not value:
        return "disabled"
    return "configured" if explicit else "generated"


def _setting_details(value: object, *, sensitive: bool) -> str:
    if sensitive:
        return "configured"
    rendered = orjson.dumps(value).decode("utf-8")
    encoded = rendered.encode("utf-8")
    if len(encoded) <= 1_024:
        return rendered
    return encoded[:1_021].decode("utf-8", errors="ignore") + "..."


def _log_explicit_settings(settings) -> None:
    """Log every operator override without duplicating the settings catalog."""

    payload = effective_settings_payload()
    dashboard_keys = payload.dashboard_keys
    generated_keys = payload.generated_keys
    logging_settings = current_settings()
    explicit_keys = deployment_setting_keys()

    for entry in build_settings_catalog():
        if entry.key in dashboard_keys:
            source = "dashboard"
        elif entry.key in explicit_keys and entry.key not in generated_keys:
            source = "environment"
        else:
            continue
        value = (
            getattr(settings, entry.key)
            if entry.key in settings.__class__.model_fields
            else getattr(logging_settings, entry.key)
        )
        log.info(
            "config.override",
            "Operator setting override",
            setting_name=entry.key,
            source_type=source,
            details=_setting_details(value, sensitive=entry.sensitive),
        )


def _log_generated_secret(
    field: str,
    value: str,
    source: str,
    *,
    useful: bool = True,
) -> None:
    if useful and source in ("generated_memory", "generated_shared") and value:
        log.warning(
            "config.generated_secret",
            "Generated operator secret",
            setting_name=field,
            generated_secret=value,
        )


def log_runtime_starting(settings) -> None:
    """Emit the single process-tree startup event."""

    logging_settings = current_settings()
    fields = {
        "log_profile": logging_settings.LOG_PROFILE.value,
        "log_format": logging_settings.LOG_FORMAT.value,
    }
    if revision := settings.COMET_COMMIT_HASH:
        fields["build_revision"] = revision
    log.info(
        "runtime.starting",
        "Comet runtime is starting",
        **fields,
    )


def log_startup_configuration(settings, *, workers: int, server_name: str) -> None:
    """Emit a compact summary of the effective operator configuration."""

    setting_values = settings.__dict__
    log.info(
        "config.identity",
        "Add-on identity and access configuration",
        details=(
            f"addon_id={settings.ADDON_ID} addon_name={settings.ADDON_NAME} "
            f"admin_password={_configured(settings.ADMIN_DASHBOARD_PASSWORD, explicit=settings.admin_dashboard_password_source == 'configured')} "
            f"configure_password={_configured(settings.CONFIGURE_PAGE_PASSWORD)} "
            f"public_api={'enabled' if settings.STREMIO_API_PREFIX else 'disabled'} "
            f"public_api_token_source={settings.PUBLIC_API_TOKEN_SOURCE} "
        ),
    )
    log.info(
        "config.server",
        "Server configuration",
        details=(
            f"server={server_name} bind={settings.FASTAPI_HOST}:{settings.FASTAPI_PORT} "
            f"workers={workers} preload={bool(settings.GUNICORN_PRELOAD_APP)} "
            f"executor_workers={settings.EXECUTOR_MAX_WORKERS} "
            f"public_url={_endpoint(settings.PUBLIC_BASE_URL)}"
        ),
    )
    database_target = (
        settings.DATABASE_PATH
        if settings.DATABASE_TYPE == "sqlite"
        else _endpoint(settings.DATABASE_URL)
    )
    log.info(
        "config.database",
        "Database and cache configuration",
        details=(
            f"backend={settings.DATABASE_TYPE} target={database_target} "
            f"batch={settings.DATABASE_BATCH_SIZE} metadata_ttl={settings.METADATA_CACHE_TTL}s "
            f"torrent_ttl={settings.TORRENT_CACHE_TTL}s "
            f"live_torrent_ttl={settings.LIVE_TORRENT_CACHE_TTL}s "
            f"debrid_ttl={settings.DEBRID_CACHE_TTL}s "
            f"debrid_refresh_ratio={settings.DEBRID_CACHE_CHECK_RATIO}"
        ),
    )
    scraper_modes = ", ".join(
        f"{label}={settings.format_scraper_mode(setting_values[field])}"
        for label, field, _proxy_field in _SCRAPERS
        if setting_values[field] is not False
    )
    log.info(
        "config.scrapers",
        "Scraper configuration",
        details=(
            f"enabled={scraper_modes or 'none'}; "
            f"live_timeout={settings.LIVE_SCRAPE_TIMEOUT:g}s "
            f"background_timeout={settings.BACKGROUND_SCRAPE_TIMEOUT:g}s "
            f"lock_ttl={settings.SCRAPE_LOCK_TTL}s"
        ),
    )
    debrid_proxy = (
        "stream_proxy=enabled "
        f"proxy_password={_configured(settings.PROXY_DEBRID_STREAM_PASSWORD, explicit=settings.proxy_debrid_stream_password_source == 'configured')} "
        f"max_connections={settings.PROXY_DEBRID_STREAM_MAX_CONNECTIONS}"
        if settings.PROXY_DEBRID_STREAM
        else "stream_proxy=disabled"
    )
    log.info(
        "config.debrid",
        "Debrid configuration",
        details=(
            f"{debrid_proxy} "
            f"account_refresh={settings.DEBRID_ACCOUNT_SCRAPE_REFRESH_INTERVAL}s "
            f"account_cache_ttl={settings.DEBRID_ACCOUNT_SCRAPE_CACHE_TTL}s "
            f"stremthru={_endpoint(settings.STREMTHRU_URL)}"
        ),
    )
    configured_scraper_proxies = sum(
        bool(setting_values[proxy_field])
        for _label, _scraper_field, proxy_field in _SCRAPERS
    )
    log.info(
        "config.network",
        "Outbound network configuration",
        details=(
            f"global_proxy={_configured(settings.GLOBAL_PROXY_URL)} "
            f"user_provided_proxy={_configured(settings.USER_PROVIDED_PROXY_URL)} "
            f"scraper_proxies={configured_scraper_proxies} "
            f"proxy_ethos={settings.PROXY_ETHOS} "
            f"http_limit={settings.HTTP_CLIENT_LIMIT} "
            f"http_limit_per_host={settings.HTTP_CLIENT_LIMIT_PER_HOST} "
            f"http_timeout={settings.HTTP_CLIENT_TIMEOUT_TOTAL:g}s "
            f"rate_limit_retries={settings.RATELIMIT_MAX_RETRIES}"
        ),
    )
    usenet_details = "enabled=False"
    if settings.USENET_ENABLED:
        usenet_details = (
            f"enabled=True native_engine={bool(settings.USENET_ENGINE_ENABLED)} "
            f"required={bool(settings.USENET_ENGINE_REQUIRED)} "
            f"native_access_token={_configured(settings.USENET_NATIVE_ACCESS_TOKEN, explicit=settings.usenet_native_access_token_source == 'configured')} "
            f"replicas={settings.USENET_REPLICA_COUNT} "
            f"max_streams={settings.USENET_NATIVE_MAX_STREAMS} "
            f"user_servers={bool(settings.USENET_NATIVE_ALLOW_USER_SERVERS)} "
            f"memory_cache={settings.USENET_MEMORY_CACHE_BYTES} "
            f"disk_cache={settings.USENET_DISK_CACHE_BYTES} "
            f"archive_jobs={settings.USENET_ARCHIVE_JOBS} "
            f"repair_jobs={settings.USENET_REPAIR_JOBS} "
            f"degraded_playback={bool(settings.USENET_DEGRADED_PLAYBACK_ENABLED)}"
        )
    log.info("config.usenet", "Usenet configuration", details=usenet_details)
    cometnet_mode = (
        f"relay {_endpoint(settings.COMETNET_RELAY_URL)}"
        if settings.COMETNET_RELAY_URL
        else ("integrated" if settings.COMETNET_ENABLED else "disabled")
    )
    enabled_features = ", ".join(
        label
        for label, enabled in (
            ("rtn_filter_debug", settings.RTN_FILTER_DEBUG),
            ("smart_language_detection", settings.SMART_LANGUAGE_DETECTION),
            ("digital_release_filter", settings.DIGITAL_RELEASE_FILTER),
            ("remove_adult_content", settings.REMOVE_ADULT_CONTENT),
            ("http_cache", settings.HTTP_CACHE_ENABLED),
            ("prometheus", settings.PROMETHEUS_ENABLED),
        )
        if enabled
    )
    log.info(
        "config.features",
        "Feature configuration",
        details=f"enabled={enabled_features or 'none'}",
    )
    if settings.BACKGROUND_SCRAPER_ENABLED:
        log.info(
            "config.background",
            "Background scraper configuration",
            details=(
                f"workers={settings.BACKGROUND_SCRAPER_CONCURRENT_WORKERS} "
                f"interval={settings.BACKGROUND_SCRAPER_INTERVAL}s "
                f"movies_per_run={settings.BACKGROUND_SCRAPER_MAX_MOVIES_PER_RUN} "
                f"series_per_run={settings.BACKGROUND_SCRAPER_MAX_SERIES_PER_RUN} "
                f"time_budget={settings.BACKGROUND_SCRAPER_RUN_TIME_BUDGET}s "
                f"queue={settings.BACKGROUND_SCRAPER_QUEUE_LOW_WATERMARK}/"
                f"{settings.BACKGROUND_SCRAPER_QUEUE_HIGH_WATERMARK}/"
                f"{settings.BACKGROUND_SCRAPER_QUEUE_HARD_CAP}"
            ),
        )
    if settings.COMETNET_ENABLED or settings.COMETNET_RELAY_URL:
        log.info(
            "config.cometnet",
            "CometNet configuration",
            details=(
                f"mode={cometnet_mode} listen_port={settings.COMETNET_LISTEN_PORT} "
                f"http_port={settings.COMETNET_HTTP_PORT} "
                f"bootstrap_nodes={len(settings.COMETNET_BOOTSTRAP_NODES)} "
                f"manual_peers={len(settings.COMETNET_MANUAL_PEERS)} "
                f"peers={settings.COMETNET_MIN_PEERS}-{settings.COMETNET_MAX_PEERS} "
                f"contribution={settings.COMETNET_CONTRIBUTION_MODE} "
                f"private_network={bool(settings.COMETNET_PRIVATE_NETWORK)}"
            ),
        )

    _log_explicit_settings(settings)

    _log_generated_secret(
        "ADMIN_DASHBOARD_PASSWORD",
        settings.ADMIN_DASHBOARD_PASSWORD,
        settings.admin_dashboard_password_source,
    )
    _log_generated_secret(
        "PROXY_DEBRID_STREAM_PASSWORD",
        settings.PROXY_DEBRID_STREAM_PASSWORD,
        settings.proxy_debrid_stream_password_source,
        useful=bool(settings.PROXY_DEBRID_STREAM),
    )
    _log_generated_secret(
        "COMETNET_API_KEY",
        settings.COMETNET_API_KEY,
        settings.cometnet_api_key_source,
        useful=bool(settings.COMETNET_RELAY_URL),
    )
    _log_generated_secret(
        "USENET_NATIVE_ACCESS_TOKEN",
        settings.USENET_NATIVE_ACCESS_TOKEN,
        settings.usenet_native_access_token_source,
        useful=bool(settings.USENET_ENABLED),
    )
    _log_generated_secret(
        "PUBLIC_API_TOKEN",
        settings.PUBLIC_API_TOKEN,
        settings.PUBLIC_API_TOKEN_SOURCE,
    )
    _log_generated_secret(
        "COMET_CAPABILITY_SECRET",
        settings.COMET_CAPABILITY_SECRET,
        settings.COMET_CAPABILITY_SECRET_SOURCE,
    )


def log_cometnet_standalone_configuration(settings) -> None:
    """Emit the settings and generated API key for the standalone relay."""

    log.info(
        "config.cometnet",
        "Standalone CometNet configuration",
        details=(
            f"mode=standalone listen_port={settings.COMETNET_LISTEN_PORT} "
            f"http_port={settings.COMETNET_HTTP_PORT} "
            f"bootstrap_nodes={len(settings.COMETNET_BOOTSTRAP_NODES)} "
            f"manual_peers={len(settings.COMETNET_MANUAL_PEERS)} "
            f"peers={settings.COMETNET_MIN_PEERS}-{settings.COMETNET_MAX_PEERS} "
            f"contribution={settings.COMETNET_CONTRIBUTION_MODE} "
            f"private_network={bool(settings.COMETNET_PRIVATE_NETWORK)}"
        ),
    )
    _log_generated_secret(
        "COMETNET_API_KEY",
        settings.COMETNET_API_KEY,
        settings.cometnet_api_key_source,
    )
