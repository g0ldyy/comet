import contextlib
import signal
import sys
import threading
import time
import traceback
import uvicorn
import os
import asyncio
import aiohttp

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from comet.api.core import main
from comet.api.stream import streams
from comet.utils.database import (
    setup_database,
    teardown_database,
    cleanup_expired_locks,
    cleanup_expired_sessions,
)
from comet.utils.trackers import download_best_trackers
from comet.utils.general import associate_urls_credentials
from comet.utils.logger import logger
from comet.utils.models import settings
from comet.utils.bandwidth_monitor import bandwidth_monitor
from comet.background_scraper.worker import background_scraper
from comet.utils.anime_mapper import anime_mapper


class LoguruMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start_time = time.time()
        try:
            response = await call_next(request)
        except Exception as e:
            logger.exception(f"Exception during request processing: {e}")
            raise
        finally:
            process_time = time.time() - start_time
            logger.log(
                "API",
                f"{request.method} {request.url.path} - {response.status_code if 'response' in locals() else '500'} - {process_time:.2f}s",
            )
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    await setup_database()

    # Load anime ID mapping for enhanced metadata and anime detection
    async with aiohttp.ClientSession() as session:
        await anime_mapper.load_anime_mapping(session)

    # Initialize bandwidth monitoring system
    await bandwidth_monitor.initialize()

    # Start background cleanup tasks
    cleanup_locks_task = asyncio.create_task(cleanup_expired_locks())
    cleanup_sessions_task = asyncio.create_task(cleanup_expired_sessions())

    # Start background scraper if enabled
    background_scraper_task = None
    if settings.BACKGROUND_SCRAPER_ENABLED:
        background_scraper_task = asyncio.create_task(background_scraper.start())

    try:
        yield
    finally:
        if background_scraper_task:
            await background_scraper.stop()
            background_scraper_task.cancel()
            try:
                await background_scraper_task
            except asyncio.CancelledError:
                pass

        cleanup_locks_task.cancel()
        cleanup_sessions_task.cancel()
        try:
            await cleanup_locks_task
        except asyncio.CancelledError:
            pass
        try:
            await cleanup_sessions_task
        except asyncio.CancelledError:
            pass

        await bandwidth_monitor.shutdown()

        await teardown_database()


app = FastAPI(
    title="Comet",
    summary="Stremio's fastest torrent/debrid search add-on.",
    lifespan=lifespan,
    redoc_url=None,
)


app.add_middleware(LoguruMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="comet/templates"), name="static")

app.include_router(main)
app.include_router(streams)


class Server(uvicorn.Server):
    def install_signal_handlers(self):
        pass

    @contextlib.contextmanager
    def run_in_thread(self):
        thread = threading.Thread(target=self.run, name="Comet")
        thread.start()
        try:
            while not self.started:
                time.sleep(1e-3)
            yield
        except Exception as e:
            logger.error(f"Error in server thread: {e}")
            logger.exception(traceback.format_exc())
            raise e
        finally:
            self.should_exit = True
            sys.exit(0)


def signal_handler(sig, frame):
    # This will handle kubernetes/docker shutdowns better
    # Toss anything that needs to be gracefully shutdown here
    logger.log("COMET", "Exiting Gracefully.")
    sys.exit(0)


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def get_urls_with_passwords(urls, passwords):
    url_credentials_pairs = associate_urls_credentials(urls, passwords)

    result = []
    for url, password in url_credentials_pairs:
        if password:
            result.append(f"{url}|{password}")
        else:
            result.append(url)

    return result


