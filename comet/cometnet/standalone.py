"""
CometNet Standalone Server

Runs CometNet as an independent service with HTTP API for receiving
torrent broadcasts from Comet workers. This is the recommended mode
for multi-worker or multi-replica deployments.

Usage:
    python -m comet.cometnet.standalone

Environment Variables:
    COMETNET_LISTEN_PORT: WebSocket port for P2P (default: 8765)
    COMETNET_HTTP_PORT: HTTP API port (default: 8766)
    COMETNET_KEYS_DIR: Directory for node identity keys
    COMETNET_BOOTSTRAP_NODES: List of bootstrap nodes (JSON array)
    COMETNET_MANUAL_PEERS: List of peers to connect to (JSON array)
    COMETNET_TRANSPORT_WEBSOCKET_COMPRESSION_ENABLED: Enable permessage-deflate (default: False)
    COMETNET_API_KEY: API key for authenticating HTTP requests. When omitted,
        a generated in-memory key is printed once in the startup logs.

Security Notes:
    - The standalone service is designed for INTERNAL cluster use only.
    - All endpoints except /health require X-API-Key header.
    - In Docker deployments, keep the HTTP port (8766) internal to the Docker network.
"""

import asyncio
import os
import secrets
import signal
import sys
import time
from contextlib import AsyncExitStack, asynccontextmanager

from comet.core.operator_settings import (
    consume_runtime_restart_request,
    fresh_runtime_environment,
    prepare_effective_settings_environment,
    request_runtime_restart,
)
from comet.observability.logging import (
    bootstrap_failure,
    configuration_invalid,
    configure_entrypoint,
    current_settings,
)

if __name__ == "__main__":
    try:
        prepare_effective_settings_environment()
    except Exception as exc:
        bootstrap_failure(exception=exc, process_role="cometnet")
        raise SystemExit(78) from None
configure_entrypoint(process_role="cometnet")

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

try:
    from comet.core.models import (
        database,
        resolve_standalone_cometnet_api_key,
        settings,
    )
except Exception as exc:
    configuration_invalid(exception=exc)
    raise SystemExit(78) from None

from comet.cometnet.admin_contracts import (
    StandaloneAddMemberRequest as AddMemberRequest,
)
from comet.cometnet.admin_contracts import (
    StandaloneCreateInviteRequest as CreateInviteRequest,
)
from comet.cometnet.admin_contracts import (
    StandaloneCreatePoolRequest as CreatePoolRequest,
)
from comet.cometnet.admin_contracts import (
    StandaloneJoinPoolRequest as JoinPoolRequest,
)
from comet.cometnet.admin_contracts import (
    StandaloneUpdateMemberRoleRequest as UpdateMemberRoleRequest,
)
from comet.cometnet.manager import CometNetService
from comet.cometnet.protocol import TorrentMetadata
from comet.core.database import setup_database, teardown_database
from comet.core.execution import (
    discard_executor,
    install_executor,
    prepare_executor,
    setup_executor,
    shutdown_executor,
)
from comet.core.live_settings import (
    prepare_settings_application,
    record_settings_application,
)
from comet.core.runtime_registry import RuntimeRegistry
from comet.core.settings_policy import COMETNET_SETTING_KEYS
from comet.observability import create_detached_task
from comet.observability.events import start_event_persistence, stop_event_persistence
from comet.observability.logging import configure, current_process_role
from comet.observability.startup import (
    log_cometnet_standalone_configuration,
    log_runtime_starting,
)
from comet.services.operator_commands import run_settings_command_dispatcher
from comet.services.torrent_manager import check_torrents_exist, torrent_update_queue


class StrictRequest(BaseModel):
    model_config = ConfigDict(strict=True)


class BroadcastRequest(StrictRequest):
    """Request model for torrent broadcast endpoint."""

    info_hash: str
    title: str
    size: int
    tracker: str = ""
    imdb_id: str | None = None
    file_index: int | None = None
    seeders: int | None = None
    season: int | None = None
    episode: int | None = None
    sources: list[str] | None = None
    parsed: dict | None = None


class BroadcastBatchRequest(StrictRequest):
    """Request model for batch torrent broadcast."""

    torrents: list[BroadcastRequest]


