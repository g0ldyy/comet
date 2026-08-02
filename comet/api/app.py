import asyncio
import os
import time
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from comet.observability.logging import (
    bootstrap_failure,
    configuration_invalid,
    current_settings,
    ensure_configured,
)

_web_master_pid = os.environ.get("COMET_WEB_MASTER_PID")
_initial_role = (
    "web_master"
    if _web_master_pid is not None and _web_master_pid == str(os.getpid())
    else "web_worker"
)
try:
    ensure_configured(process_role=_initial_role)
except Exception as exc:
    bootstrap_failure(exception=exc, process_role=_initial_role)
    raise SystemExit(78) from None

try:
    from comet.core.models import STREMIO_API_PREFIX, database, settings
except Exception as exc:
    configuration_invalid(exception=exc)
    raise SystemExit(78) from None

from comet.api import configure_metrics
from comet.api.endpoints import (
    base,
    chilllink,
    cometnet,
    config,
    debrid_sync,
    kodi,
    manifest,
    nzb,
    playback,
    prometheus,
    torznab,
    usenet_playback,
)
from comet.api.endpoints import stream as streams_router
from comet.api.frontend import install_frontend_assets
from comet.api.v1 import router as api_v1
from comet.api.v1.responses import install_api_error_handlers
from comet.background_scraper.worker import background_scraper
from comet.cometnet.manager import init_cometnet_service
from comet.cometnet.relay import init_relay, stop_relay
from comet.core.capability_states import shutdown_capability_refreshes
from comet.core.database import (
    cleanup_expired_kodi_setup_codes,
    cleanup_expired_locks,
    cleanup_expired_usenet_state,
    setup_database,
    teardown_database,
)
from comet.core.execution import setup_executor, shutdown_executor
from comet.core.runtime_registry import RuntimeRegistry
from comet.observability import (
    TerminalFlag,
    create_detached_task,
    current_terminal_flags,
    log,
    metrics,
    request_context,
)
from comet.services.anime import anime_mapper
from comet.services.bandwidth import bandwidth_monitor
from comet.services.debrid_account_scraper import shutdown_account_sync_tasks
from comet.services.debrid_cache import shutdown_cache_writes
from comet.services.dmm_ingester import dmm_ingester
from comet.services.indexer_manager import indexer_manager
from comet.services.operator_commands import (
    run_command_dispatcher,
    withdraw_command_dispatcher,
)
from comet.services.torrent_manager import (
    add_torrent_queue,
    check_torrents_exist,
    torrent_update_queue,
)
from comet.services.trackers import download_best_trackers
from comet.services.usenet_operations import usenet_operation_monitor
from comet.utils.http_client import http_client_manager
from comet.utils.memory import memory_trimmer
from comet.utils.network_manager import network_manager

try:
    configure_metrics(settings)
except Exception as exc:
    configuration_invalid(exception=exc)
    raise SystemExit(78) from None


def _route_name(scope: dict[str, Any]) -> str:
    name = getattr(scope.get("route"), "name", None)
    if (
        isinstance(name, str)
        and name
        and name.isascii()
        and len(name) <= 64
        and name.replace("_", "").isalnum()
        and (name[0].isalpha())
    ):
        return name.lower()
    return "unmatched"


def _is_private_response(scope: dict[str, Any]) -> bool:
    endpoint = scope.get("endpoint")
    module = getattr(endpoint, "__module__", "")
    path_parameter_names = frozenset((scope.get("path_params") or {}).keys())
    return bool(
        module.startswith("comet.api.v1.")
        or module == "comet.api.endpoints.kodi"
        or path_parameter_names & {"b64config", "capability", "code"}
    )


_PROBE_ROUTE_NAMES = frozenset(
    {
        "health",
        "ready",
        "prometheus_metrics",
    }
)
_SHARED_ROUTE_NAMES = frozenset(
    {
        "manifest",
        "get_manifest",
        "stream",
        "streams",
        "torznab_api",
        "prometheus_metrics",
    }
)


