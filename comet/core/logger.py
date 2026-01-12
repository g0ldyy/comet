import logging
import re
import sys
import time

from loguru import logger

from comet.core.execution import max_workers
from comet.core.log_levels import (CUSTOM_LOG_LEVELS, STANDARD_LOG_LEVELS,
                                   get_level_info)
from comet.utils.parsing import associate_urls_credentials

logging.getLogger("demagnetize").setLevel(
    logging.CRITICAL
)  # disable demagnetize logging


def setupLogger(level: str):
    # Configure custom log levels
    for level_name, level_config in CUSTOM_LOG_LEVELS.items():
        logger.level(
            level_name,
            no=level_config["no"],
            icon=level_config["icon"],
            color=level_config["loguru_color"],
        )

    # Configure standard log levels (override defaults)
    for level_name, level_config in STANDARD_LOG_LEVELS.items():
        logger.level(
            level_name, icon=level_config["icon"], color=level_config["loguru_color"]
        )

    log_format = (
        "<white>{time:YYYY-MM-DD}</white> <magenta>{time:HH:mm:ss}</magenta> | "
        "<level>{level.icon}</level> <level>{level}</level> | "
        "<cyan>{module}</cyan>.<cyan>{function}</cyan> - <level>{message}</level>"
    )

    logger.configure(
        handlers=[
            {
                "sink": sys.stderr,
                "level": level,
                "format": log_format,
                "backtrace": False,
                "diagnose": False,
                "enqueue": True,
            }
        ]
    )


setupLogger("DEBUG")


class LogCapture:
    def __init__(self):
        self.logs = []
        self.max_logs = 1000

    def add_log(self, record):
        # Format the log record similar to loguru format
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created))
        level_name = record.levelname

        # Handle special loguru levels
        if hasattr(record, "extra") and "level_name" in record.extra:
            level_name = record.extra["level_name"]

        level_info = get_level_info(level_name)

        log_entry = {
            "timestamp": timestamp,
            "level": level_name,
            "icon": level_info["icon"],
            "color": level_info["color"],
            "module": getattr(record, "module", "unknown"),
            "function": getattr(record, "funcName", "unknown"),
            "message": record.getMessage(),
            "created": record.created,
        }

        self.logs.append(log_entry)
        if len(self.logs) > self.max_logs:
            self.logs.pop(0)

    def get_logs(self):
        return self.logs


# Global log capture instance
log_capture = LogCapture()


# Loguru handler to capture logs
class LoguruHandler:
    def __init__(self, log_capture):
        self.log_capture = log_capture

    def write(self, message):
        if message.strip():
            # Try to extract timestamp, level, module, function, and message
            # This is a simplified parser for loguru format
            pattern = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| (.*?) ?(\w+) \| (\w+)\.(\w+) - (.+)"
            match = re.match(pattern, message.strip())

            if match:
                timestamp_str, icon, level, module, function, msg = match.groups()

                level_info = get_level_info(level)

                log_entry = {
                    "timestamp": timestamp_str,
                    "level": level,
                    "icon": icon or level_info["icon"],
                    "color": level_info["color"],
                    "module": module,
                    "function": function,
                    "message": msg,
                    "created": time.time(),
                }

                self.log_capture.add_log_entry(log_entry)


# Add method to log capture to handle parsed entries
def add_log_entry_to_capture(self, log_entry):
    self.logs.append(log_entry)
    if len(self.logs) > self.max_logs:
        self.logs.pop(0)


# Monkey patch the method
log_capture.add_log_entry = add_log_entry_to_capture.__get__(log_capture, LogCapture)

# Set up loguru handler
loguru_handler = LoguruHandler(log_capture)

# Add our handler to loguru
logger.add(
    loguru_handler.write,
    level="DEBUG",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level.icon} {level} | {module}.{function} - {message}",
)


def log_scraper_error(
    scraper_name: str, scraper_url: str, media_id: str, error: Exception
):
    api_password_missing = ""
    if "MediaFusion" in scraper_name:
        api_password_missing = " or your API password could be wrong"

    logger.warning(
        f"Exception while getting torrents for {media_id} with {scraper_name} ({scraper_url}), you are most likely being ratelimited{api_password_missing}: {error}"
    )