_api_key, _ = resolve_standalone_cometnet_api_key()


async def _cancel_task(task: asyncio.Task) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def _require_broadcast_media_id(imdb_id: str | None) -> None:
    if not imdb_id:
        raise HTTPException(status_code=400, detail="imdb_id is required")


async def verify_api_key(x_api_key: str = Header(None, alias="X-API-Key")):
    """
    Verify API key.

    The API key is mandatory. All protected endpoints require the X-API-Key header.
    """
    if not x_api_key:
        raise HTTPException(
            status_code=401,
            detail="API key required. Set X-API-Key header.",
        )

    if not secrets.compare_digest(x_api_key, _api_key):
        raise HTTPException(
            status_code=403,
            detail="Invalid API key.",
        )

    return True


class StandaloneCometNet:
    """
    Standalone CometNet server with HTTP API.

    This runs CometNet as an independent service that:
    - Manages P2P connections via WebSocket
    - Exposes HTTP API for Comet workers to submit torrents
    - Can run as a separate container/pod in cluster deployments

    Security:
    - All endpoints except /health require COMETNET_API_KEY authentication.
    - Set X-API-Key with the configured or startup-generated key.
    - When running in Docker, keep port 8766 internal to the Docker network.
    """

    def __init__(
        self,
        ws_port: int = 8765,
        http_port: int = 8766,
        bootstrap_nodes: list[str] | None = None,
        manual_peers: list[str] | None = None,
        max_peers: int = 50,
        min_peers: int = 3,
        keys_dir: str | None = None,
        advertise_url: str | None = None,
    ):
        self.ws_port = ws_port
        self.http_port = http_port

        self.service = self._build_service(
            settings,
            ws_port=ws_port,
            bootstrap_nodes=bootstrap_nodes,
            manual_peers=manual_peers,
            max_peers=max_peers,
            min_peers=min_peers,
            keys_dir=keys_dir,
            advertise_url=advertise_url,
        )

        self._broadcasts_received = 0
        self._broadcasts_success = 0
        self._start_time = time.time()

        self.app = self._create_app()

    @staticmethod
    def _build_service(
        config,
        *,
        ws_port=None,
        bootstrap_nodes=None,
        manual_peers=None,
        max_peers=None,
        min_peers=None,
        keys_dir=None,
        advertise_url=None,
    ) -> CometNetService:
        return CometNetService(
            enabled=True,
            listen_port=config.COMETNET_LISTEN_PORT if ws_port is None else ws_port,
            bootstrap_nodes=(
                config.COMETNET_BOOTSTRAP_NODES
                if bootstrap_nodes is None
                else bootstrap_nodes
            ),
            manual_peers=(
                config.COMETNET_MANUAL_PEERS if manual_peers is None else manual_peers
            ),
            max_peers=config.COMETNET_MAX_PEERS if max_peers is None else max_peers,
            min_peers=config.COMETNET_MIN_PEERS if min_peers is None else min_peers,
            keys_dir=config.COMETNET_KEYS_DIR if keys_dir is None else keys_dir,
            advertise_url=(
                config.COMETNET_ADVERTISE_URL
                if advertise_url is None
                else advertise_url
            ),
        )

    @staticmethod
    def _configure_service(service: CometNetService) -> None:
        service.set_save_torrent_callback(torrent_update_queue.add_network_torrent)
        service.set_check_torrents_exist_callback(check_torrents_exist)

    async def apply_settings(self) -> bool:
        global _api_key

        prepared = await prepare_settings_application(database)
        candidate = prepared.candidate
        next_logging = prepared.logging
        next_executor = (
            prepare_executor(
                candidate.EXECUTOR_MAX_WORKERS,
                next_logging.LOG_PROFILE.value,
                next_logging.LOG_FORMAT.value,
                next_logging.no_color,
            )
            if "EXECUTOR_MAX_WORKERS" in prepared.changed_keys
            or prepared.logging_changed
            else None
        )
        cometnet_changed = not (
            COMETNET_SETTING_KEYS - {"COMETNET_HTTP_PORT"}
        ).isdisjoint(prepared.changed_keys)
        if cometnet_changed:
            replacement = self._build_service(candidate)
            self._configure_service(replacement)
            previous = self.service
            try:
                await previous.stop()
                with settings.bind_snapshot(candidate):
                    await replacement.start()
            except BaseException:
                discard_executor(next_executor)
                with settings.bind_snapshot(prepared.current):
                    await previous.start()
                raise
            self.service = replacement

        if next_executor is not None:
            install_executor(next_executor)
        if prepared.logging_changed:
            configure(next_logging, process_role=current_process_role())
        settings.publish(candidate)
        record_settings_application(prepared.application)
        _api_key = candidate.COMETNET_API_KEY
        if "COMETNET_HTTP_PORT" in prepared.changed_keys:
            create_detached_task(
                self._restart_for_http_port(),
                name="cometnet.http.restart",
            )
        return True

    @staticmethod
    async def _restart_for_http_port() -> None:
        await asyncio.sleep(1)
        request_runtime_restart()
        os.kill(os.getpid(), signal.SIGTERM)

    def _create_app(self) -> FastAPI:
        """Create the FastAPI application with endpoints."""

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            async with AsyncExitStack() as cleanup:
                await setup_database()
                cleanup.push_async_callback(teardown_database)
                start_event_persistence(str(database.url))
                cleanup.callback(stop_event_persistence)

                logging_settings = current_settings()
                setup_executor(
                    settings.EXECUTOR_MAX_WORKERS,
                    logging_settings.LOG_PROFILE.value,
                    logging_settings.LOG_FORMAT.value,
                    logging_settings.no_color,
                )
                cleanup.callback(shutdown_executor)
                cleanup.push_async_callback(torrent_update_queue.stop)

                self.service.set_save_torrent_callback(
                    torrent_update_queue.add_network_torrent
                )
                self.service.set_check_torrents_exist_callback(check_torrents_exist)

                async def stop_current_service():
                    await self.service.stop()

                cleanup.push_async_callback(stop_current_service)
                await self.service.start()

                runtime_registry = RuntimeRegistry(database, settings)
                cleanup.push_async_callback(runtime_registry.withdraw)

                async def readiness():
                    return {
                        "state": "ready" if self.service.running else "starting",
                        "components": {"cometnet": self.service.running},
                    }

                heartbeat = create_detached_task(
                    runtime_registry.run_heartbeat(
                        role="cometnet",
                        readiness=readiness,
                    ),
                    name="cometnet.heartbeat",
                )
                settings_dispatcher = create_detached_task(
                    run_settings_command_dispatcher(self.apply_settings),
                    name="cometnet.settings",
                )
                cleanup.push_async_callback(_cancel_task, settings_dispatcher)
                cleanup.push_async_callback(_cancel_task, heartbeat)

                yield

        app = FastAPI(
            title="CometNet Standalone",
            description="CometNet P2P Network - Standalone Mode",
            version="1.0.0",
            lifespan=lifespan,
            docs_url="/docs",
            redoc_url=None,
        )

        @app.get("/health")
        async def health():
            """Health check endpoint."""
            return {
                "status": "healthy",
                "service": "cometnet-standalone",
                "uptime_seconds": int(time.time() - self._start_time),
                "running": self.service._running,
            }

        @app.get("/stats", dependencies=[Depends(verify_api_key)])
        async def stats():
            """Get CometNet statistics."""
            service_stats = await self.service.get_stats()
            return {
                **service_stats,
                "standalone": {
                    "http_port": self.http_port,
                    "broadcasts_received": self._broadcasts_received,
                    "broadcasts_success": self._broadcasts_success,
                    "uptime_seconds": int(time.time() - self._start_time),
                },
            }

        @app.get("/peers", dependencies=[Depends(verify_api_key)])
        async def peers():
            """Get list of connected peers."""
            return await self.service.get_peers()

        @app.get("/pools", dependencies=[Depends(verify_api_key)])
        async def pools():
            """Get pools information."""
            return await self.service.get_pools()

        @app.post("/pools", dependencies=[Depends(verify_api_key)])
        async def create_pool(request: CreatePoolRequest):
            """Create a new pool."""
            try:
                return await self.service.create_pool(
                    pool_id=request.pool_id,
                    display_name=request.display_name,
                    description=request.description,
                    join_mode=request.join_mode,
                )
            except ValueError as error:
                raise HTTPException(
                    status_code=400, detail="Invalid pool request"
                ) from error
            except PermissionError as error:
                raise HTTPException(
                    status_code=403, detail="Pool operation is forbidden"
                ) from error

        @app.delete("/pools/{pool_id}", dependencies=[Depends(verify_api_key)])
        async def delete_pool(pool_id: str):
            """Delete a pool."""
            if await self.service.delete_pool(pool_id):
                return {"status": "success"}
            raise HTTPException(
                status_code=404, detail="Pool not found or failed to delete"
            )

        @app.post("/pools/{pool_id}/join", dependencies=[Depends(verify_api_key)])
        async def join_pool(pool_id: str, request: JoinPoolRequest):
            """Join a pool using an invite code."""
            success = await self.service.join_pool_with_invite(
                pool_id, request.invite_code, request.node_url
            )
            if not success:
                raise HTTPException(status_code=403, detail="Failed to join pool")
            return {"status": "success"}

        @app.post("/pools/{pool_id}/invite", dependencies=[Depends(verify_api_key)])
        async def create_pool_invite(pool_id: str, request: CreateInviteRequest):
            """Create an invite link for a pool."""
            invite_link = await self.service.create_pool_invite(
                pool_id, request.expires_in, request.max_uses
            )
            if invite_link:
                return {"invite_link": invite_link}
            raise HTTPException(status_code=400, detail="Failed to create invite")

        @app.get("/pools/{pool_id}/invites", dependencies=[Depends(verify_api_key)])
        async def get_pool_invites(pool_id: str):
            """Get active invites for a pool."""
            return await self.service.get_pool_invites(pool_id)

        @app.delete(
            "/pools/{pool_id}/invites/{invite_code}",
            dependencies=[Depends(verify_api_key)],
        )
        async def delete_pool_invite(pool_id: str, invite_code: str):
            """Delete a pool invite."""
            if await self.service.delete_pool_invite(pool_id, invite_code):
                return {"status": "success"}
            raise HTTPException(status_code=400, detail="Failed to delete invite")

        @app.post("/pools/{pool_id}/subscribe", dependencies=[Depends(verify_api_key)])
        async def subscribe_pool(pool_id: str):
            """Subscribe to a pool."""
            if await self.service.subscribe_to_pool(pool_id):
                return {"status": "success"}
            return {"status": "failed"}

        @app.delete(
            "/pools/{pool_id}/subscribe", dependencies=[Depends(verify_api_key)]
        )
        async def unsubscribe_pool(pool_id: str):
            """Unsubscribe from a pool."""
            if await self.service.unsubscribe_from_pool(pool_id):
                return {"status": "success"}
            return {"status": "failed"}

        @app.post("/pools/{pool_id}/members", dependencies=[Depends(verify_api_key)])
        async def add_pool_member(pool_id: str, request: AddMemberRequest):
            """Add a member to a pool."""
            if await self.service.add_pool_member(
                pool_id, request.member_key, request.role
            ):
                return {"status": "success"}
            raise HTTPException(status_code=400, detail="Failed to add member")

        @app.delete(
            "/pools/{pool_id}/members/{member_key}",
            dependencies=[Depends(verify_api_key)],
        )
        async def remove_pool_member(pool_id: str, member_key: str):
            """Remove a member from a pool."""
            if await self.service.remove_pool_member(pool_id, member_key):
                return {"status": "success"}
            raise HTTPException(status_code=400, detail="Failed to remove member")

        @app.get("/pools/{pool_id}", dependencies=[Depends(verify_api_key)])
        async def get_pool_details(pool_id: str):
            """Get detailed information about a pool including all members."""
            pool = await self.service.get_pool_details(pool_id)
            if pool is None:
                raise HTTPException(status_code=404, detail="Pool not found")
            return pool

        @app.patch(
            "/pools/{pool_id}/members/{member_key}/role",
            dependencies=[Depends(verify_api_key)],
        )
        async def update_member_role(
            pool_id: str, member_key: str, request: UpdateMemberRoleRequest
        ):
            """Change a member's role (promote to admin or demote to member)."""
            try:
                if await self.service.update_member_role(
                    pool_id, member_key, request.role
                ):
                    return {"status": "success"}
                raise HTTPException(status_code=400, detail="Failed to update role")
            except PermissionError as error:
                raise HTTPException(
                    status_code=403, detail="Pool operation is forbidden"
                ) from error
            except ValueError as error:
                raise HTTPException(
                    status_code=400, detail="Invalid pool request"
                ) from error

        @app.post("/pools/{pool_id}/leave", dependencies=[Depends(verify_api_key)])
        async def leave_pool(pool_id: str):
            """Leave a pool (self-removal). Any member except creator can leave."""
            try:
                if await self.service.leave_pool(pool_id):
                    return {"status": "success"}
                raise HTTPException(
                    status_code=400, detail="Failed to leave pool (not a member?)"
                )
            except PermissionError as error:
                raise HTTPException(
                    status_code=403, detail="Pool operation is forbidden"
                ) from error
            except ValueError as error:
                raise HTTPException(
                    status_code=400, detail="Invalid pool request"
                ) from error

        @app.post("/broadcast", dependencies=[Depends(verify_api_key)])
        async def broadcast(request: BroadcastRequest):
            """
            Broadcast a single torrent to the P2P network.

            This endpoint is called by Comet workers when they discover new torrents.
            """
            self._broadcasts_received += 1

            if not self.service._running:
                raise HTTPException(
                    status_code=503, detail="CometNet service not running"
                )

            _require_broadcast_media_id(request.imdb_id)

            try:
                metadata = TorrentMetadata(**request.model_dump())

                await self.service.broadcast_torrent(metadata)
                self._broadcasts_success += 1

                return {"status": "queued", "info_hash": request.info_hash}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.post("/broadcast/batch", dependencies=[Depends(verify_api_key)])
        async def broadcast_batch(request: BroadcastBatchRequest):
            """
            Broadcast multiple torrents to the P2P network in batch.

            This is more efficient for bulk broadcasts.
            """
            self._broadcasts_received += len(request.torrents)

            if not self.service._running:
                raise HTTPException(
                    status_code=503, detail="CometNet service not running"
                )

            errors = []
            metadata_batch = []

            for torrent in request.torrents:
                try:
                    _require_broadcast_media_id(torrent.imdb_id)
                    metadata = TorrentMetadata(**torrent.model_dump())
                    metadata_batch.append(metadata)
                except HTTPException as e:
                    errors.append({"info_hash": torrent.info_hash, "error": e.detail})
                except Exception as e:
                    errors.append({"info_hash": torrent.info_hash, "error": str(e)})

            if metadata_batch:
                await self.service.broadcast_torrents(metadata_batch)

            queued = len(metadata_batch)
            self._broadcasts_success += queued

            return {
                "status": "completed",
                "queued": queued,
                "errors": errors,
                "total": len(request.torrents),
            }

        @app.exception_handler(Exception)
        async def generic_exception_handler(request: Request, exc: Exception):
            return JSONResponse(
                status_code=500,
                content={"detail": "Internal server error"},
            )

        return app

    async def run(self):
        """Run the standalone server."""
        config = uvicorn.Config(
            self.app,
            host=None,
            port=self.http_port,
            log_config=None,
            access_log=False,
        )
        server = uvicorn.Server(config)
        await server.serve()


def main() -> int:
    """Main entry point for standalone CometNet."""

    log_runtime_starting(settings)
    log_cometnet_standalone_configuration(settings)
    ws_port = settings.COMETNET_LISTEN_PORT
    http_port = settings.COMETNET_HTTP_PORT

    standalone = StandaloneCometNet(
        ws_port=ws_port,
        http_port=http_port,
        bootstrap_nodes=settings.COMETNET_BOOTSTRAP_NODES,
        manual_peers=settings.COMETNET_MANUAL_PEERS,
        max_peers=settings.COMETNET_MAX_PEERS,
        min_peers=settings.COMETNET_MIN_PEERS,
        keys_dir=settings.COMETNET_KEYS_DIR,
        advertise_url=settings.COMETNET_ADVERTISE_URL,
    )

    try:
        asyncio.run(standalone.run())
    except KeyboardInterrupt:
        return 0
    if consume_runtime_restart_request():
        os.execvpe(
            sys.executable,
            [sys.executable, "-m", "comet.cometnet.standalone"],
            fresh_runtime_environment(),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