def _shared_cache_response(
    route_name: str,
    headers: list[tuple[bytes, bytes]],
) -> bool:
    cache_control = b",".join(
        value.lower() for name, value in headers if name.lower() == b"cache-control"
    )
    if b"private" in cache_control or b"no-store" in cache_control:
        return False
    return bool(
        route_name in _SHARED_ROUTE_NAMES
        or b"public" in cache_control
        or b"s-maxage" in cache_control
    )


class CorrelationMiddleware:
    """Pure ASGI correlation, response policy, metrics and HTTP ownership."""

    def __init__(self, application):
        self.application = application

    async def __call__(self, scope, receive, send):
        with settings.bind():
            if scope["type"] == "http":
                async with http_client_manager.bind():
                    await self._call_bound(scope, receive, send)
            else:
                await self._call_bound(scope, receive, send)

    async def _call_bound(self, scope, receive, send):
        if scope["type"] != "http":
            await self.application(scope, receive, send)
            return

        method = scope["method"].lower()
        started_at = time.monotonic_ns()
        status_code = 500
        response_size = 0
        response_started = False
        send_failed = False
        application_error: BaseException | None = None
        request_identifier: str

        with request_context() as request_identifier:
            metrics.http_started(method)

            async def send_with_context(message):
                nonlocal status_code, response_size
                nonlocal response_started, send_failed
                try:
                    if message["type"] == "http.response.start":
                        response_started = True
                        status_code = int(message["status"])
                        headers = [
                            (name, value)
                            for name, value in message.get("headers", [])
                            if name.lower() != b"x-request-id"
                        ]
                        if _is_private_response(scope):
                            headers = [
                                (name, value)
                                for name, value in headers
                                if name.lower()
                                not in {
                                    b"cache-control",
                                    b"pragma",
                                    b"referrer-policy",
                                }
                            ]
                            headers.extend(
                                (
                                    (b"cache-control", b"private, no-store"),
                                    (b"pragma", b"no-cache"),
                                    (b"referrer-policy", b"no-referrer"),
                                )
                            )
                            if scope["path"].startswith("/api/v1/"):
                                headers.append((b"vary", b"Cookie"))
                        route_name = _route_name(scope)
                        if not _shared_cache_response(route_name, headers):
                            headers.append(
                                (b"x-request-id", request_identifier.encode("ascii"))
                            )
                        message = {**message, "headers": headers}
                    elif message["type"] == "http.response.body":
                        response_size += len(message.get("body", b""))
                    await send(message)
                except BaseException:
                    send_failed = True
                    raise

            try:
                await self.application(scope, receive, send_with_context)
            except BaseException as exc:
                application_error = exc
                raise
            finally:
                duration_ms = (time.monotonic_ns() - started_at) / 1_000_000
                route_name = _route_name(scope)
                is_probe = route_name in _PROBE_ROUTE_NAMES
                if not is_probe:
                    metrics.http_finished(
                        method,
                        route_name,
                        status_code,
                        duration_ms / 1000,
                        response_size,
                    )
                else:
                    metrics.http_probe_finished(method)
                if not is_probe:
                    flags = current_terminal_flags()
                    snapshot = (
                        flags.snapshot() if flags is not None else TerminalFlag(0)
                    )
                    transport_failure = send_failed or bool(
                        application_error is not None
                        and response_started
                        and status_code < 500
                    )
                    if transport_failure and (
                        snapshot & TerminalFlag.TRANSPORT_FAILURE_EXPLAINED
                    ):
                        log.verbose(
                            "http.request.completed",
                            "HTTP request completed",
                            route_name=route_name,
                            http_method=method,
                            http_status=status_code,
                            duration_ms=duration_ms,
                        )
                    elif transport_failure:
                        log.error(
                            "http.response.failed",
                            "HTTP response failed",
                            error_code="transport_failure",
                            route_name=route_name,
                            http_method=method,
                            http_status=status_code,
                            duration_ms=duration_ms,
                        )
                    elif status_code >= 500 and (
                        snapshot & TerminalFlag.BUSINESS_FAILURE_EXPLAINED
                    ):
                        log.verbose(
                            "http.request.completed",
                            "HTTP request completed",
                            route_name=route_name,
                            http_method=method,
                            http_status=status_code,
                            duration_ms=duration_ms,
                        )
                    elif status_code >= 500 and (snapshot & TerminalFlag.BUSINESS_SEEN):
                        log.error(
                            "http.response.failed",
                            "HTTP response failed",
                            error_code="unexplained_response_failure",
                            route_name=route_name,
                            http_method=method,
                            http_status=status_code,
                            duration_ms=duration_ms,
                        )
                    elif status_code >= 500 or application_error is not None:
                        log.error(
                            "http.request.failed",
                            "HTTP request failed",
                            error_code="unhandled_failure",
                            route_name=route_name,
                            http_method=method,
                            http_status=status_code,
                            duration_ms=duration_ms,
                        )
                    else:
                        log.verbose(
                            "http.request.completed",
                            "HTTP request completed",
                            route_name=route_name,
                            http_method=method,
                            http_status=status_code,
                            duration_ms=duration_ms,
                        )


