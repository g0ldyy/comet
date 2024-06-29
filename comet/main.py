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
from comet.utils.db import setup_database, teardown_database, write_config
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
    write_config()
    await setup_database()
    yield
    await teardown_database()

app = FastAPI(
    title="Comet",
    summary="Stremio's fastest torrent/debrid search add-on.",
    version="1.0.0",
    lifespan=lifespan,
    redoc_url=None,
    license_info={
        "name": "GPL-3.0",
        "url": "https://www.gnu.org/licenses/gpl-3.0.en.html",
    }
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
    logger.log('COMET', 'Exiting Gracefully.')
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

config = uvicorn.Config(
    app,
    host=settings.FASTAPI_HOST,
    port=settings.FASTAPI_PORT,
    workers=settings.FASTAPI_WORKERS,
    log_config=None
)
server = Server(config=config)

def start_log():
    logger.log('COMET', f'Server started on {settings.FASTAPI_HOST}:{settings.FASTAPI_PORT}')
    logger.log('COMET', f'Database: {settings.DATABASE_PATH}')
    logger.log('COMET', f'Workers: {settings.FASTAPI_WORKERS}')
    logger.log('COMET', f'Get Torrent Timeout: {settings.GET_TORRENT_TIMEOUT}')
    logger.log('COMET', f"Debrid Proxy: {settings.DEBRID_PROXY_URL}")
    logger.log('COMET', f'Indexers: {settings.INDEXER_MANAGER_INDEXERS}')
    
    if settings.CUSTOM_HEADER_HTML:
        logger.log('COMET', f'Custom Header HTML Enabled: {bool(settings.CUSTOM_HEADER_HTML)}')

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