def log_startup_info(settings):
    def get_urls_with_passwords(urls, passwords):
        url_credentials_pairs = associate_urls_credentials(urls, passwords)

        result = []
        for url, password in url_credentials_pairs:
            if password:
                result.append(f"{url}|{password}")
            else:
                result.append(url)

        return result

    logger.log(
        "COMET",
        f"Server started on http://{settings.FASTAPI_HOST}:{settings.FASTAPI_PORT} - {settings.FASTAPI_WORKERS} workers",
    )
    logger.log("COMET", f"Gunicorn Preload App: {settings.GUNICORN_PRELOAD_APP}")

    logger.log(
        "COMET",
        f"ProcessPoolExecutor: {max_workers} workers",
    )

    if settings.PUBLIC_BASE_URL:
        logger.log("COMET", f"Public Base URL: {settings.PUBLIC_BASE_URL}")

    logger.log(
        "COMET",
        f"Admin Dashboard Password: {settings.ADMIN_DASHBOARD_PASSWORD} -  http://{settings.FASTAPI_HOST}:{settings.FASTAPI_PORT}/admin - Public Metrics API: {settings.PUBLIC_METRICS_API}",
    )

    replicas = ""
    if settings.DATABASE_TYPE != "sqlite":
        replicas = f" - Read Replicas: {settings.DATABASE_READ_REPLICA_URLS}"
    logger.log(
        "COMET",
        f"Database ({settings.DATABASE_TYPE}): {settings.DATABASE_PATH if settings.DATABASE_TYPE == 'sqlite' else settings.DATABASE_URL} - Batch Size: {settings.DATABASE_BATCH_SIZE} - TTL: metadata={settings.METADATA_CACHE_TTL}s, torrents={settings.TORRENT_CACHE_TTL}s, live_torrents={settings.LIVE_TORRENT_CACHE_TTL}s, debrid={settings.DEBRID_CACHE_TTL}s, metrics={settings.METRICS_CACHE_TTL}s - Debrid Ratio: {settings.DEBRID_CACHE_CHECK_RATIO} - Startup Cleanup Interval: {settings.DATABASE_STARTUP_CLEANUP_INTERVAL}s{replicas}",
    )

    # SQLite concurrency warnings
    if settings.DATABASE_TYPE == "sqlite":
        logger.warning(
            "⚠️  SQLite has poor concurrency support and is NOT recommended for production. "
            "Consider using PostgreSQL for better performance and reliability."
        )
        if settings.FASTAPI_WORKERS != 1:
            logger.warning(
                f"⚠️  SQLite with {settings.FASTAPI_WORKERS} workers may cause database locking issues. "
                "Use PostgreSQL or set FASTAPI_WORKERS=1."
            )
        if settings.BACKGROUND_SCRAPER_ENABLED:
            logger.warning(
                "⚠️  Background scraper with SQLite may cause database locking issues. "
                "Use PostgreSQL for reliable background scraping."
            )

    anime_mapping_refresh = (
        f" - Refresh Interval: {settings.ANIME_MAPPING_REFRESH_INTERVAL}s"
        if settings.ANIME_MAPPING_ENABLED
        else ""
    )
    logger.log(
        "COMET",
        f"Anime Mapping: {settings.ANIME_MAPPING_ENABLED}{anime_mapping_refresh}",
    )

    logger.log(
        "COMET",
        f"Global Proxy: {settings.GLOBAL_PROXY_URL} - Ethos: {settings.PROXY_ETHOS}",
    )
    logger.log(
        "COMET",
        f"Rate Limit Manager: Max Retries={settings.RATELIMIT_MAX_RETRIES} - Base Delay={settings.RATELIMIT_RETRY_BASE_DELAY}s",
    )

    jackett_info = ""
    if settings.is_any_context_enabled(settings.SCRAPE_JACKETT):
        indexers = (
            ", ".join(settings.JACKETT_INDEXERS)
            if settings.JACKETT_INDEXERS
            else "All Configured/Healthy"
        )
        jackett_info = f" - {settings.JACKETT_URL} - Indexers: {indexers}"
    logger.log(
        "COMET",
        f"Jackett Scraper: {settings.format_scraper_mode(settings.SCRAPE_JACKETT)}{jackett_info}",
    )

    prowlarr_info = ""
    if settings.is_any_context_enabled(settings.SCRAPE_PROWLARR):
        indexers = (
            ", ".join(settings.PROWLARR_INDEXERS)
            if settings.PROWLARR_INDEXERS
            else "All Configured/Healthy"
        )
        prowlarr_info = f" - {settings.PROWLARR_URL} - Indexers: {indexers}"
    logger.log(
        "COMET",
        f"Prowlarr Scraper: {settings.format_scraper_mode(settings.SCRAPE_PROWLARR)}{prowlarr_info}",
    )

    logger.log("COMET", f"Indexer Manager Timeout: {settings.INDEXER_MANAGER_TIMEOUT}s")
    logger.log(
        "COMET",
        f"Indexer Manager Wait Timeout: {settings.INDEXER_MANAGER_WAIT_TIMEOUT}s",
    )
    logger.log(
        "COMET",
        f"Indexer Manager Update Interval: {settings.INDEXER_MANAGER_UPDATE_INTERVAL}s",
    )
    logger.log("COMET", f"Get Torrent Timeout: {settings.GET_TORRENT_TIMEOUT}s")
    logger.log("COMET", f"Magnet Resolve Timeout: {settings.MAGNET_RESOLVE_TIMEOUT}s")
    logger.log("COMET", f"Catalog Timeout: {settings.CATALOG_TIMEOUT}s")
    logger.log("COMET", f"Scrape Lock TTL: {settings.SCRAPE_LOCK_TTL}s")
    logger.log("COMET", f"Scrape Wait Timeout: {settings.SCRAPE_WAIT_TIMEOUT}s")
    logger.log(
        "COMET", f"Download Torrent Files: {bool(settings.DOWNLOAD_TORRENT_FILES)}"
    )

    comet_url = (
        f" - {settings.COMET_URL}"
        if settings.is_any_context_enabled(settings.SCRAPE_COMET)
        else ""
    )
    logger.log(
        "COMET",
        f"Comet Scraper: {settings.format_scraper_mode(settings.SCRAPE_COMET)}{comet_url}",
    )

    nyaa_anime_only = (
        f" - Anime Only: {bool(settings.NYAA_ANIME_ONLY)} - Concurrent Pages: {settings.NYAA_MAX_CONCURRENT_PAGES}"
        if settings.is_any_context_enabled(settings.SCRAPE_NYAA)
        else ""
    )
    logger.log(
        "COMET",
        f"Nyaa Scraper: {settings.format_scraper_mode(settings.SCRAPE_NYAA)}{nyaa_anime_only}",
    )

    animetosho_anime_only = (
        f" - Anime Only: {bool(settings.ANIMETOSHO_ANIME_ONLY)} - Concurrent Pages: {settings.ANIMETOSHO_MAX_CONCURRENT_PAGES}"
        if settings.is_any_context_enabled(settings.SCRAPE_ANIMETOSHO)
        else ""
    )
    logger.log(
        "COMET",
        f"AnimeTosho Scraper: {settings.format_scraper_mode(settings.SCRAPE_ANIMETOSHO)}{animetosho_anime_only}",
    )

    zilean_url = (
        f" - {settings.ZILEAN_URL}"
        if settings.is_any_context_enabled(settings.SCRAPE_ZILEAN)
        else ""
    )
    logger.log(
        "COMET",
        f"Zilean Scraper: {settings.format_scraper_mode(settings.SCRAPE_ZILEAN)}{zilean_url}",
    )

    stremthru_scrape_url = (
        f" - {settings.STREMTHRU_SCRAPE_URL}"
        if settings.is_any_context_enabled(settings.SCRAPE_STREMTHRU)
        else ""
    )
    logger.log(
        "COMET",
        f"StremThru Scraper: {settings.format_scraper_mode(settings.SCRAPE_STREMTHRU)}{stremthru_scrape_url}",
    )

    bitmagnet_info = (
        f" - {settings.BITMAGNET_URL} - Concurrent Pages: {settings.BITMAGNET_MAX_CONCURRENT_PAGES} - Max Offset: {settings.BITMAGNET_MAX_OFFSET}"
        if settings.is_any_context_enabled(settings.SCRAPE_BITMAGNET)
        else ""
    )
    logger.log(
        "COMET",
        f"Bitmagnet Scraper: {settings.format_scraper_mode(settings.SCRAPE_BITMAGNET)}{bitmagnet_info}",
    )

    torrentio_url = (
        f" - {settings.TORRENTIO_URL}"
        if settings.is_any_context_enabled(settings.SCRAPE_TORRENTIO)
        else ""
    )
    logger.log(
        "COMET",
        f"Torrentio Scraper: {settings.format_scraper_mode(settings.SCRAPE_TORRENTIO)}{torrentio_url}",
    )

    mediafusion_display = (
        f" - {', '.join(get_urls_with_passwords(settings.MEDIAFUSION_URL, settings.MEDIAFUSION_API_PASSWORD))}"
        if settings.is_any_context_enabled(settings.SCRAPE_MEDIAFUSION)
        else ""
    )
    logger.log(
        "COMET",
        f"MediaFusion Scraper: {settings.format_scraper_mode(settings.SCRAPE_MEDIAFUSION)}{mediafusion_display} - Live Search: {settings.MEDIAFUSION_LIVE_SEARCH}",
    )

    aiostreams_display = (
        f" - {', '.join(get_urls_with_passwords(settings.AIOSTREAMS_URL, settings.AIOSTREAMS_USER_UUID_AND_PASSWORD))}"
        if settings.is_any_context_enabled(settings.SCRAPE_AIOSTREAMS)
        else ""
    )
    logger.log(
        "COMET",
        f"AIOStreams Scraper: {settings.format_scraper_mode(settings.SCRAPE_AIOSTREAMS)}{aiostreams_display}",
    )

    jackettio_url = (
        f" - {settings.JACKETTIO_URL}"
        if settings.is_any_context_enabled(settings.SCRAPE_JACKETTIO)
        else ""
    )
    logger.log(
        "COMET",
        f"Jackettio Scraper: {settings.format_scraper_mode(settings.SCRAPE_JACKETTIO)}{jackettio_url}",
    )

    debridio_info = (
        f" - {settings.DEBRIDIO_API_KEY} - {settings.DEBRIDIO_PROVIDER}|{settings.DEBRIDIO_PROVIDER_KEY}"
        if settings.is_any_context_enabled(settings.SCRAPE_DEBRIDIO)
        else ""
    )
    logger.log(
        "COMET",
        f"Debridio Scraper: {settings.format_scraper_mode(settings.SCRAPE_DEBRIDIO)}{debridio_info}",
    )

    torbox_api_key = (
        f" - {settings.TORBOX_API_KEY}"
        if settings.is_any_context_enabled(settings.SCRAPE_TORBOX)
        else ""
    )
    logger.log(
        "COMET",
        f"TorBox Scraper: {settings.format_scraper_mode(settings.SCRAPE_TORBOX)}{torbox_api_key}",
    )

    logger.log(
        "COMET",
        f"TorrentsDB Scraper: {settings.format_scraper_mode(settings.SCRAPE_TORRENTSDB)}",
    )

    debrid_stream_proxy_display = (
        f" - Password: {settings.PROXY_DEBRID_STREAM_PASSWORD} - Max Connections: {settings.PROXY_DEBRID_STREAM_MAX_CONNECTIONS} - Inactivity Threshold: {settings.PROXY_DEBRID_STREAM_INACTIVITY_THRESHOLD}s - Default Debrid Service: {settings.PROXY_DEBRID_STREAM_DEBRID_DEFAULT_SERVICE} - Default Debrid API Key: {settings.PROXY_DEBRID_STREAM_DEBRID_DEFAULT_APIKEY}"
        if settings.PROXY_DEBRID_STREAM
        else ""
    )
    logger.log(
        "COMET",
        f"Debrid Stream Proxy: {bool(settings.PROXY_DEBRID_STREAM)}{debrid_stream_proxy_display}",
    )

    logger.log("COMET", f"StremThru URL: {settings.STREMTHRU_URL}")

    disabled_streams_info = (
        f" - Name: {settings.TORRENT_DISABLED_STREAM_NAME} - URL: {settings.TORRENT_DISABLED_STREAM_URL} - Description: {settings.TORRENT_DISABLED_STREAM_DESCRIPTION}"
        if settings.DISABLE_TORRENT_STREAMS
        else ""
    )
    logger.log(
        "COMET",
        f"Disable Torrent Streams: {bool(settings.DISABLE_TORRENT_STREAMS)}{disabled_streams_info}",
    )

    logger.log("COMET", f"Remove Adult Content: {bool(settings.REMOVE_ADULT_CONTENT)}")
    logger.log("COMET", f"RTN Filter Debug: {bool(settings.RTN_FILTER_DEBUG)}")
    logger.log(
        "COMET", f"Digital Release Filter: {bool(settings.DIGITAL_RELEASE_FILTER)}"
    )
    logger.log(
        "COMET",
        f"TMDB Read Access Token: {settings.TMDB_READ_ACCESS_TOKEN if settings.TMDB_READ_ACCESS_TOKEN else 'Shared'}",
    )
    logger.log("COMET", f"Custom Header HTML: {bool(settings.CUSTOM_HEADER_HTML)}")

    http_cache_info = (
        f" - Streams TTL: {settings.HTTP_CACHE_STREAMS_TTL}s - Manifest TTL: {settings.HTTP_CACHE_MANIFEST_TTL}s - Configure TTL: {settings.HTTP_CACHE_CONFIGURE_TTL}s - SWR: {settings.HTTP_CACHE_STALE_WHILE_REVALIDATE}s"
        if settings.HTTP_CACHE_ENABLED
        else ""
    )
    logger.log(
        "COMET",
        f"HTTP Cache: {bool(settings.HTTP_CACHE_ENABLED)}{http_cache_info}",
    )

    background_scraper_display = (
        f" - Workers: {settings.BACKGROUND_SCRAPER_CONCURRENT_WORKERS} - Interval: {settings.BACKGROUND_SCRAPER_INTERVAL}s - Max Movies/Run: {settings.BACKGROUND_SCRAPER_MAX_MOVIES_PER_RUN} - Max Series/Run: {settings.BACKGROUND_SCRAPER_MAX_SERIES_PER_RUN}"
        if settings.BACKGROUND_SCRAPER_ENABLED
        else ""
    )
    logger.log(
        "COMET",
        f"Background Scraper: {bool(settings.BACKGROUND_SCRAPER_ENABLED)}{background_scraper_display}",
    )
