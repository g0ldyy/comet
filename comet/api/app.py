import asyncio
import time
from contextlib import asynccontextmanager

import aiohttp
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from comet.api.endpoints import (admin, base, chilllink, config, manifest,
                                 playback)
from comet.api.endpoints import stream as streams_router
from comet.background_scraper.worker import background_scraper
from comet.core.database import (cleanup_expired_locks,
                                 cleanup_expired_sessions, setup_database,
                                 teardown_database)
from comet.core.execution import setup_executor, shutdown_executor
from comet.core.logger import logger
from comet.core.models import settings
from comet.services.anime import anime_mapper
from comet.services.bandwidth import bandwidth_monitor
from comet.services.indexer_manager import indexer_manager
from comet.services.torrent_manager import (add_torrent_queue,
                                            torrent_update_queue)
from comet.services.trackers import download_best_trackers


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
    # loop = asyncio.get_running_loop()
    # loop.set_debug(True)

    await setup_database()
    setup_executor()

    if settings.DOWNLOAD_GENERIC_TRACKERS:
        await download_best_trackers()

    # Load anime ID mapping for enhanced metadata and anime detection
    async with aiohttp.ClientSession() as session:
        await anime_mapper.load_anime_mapping(session)

    # Initialize bandwidth monitoring system
    if settings.PROXY_DEBRID_STREAM:
        await bandwidth_monitor.initialize()

    # Start background cleanup tasks
    cleanup_locks_task = asyncio.create_task(cleanup_expired_locks())
    cleanup_sessions_task = asyncio.create_task(cleanup_expired_sessions())

    # Start background scraper if enabled
    background_scraper_task = None
    if settings.BACKGROUND_SCRAPER_ENABLED:
        background_scraper_task = asyncio.create_task(background_scraper.start())

    # Start indexer manager
    indexer_manager_task = asyncio.create_task(indexer_manager.run())

    try:
        yield
    finally:
        indexer_manager_task.cancel()
        try:
            await indexer_manager_task
        except asyncio.CancelledError:
            pass

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

        if settings.PROXY_DEBRID_STREAM:
            await bandwidth_monitor.shutdown()

        await add_torrent_queue.stop()
        await torrent_update_queue.stop()

        await teardown_database()
        shutdown_executor()


tags_metadata = [
    {
        "name": "General",
        "description": "General application endpoints.",
    },
    {
        "name": "Configuration",
        "description": "Endpoints for configuring Comet.",
    },
    {
        "name": "Stremio",
        "description": "Standard Stremio endpoints.",
    },
    {
        "name": "ChillLink",
        "description": "Chillio specific endpoints.",
    },
    {
        "name": "Admin",
        "description": "Admin dashboard and API endpoints.",
    },
]

app = FastAPI(
    title="Comet",
    summary="Stremio's fastest torrent/debrid search add-on.",
    lifespan=lifespan,
    redoc_url=None,
    openapi_tags=tags_metadata,
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

app.include_router(base.router)
app.include_router(config.router)
app.include_router(manifest.router)
app.include_router(admin.router)
app.include_router(playback.router)
app.include_router(streams_router.streams)
app.include_router(chilllink.router)