async def _cancel_task(task: asyncio.Task):
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@asynccontextmanager
async def _lifespan_resources(app: FastAPI):
    base.readiness_tracker.reset()
    app.state.worker_initialized = False
    async with AsyncExitStack() as cleanup:
        await setup_database()
        cleanup.push_async_callback(teardown_database)
        from comet.observability.events import (
            start_event_persistence,
            stop_event_persistence,
        )

        start_event_persistence(str(database.url))
        cleanup.callback(stop_event_persistence)
        cleanup.push_async_callback(shutdown_capability_refreshes)
        cleanup.push_async_callback(shutdown_cache_writes)
        cleanup.push_async_callback(anime_mapper.stop)

        cleanup.callback(shutdown_executor)
        logging_settings = current_settings()
        setup_executor(
            settings.EXECUTOR_MAX_WORKERS,
            logging_settings.LOG_PROFILE.value,
            logging_settings.LOG_FORMAT.value,
            logging_settings.no_color,
        )

        cleanup.push_async_callback(http_client_manager.close)
        await http_client_manager.init()
        cleanup.push_async_callback(shutdown_account_sync_tasks)
        cleanup.push_async_callback(network_manager.close_all)
        cleanup.push_async_callback(torrent_update_queue.stop)
        cleanup.push_async_callback(add_torrent_queue.stop)

        if settings.DOWNLOAD_GENERIC_TRACKERS:
            await download_best_trackers()

        # Load anime ID mapping for enhanced metadata and anime detection
        await anime_mapper.load_anime_mapping()

        # Initialize bandwidth monitoring system
        if settings.PROXY_DEBRID_STREAM:
            cleanup.push_async_callback(bandwidth_monitor.shutdown)
            await bandwidth_monitor.initialize()
        if settings.USENET_ENABLED:
            cleanup.push_async_callback(usenet_operation_monitor.shutdown)
            await usenet_operation_monitor.initialize()

        # Start background cleanup tasks
        cleanup_locks_task = create_detached_task(
            cleanup_expired_locks(),
            name="database.cleanup_locks",
        )
        cleanup.push_async_callback(_cancel_task, cleanup_locks_task)
        cleanup_kodi_task = create_detached_task(
            cleanup_expired_kodi_setup_codes(),
            name="database.cleanup_kodi",
        )
        cleanup.push_async_callback(_cancel_task, cleanup_kodi_task)
        if settings.USENET_ENABLED:
            cleanup_usenet_task = create_detached_task(
                cleanup_expired_usenet_state(),
                name="database.cleanup_usenet",
            )
            cleanup.push_async_callback(_cancel_task, cleanup_usenet_task)
        memory_trimmer.reconfigure(settings)
        memory_trim_task = create_detached_task(
            memory_trimmer.run(),
            name="memory.trim",
        )
        cleanup.push_async_callback(_cancel_task, memory_trim_task)

        # Start background scraper if enabled
        if settings.BACKGROUND_SCRAPER_ENABLED:
            background_scraper.clear_finished_task()
            if not background_scraper.task:
                background_scraper.task = create_detached_task(
                    background_scraper.start(),
                    name="background.scraper",
                )
            cleanup.push_async_callback(background_scraper.stop)

        # Start DMM Ingester if enabled
        if settings.DMM_INGEST_ENABLED:
            dmm_ingester_task = create_detached_task(
                dmm_ingester.start(),
                name="dmm.ingester",
            )
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
        indexer_manager_task = create_detached_task(
            indexer_manager.run(),
            name="indexer.manager",
        )
        cleanup.push_async_callback(indexer_manager.close)
        cleanup.push_async_callback(_cancel_task, indexer_manager_task)

        runtime_registry = RuntimeRegistry(database, settings)
        cleanup.push_async_callback(runtime_registry.withdraw)

        async def runtime_readiness():
            snapshot = await base.current_readiness(
                worker_ready=app.state.worker_initialized
            )
            return {
                "state": snapshot.state,
                "components": snapshot.components,
            }

        runtime_heartbeat_task = create_detached_task(
            runtime_registry.run_heartbeat(
                role="web_worker",
                readiness=runtime_readiness,
            ),
            name="runtime.heartbeat",
        )
        cleanup.push_async_callback(_cancel_task, runtime_heartbeat_task)
        cleanup.push_async_callback(withdraw_command_dispatcher)
        command_dispatcher_task = create_detached_task(
            run_command_dispatcher(),
            name="operator.commands",
        )
        cleanup.push_async_callback(_cancel_task, command_dispatcher_task)
        app.state.worker_initialized = True
        initial_readiness = await base.current_readiness(worker_ready=True)
        base.readiness_tracker.observe(initial_readiness)
        await runtime_registry.heartbeat(
            role="web_worker",
            readiness={
                "state": initial_readiness.state,
                "components": initial_readiness.components,
            },
        )
        try:
            yield
        finally:
            app.state.worker_initialized = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_configured(process_role="web_worker")
    started_at = time.monotonic_ns()
    try:
        async with _lifespan_resources(app):
            log.info(
                "process.ready",
                "Web worker is ready",
                duration_ms=(time.monotonic_ns() - started_at) / 1_000_000,
            )
            try:
                yield
            finally:
                log.verbose(
                    "process.stopping",
                    "Web worker is stopping",
                    duration_ms=(time.monotonic_ns() - started_at) / 1_000_000,
                )
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        log.error(
            "process.failed",
            "Web worker failed",
            error_code="worker_failure",
            duration_ms=(time.monotonic_ns() - started_at) / 1_000_000,
            exc=exc,
        )
        raise


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