def start_log():
    logger.log(
        "COMET",
        f"Server started on http://{settings.FASTAPI_HOST}:{settings.FASTAPI_PORT} - {settings.FASTAPI_WORKERS} workers",
    )
    logger.log(
        "COMET",
        f"Admin Dashboard Password: {settings.ADMIN_DASHBOARD_PASSWORD} -  http://{settings.FASTAPI_HOST}:{settings.FASTAPI_PORT}/admin - Public Metrics API: {settings.PUBLIC_METRICS_API}",
    )
    logger.log(
        "COMET",
        f"Database ({settings.DATABASE_TYPE}): {settings.DATABASE_PATH if settings.DATABASE_TYPE == 'sqlite' else settings.DATABASE_URL} - TTL: metadata={settings.METADATA_CACHE_TTL}s, torrents={settings.TORRENT_CACHE_TTL}s, debrid={settings.DEBRID_CACHE_TTL}s",
    )
    logger.log("COMET", f"Debrid Proxy: {settings.DEBRID_PROXY_URL}")

    if settings.is_any_context_enabled(settings.INDEXER_MANAGER_MODE):
        logger.log(
            "COMET",
            f"Indexer Manager: {settings.INDEXER_MANAGER_TYPE}|{settings.INDEXER_MANAGER_URL} - Mode: {settings.INDEXER_MANAGER_MODE} - Timeout: {settings.INDEXER_MANAGER_TIMEOUT}s",
        )
        logger.log("COMET", f"Indexers: {', '.join(settings.INDEXER_MANAGER_INDEXERS)}")
        logger.log("COMET", f"Get Torrent Timeout: {settings.GET_TORRENT_TIMEOUT}s")
        logger.log(
            "COMET", f"Download Torrent Files: {bool(settings.DOWNLOAD_TORRENT_FILES)}"
        )
    else:
        logger.log("COMET", "Indexer Manager: False")

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
        f" - Anime Only: {bool(settings.NYAA_ANIME_ONLY)}"
        if settings.is_any_context_enabled(settings.SCRAPE_NYAA)
        else ""
    )
    logger.log(
        "COMET",
        f"Nyaa Scraper: {settings.format_scraper_mode(settings.SCRAPE_NYAA)}{nyaa_anime_only}",
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

    debridio_api_key = (
        f" - {settings.DEBRIDIO_API_KEY}"
        if settings.is_any_context_enabled(settings.SCRAPE_DEBRIDIO)
        else ""
    )
    logger.log(
        "COMET",
        f"Debridio Scraper: {settings.format_scraper_mode(settings.SCRAPE_DEBRIDIO)}{debridio_api_key}",
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

    debrid_stream_proxy_display = (
        f" - Password: {settings.PROXY_DEBRID_STREAM_PASSWORD} - Max Connections: {settings.PROXY_DEBRID_STREAM_MAX_CONNECTIONS} - Default Debrid Service: {settings.PROXY_DEBRID_STREAM_DEBRID_DEFAULT_SERVICE} - Default Debrid API Key: {settings.PROXY_DEBRID_STREAM_DEBRID_DEFAULT_APIKEY}"
        if settings.PROXY_DEBRID_STREAM
        else ""
    )
    logger.log(
        "COMET",
        f"Debrid Stream Proxy: {bool(settings.PROXY_DEBRID_STREAM)}{debrid_stream_proxy_display}",
    )

    logger.log("COMET", f"StremThru URL: {settings.STREMTHRU_URL}")

    logger.log("COMET", f"Remove Adult Content: {bool(settings.REMOVE_ADULT_CONTENT)}")
    logger.log("COMET", f"Custom Header HTML: {bool(settings.CUSTOM_HEADER_HTML)}")

    background_scraper_display = (
        f" - Workers: {settings.BACKGROUND_SCRAPER_CONCURRENT_WORKERS} - Interval: {settings.BACKGROUND_SCRAPER_INTERVAL}s - Max Movies/Run: {settings.BACKGROUND_SCRAPER_MAX_MOVIES_PER_RUN} - Max Series/Run: {settings.BACKGROUND_SCRAPER_MAX_SERIES_PER_RUN}"
        if settings.BACKGROUND_SCRAPER_ENABLED
        else ""
    )
    logger.log(
        "COMET",
        f"Background Scraper: {bool(settings.BACKGROUND_SCRAPER_ENABLED)}{background_scraper_display}",
    )


def run_with_uvicorn():
    """Run the server with uvicorn only"""
    config = uvicorn.Config(
        app,
        host=settings.FASTAPI_HOST,
        port=settings.FASTAPI_PORT,
        proxy_headers=True,
        forwarded_allow_ips="*",
        workers=settings.FASTAPI_WORKERS,
        log_config=None,
    )
    server = Server(config=config)

    with server.run_in_thread():
        start_log()
        try:
            while True:
                time.sleep(1)  # Keep the main thread alive
        except KeyboardInterrupt:
            logger.log("COMET", "Server stopped by user")
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            logger.exception(traceback.format_exc())
        finally:
            logger.log("COMET", "Server Shutdown")


def run_with_gunicorn():
    """Run the server with gunicorn and uvicorn workers"""
    import gunicorn.app.base

    class StandaloneApplication(gunicorn.app.base.BaseApplication):
        def __init__(self, app, options=None):
            self.options = options or {}
            self.application = app
            super().__init__()

        def load_config(self):
            config = {
                key: value
                for key, value in self.options.items()
                if key in self.cfg.settings and value is not None
            }
            for key, value in config.items():
                self.cfg.set(key.lower(), value)

        def load(self):
            return self.application

    workers = settings.FASTAPI_WORKERS
    if workers < 1:
        workers = min((os.cpu_count() or 1) * 2 + 1, 12)

    options = {
        "bind": f"{settings.FASTAPI_HOST}:{settings.FASTAPI_PORT}",
        "workers": workers,
        "worker_class": "uvicorn.workers.UvicornWorker",
        "timeout": 120,
        "keepalive": 5,
        "preload_app": True,
        "proxy_protocol": True,
        "forwarded_allow_ips": "*",
        "loglevel": "warning",
    }

    start_log()
    logger.log("COMET", f"Starting with gunicorn using {workers} workers")

    StandaloneApplication(app, options).run()


if __name__ == "__main__":
    if os.name == "nt" or not settings.USE_GUNICORN:
        run_with_uvicorn()
    else:
        run_with_gunicorn()
