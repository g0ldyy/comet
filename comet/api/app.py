import asyncio
import time
from contextlib import asynccontextmanager

import aiohttp
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from comet.api.endpoints import (admin, base, chilllink, cometnet, cometnet_ui,
                                 config, manifest, playback)
from comet.api.endpoints import stream as streams_router
from comet.background_scraper.worker import background_scraper
from comet.cometnet.manager import init_cometnet_service
from comet.cometnet.protocol import TorrentMetadata
from comet.cometnet.relay import init_relay, stop_relay
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

    # Initialize CometNet
    cometnet_service = None
    cometnet_relay = None

    if settings.COMETNET_RELAY_URL:
        cometnet_relay = await init_relay(
            settings.COMETNET_RELAY_URL, api_key=settings.COMETNET_API_KEY
        )

    elif settings.COMETNET_ENABLED:
        if settings.FASTAPI_WORKERS > 1:
            logger.warning(
                f"⚠️  CometNet with {settings.FASTAPI_WORKERS} workers may cause port conflicts. "
                "Consider using COMETNET_RELAY_URL for multi-worker deployments."
            )

        cometnet_service = init_cometnet_service(
            enabled=True,
            listen_port=settings.COMETNET_LISTEN_PORT,
            bootstrap_nodes=settings.COMETNET_BOOTSTRAP_NODES,
            manual_peers=settings.COMETNET_MANUAL_PEERS,
            max_peers=settings.COMETNET_MAX_PEERS,
            min_peers=settings.COMETNET_MIN_PEERS,
        )

        # Set callback to save torrents received from the network
        async def save_torrent_from_network(metadata):
            """Save a torrent received from the CometNet P2P network."""

            if isinstance(metadata, TorrentMetadata):
                await torrent_update_queue.add_torrent_info(
                    {
                        "info_hash": metadata.info_hash,
                        "index": metadata.file_index or 0,
                        "season": metadata.season,
                        "episode": metadata.episode,
                        "title": metadata.title,
                        "seeders": metadata.seeders or 0,
                        "size": metadata.size,
                        "tracker": metadata.tracker,
                        "sources": metadata.sources or [],
                        "parsed": metadata.parsed or {"raw_title": metadata.title},
                    },
                    media_id=metadata.imdb_id,
                    from_cometnet=True,
                )

        cometnet_service.set_save_torrent_callback(save_torrent_from_network)
        await cometnet_service.start()

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

        if cometnet_service:
            await cometnet_service.stop()

        if cometnet_relay:
            await stop_relay()

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
app.include_router(cometnet.router)
app.include_router(cometnet_ui.router)