fastapi_app = FastAPI(
    title="Comet",
    summary="Stremio's fastest torrent/debrid/usenet search add-on.",
    lifespan=lifespan,
    docs_url=None if STREMIO_API_PREFIX else "/docs",
    openapi_url=None if STREMIO_API_PREFIX else "/openapi.json",
    redoc_url=None,
    openapi_tags=tags_metadata,
)
install_api_error_handlers(fastapi_app)


fastapi_app.include_router(base.router)
fastapi_app.include_router(api_v1.router)
fastapi_app.include_router(config.router)
fastapi_app.include_router(cometnet.router)
fastapi_app.include_router(kodi.router)
fastapi_app.include_router(nzb.export_router)
if settings.PROMETHEUS_ENABLED:
    fastapi_app.include_router(prometheus.router)

if STREMIO_API_PREFIX:
    fastapi_app.include_router(config.router, prefix=STREMIO_API_PREFIX)

stremio_routers = (
    manifest.router,
    nzb.router,
    playback.router,
    usenet_playback.router,
    debrid_sync.router,
    streams_router.streams,
    torznab.router,
    chilllink.router,
)

for stremio_router in stremio_routers:
    fastapi_app.include_router(stremio_router, prefix=STREMIO_API_PREFIX)

install_frontend_assets(fastapi_app)

app = CorrelationMiddleware(
    CORSMiddleware(
        fastapi_app,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )
)
