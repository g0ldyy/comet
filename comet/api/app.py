import asyncio
import time
from contextlib import AsyncExitStack, asynccontextmanager

import aiohttp
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from comet.api.endpoints import (
    admin,
    base,
    chilllink,
    cometnet,
    cometnet_ui,
    config,
    debrid_sync,
    kodi,
    manifest,
    playback,
)
from comet.api.endpoints import stream as streams_router
from comet.background_scraper.worker import background_scraper
from comet.cometnet.manager import init_cometnet_service
from comet.cometnet.relay import init_relay, stop_relay
from comet.core.database import (
    cleanup_expired_kodi_setup_codes,
    cleanup_expired_locks,
    setup_database,
    teardown_database,
)
from comet.core.execution import setup_executor, shutdown_executor
from comet.core.logger import logger
from comet.core.models import STREMIO_API_PREFIX, settings
from comet.services.anime import anime_mapper
from comet.services.bandwidth import bandwidth_monitor
from comet.services.debrid_account_scraper import shutdown_account_sync_tasks
from comet.services.debrid_cache import shutdown_cache_writes
from comet.services.dmm_ingester import dmm_ingester
from comet.services.indexer_manager import indexer_manager
from comet.services.torrent_manager import (
    add_torrent_queue,
    check_torrents_exist,
    torrent_update_queue,
)
from comet.services.trackers import download_best_trackers
from comet.utils.http_client import http_client_manager
from comet.utils.memory import periodic_memory_trim
from comet.utils.network_manager import network_manager


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


async def _cancel_task(task: asyncio.Task):
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.error(f"Background task failed during shutdown: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # loop = asyncio.get_running_loop()
    # loop.set_debug(True)

    async with AsyncExitStack() as cleanup:
        await setup_database()
        cleanup.push_async_callback(teardown_database)
        cleanup.push_async_callback(shutdown_cache_writes)
        cleanup.push_async_callback(anime_mapper.stop)

        cleanup.callback(shutdown_executor)
        setup_executor()

        cleanup.push_async_callback(http_client_manager.close)
        await http_client_manager.init()
        cleanup.push_async_callback(shutdown_account_sync_tasks)
        cleanup.push_async_callback(network_manager.close_all)
        cleanup.push_async_callback(torrent_update_queue.stop)
        cleanup.push_async_callback(add_torrent_queue.stop)

        if settings.DOWNLOAD_GENERIC_TRACKERS:
            await download_best_trackers()

        # Load anime ID mapping for enhanced metadata and anime detection
        async with aiohttp.ClientSession() as session:
            await anime_mapper.load_anime_mapping(session)

        # Initialize bandwidth monitoring system
        if settings.PROXY_DEBRID_STREAM:
            cleanup.push_async_callback(bandwidth_monitor.shutdown)
            await bandwidth_monitor.initialize()

        # Start background cleanup tasks
        cleanup_locks_task = asyncio.create_task(cleanup_expired_locks())
        cleanup.push_async_callback(_cancel_task, cleanup_locks_task)
        cleanup_kodi_task = asyncio.create_task(cleanup_expired_kodi_setup_codes())
        cleanup.push_async_callback(_cancel_task, cleanup_kodi_task)
        memory_trim_interval = settings.MEMORY_TRIM_INTERVAL
        if memory_trim_interval > 0:
            memory_trim_task = asyncio.create_task(
                periodic_memory_trim(memory_trim_interval)
            )
            cleanup.push_async_callback(_cancel_task, memory_trim_task)

        # Start background scraper if enabled
        if settings.BACKGROUND_SCRAPER_ENABLED:
            background_scraper.clear_finished_task()
            if not background_scraper.task:
                background_scraper.task = asyncio.create_task(
                    background_scraper.start()
                )
            cleanup.push_async_callback(background_scraper.stop)

        # Start DMM Ingester if enabled
        if settings.DMM_INGEST_ENABLED:
            dmm_ingester_task = asyncio.create_task(dmm_ingester.start())
            cleanup.push_async_callback(_cancel_task, dmm_ingester_task)
            cleanup.push_async_callback(dmm_ingester.stop)

        # Initialize CometNet
        if settings.COMETNET_RELAY_URL:
            await init_relay(
                settings.COMETNET_RELAY_URL, api_key=settings.COMETNET_API_KEY
            )
            cleanup.push_async_callback(stop_relay)

        elif settings.COMETNET_ENABLED:
            cometnet_service = init_cometnet_service(
                enabled=True,
                listen_port=settings.COMETNET_LISTEN_PORT,
                bootstrap_nodes=settings.COMETNET_BOOTSTRAP_NODES,
                manual_peers=settings.COMETNET_MANUAL_PEERS,
                max_peers=settings.COMETNET_MAX_PEERS,
                min_peers=settings.COMETNET_MIN_PEERS,
            )

            # Set callback to save torrents received from the network
            cometnet_service.set_save_torrent_callback(
                torrent_update_queue.add_network_torrent
            )
            cometnet_service.set_check_torrents_exist_callback(check_torrents_exist)
            await cometnet_service.start()
            cleanup.push_async_callback(cometnet_service.stop)

        # Start indexer manager
        indexer_manager_task = asyncio.create_task(indexer_manager.run())
        cleanup.push_async_callback(indexer_manager.close)
        cleanup.push_async_callback(_cancel_task, indexer_manager_task)

        yield


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
        "name": "Kodi",
        "description": "Kodi specific endpoints.",
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
    docs_url=None if STREMIO_API_PREFIX else "/docs",
    openapi_url=None if STREMIO_API_PREFIX else "/openapi.json",
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

app.include_router(base.router)
app.include_router(config.router)
app.include_router(admin.router)
app.include_router(cometnet.router)
app.include_router(cometnet_ui.router)
app.include_router(kodi.router)

if STREMIO_API_PREFIX:
    app.include_router(config.router, prefix=STREMIO_API_PREFIX)

stremio_routers = (
    manifest.router,
    playback.router,
    debrid_sync.router,
    streams_router.streams,
    chilllink.router,
)

for stremio_router in stremio_routers:
    app.include_router(stremio_router, prefix=STREMIO_API_PREFIX)
