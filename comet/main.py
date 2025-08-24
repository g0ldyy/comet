import contextlib
import signal
import sys
import threading
import time
import traceback
import uvicorn
import os
import asyncio

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
from comet.utils.logger import logger
from comet.utils.models import settings
from comet.utils.bandwidth_monitor import bandwidth_monitor


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
    await download_best_trackers()

    # Initialize bandwidth monitoring system
    await bandwidth_monitor.initialize()

    # Start background cleanup tasks
    cleanup_locks_task = asyncio.create_task(cleanup_expired_locks())
    cleanup_sessions_task = asyncio.create_task(cleanup_expired_sessions())

    try:
        yield
    finally:
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

    if settings.INDEXER_MANAGER_TYPE:
        logger.log(
            "COMET",
            f"Indexer Manager: {settings.INDEXER_MANAGER_TYPE}|{settings.INDEXER_MANAGER_URL} - Timeout: {settings.INDEXER_MANAGER_TIMEOUT}s",
        )
        logger.log("COMET", f"Indexers: {', '.join(settings.INDEXER_MANAGER_INDEXERS)}")
        logger.log("COMET", f"Get Torrent Timeout: {settings.GET_TORRENT_TIMEOUT}s")
        logger.log(
            "COMET", f"Download Torrent Files: {bool(settings.DOWNLOAD_TORRENT_FILES)}"
        )
    else:
        logger.log("COMET", "Indexer Manager: False")

    comet_url = f" - {settings.COMET_URL}"
    logger.log(
        "COMET",
        f"Comet Scraper: {bool(settings.SCRAPE_COMET)}{comet_url if settings.SCRAPE_COMET else ''}",
    )

    zilean_url = f" - {settings.ZILEAN_URL}"
    logger.log(
        "COMET",
        f"Zilean Scraper: {bool(settings.SCRAPE_ZILEAN)}{zilean_url if settings.SCRAPE_ZILEAN else ''}",
    )

    torrentio_url = f" - {settings.TORRENTIO_URL}"
    logger.log(
        "COMET",
        f"Torrentio Scraper: {bool(settings.SCRAPE_TORRENTIO)}{torrentio_url if settings.SCRAPE_TORRENTIO else ''}",
    )

    mediafusion_url = f" - {settings.MEDIAFUSION_URL}"
    mediafusion_api_password = (
        f" - API Password: {settings.MEDIAFUSION_API_PASSWORD}"
        if settings.MEDIAFUSION_API_PASSWORD
        else ""
    )
    logger.log(
        "COMET",
        f"MediaFusion Scraper: {bool(settings.SCRAPE_MEDIAFUSION)}{mediafusion_url if settings.SCRAPE_MEDIAFUSION else ''}{mediafusion_api_password if settings.SCRAPE_MEDIAFUSION else ''} - Live Search: {settings.MEDIAFUSION_LIVE_SEARCH}",
    )

    logger.log(
        "COMET",
        f"Debrid Stream Proxy: {bool(settings.PROXY_DEBRID_STREAM)} - Password: {settings.PROXY_DEBRID_STREAM_PASSWORD} - Max Connections: {settings.PROXY_DEBRID_STREAM_MAX_CONNECTIONS} - Default Debrid Service: {settings.PROXY_DEBRID_STREAM_DEBRID_DEFAULT_SERVICE} - Default Debrid API Key: {settings.PROXY_DEBRID_STREAM_DEBRID_DEFAULT_APIKEY}",
    )

    logger.log("COMET", f"StremThru URL: {settings.STREMTHRU_URL}")

    logger.log("COMET", f"Remove Adult Content: {bool(settings.REMOVE_ADULT_CONTENT)}")
    logger.log("COMET", f"Custom Header HTML: {bool(settings.CUSTOM_HEADER_HTML)}")


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
