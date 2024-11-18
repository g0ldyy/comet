import contextlib
import signal
import sys
import threading
import time
import traceback
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from comet.api.core import main
from comet.api.stream import streams
from comet.utils.db import setup_database, teardown_database
from comet.utils.logger import logger
from comet.utils.models import settings


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
    yield
    await teardown_database()


app = FastAPI(
    title="Comet",
    summary="Stremio's fastest torrent/debrid search add-on.",
    version="1.0.0",
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


def start_log():
    logger.log(
        "COMET",
        f"Server started on http://{settings.FASTAPI_HOST}:{settings.FASTAPI_PORT} - {settings.FASTAPI_WORKERS} workers",
    )
    logger.log(
        "COMET",
        f"Dashboard Admin Password: {settings.DASHBOARD_ADMIN_PASSWORD} -  http://{settings.FASTAPI_HOST}:{settings.FASTAPI_PORT}/active-connections?password={settings.DASHBOARD_ADMIN_PASSWORD}",
    )
    logger.log(
        "COMET",
        f"Database ({settings.DATABASE_TYPE}): {settings.DATABASE_PATH if settings.DATABASE_TYPE == 'sqlite' else settings.DATABASE_URL} - TTL: {settings.CACHE_TTL}s",
    )
    logger.log("COMET", f"Debrid Proxy: {settings.DEBRID_PROXY_URL}")

    if settings.INDEXER_MANAGER_TYPE:
        logger.log(
            "COMET",
            f"Indexer Manager: {settings.INDEXER_MANAGER_TYPE}|{settings.INDEXER_MANAGER_URL} - Timeout: {settings.INDEXER_MANAGER_TIMEOUT}s",
        )
        logger.log("COMET", f"Indexers: {', '.join(settings.INDEXER_MANAGER_INDEXERS)}")
        logger.log("COMET", f"Get Torrent Timeout: {settings.GET_TORRENT_TIMEOUT}s")
    else:
        logger.log("COMET", "Indexer Manager: False")

    if settings.ZILEAN_URL:
        logger.log(
            "COMET",
            f"Zilean: {settings.ZILEAN_URL} - Take first: {settings.ZILEAN_TAKE_FIRST}",
        )
    else:
        logger.log("COMET", "Zilean: False")

    logger.log("COMET", f"Torrentio Scraper: {bool(settings.SCRAPE_TORRENTIO)}")

    mediafusion_url = f" - {settings.MEDIAFUSION_URL}"
    logger.log(
        "COMET",
        f"MediaFusion Scraper: {bool(settings.SCRAPE_MEDIAFUSION)}{mediafusion_url if settings.SCRAPE_MEDIAFUSION else ''}",
    )

    logger.log(
        "COMET",
        f"Debrid Stream Proxy: {bool(settings.PROXY_DEBRID_STREAM)} - Password: {settings.PROXY_DEBRID_STREAM_PASSWORD} - Max Connections: {settings.PROXY_DEBRID_STREAM_MAX_CONNECTIONS} - Default Debrid Service: {settings.PROXY_DEBRID_STREAM_DEBRID_DEFAULT_SERVICE} - Default Debrid API Key: {settings.PROXY_DEBRID_STREAM_DEBRID_DEFAULT_APIKEY}",
    )
    logger.log("COMET", f"Title Match Check: {bool(settings.TITLE_MATCH_CHECK)}")
    logger.log("COMET", f"Remove Adult Content: {bool(settings.REMOVE_ADULT_CONTENT)}")
    logger.log("COMET", f"Custom Header HTML: {bool(settings.CUSTOM_HEADER_HTML)}")


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
