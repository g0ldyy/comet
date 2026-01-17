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
    COMETNET_API_KEY: Optional API key for authenticating HTTP requests

Security Notes:
    - The standalone service is designed for INTERNAL cluster use only.
    - If exposed publicly, set COMETNET_API_KEY to protect sensitive endpoints.
    - When API key is set, all endpoints except /health require X-API-Key header.
    - In Docker deployments, keep the HTTP port (8766) internal to the Docker network.
"""

import asyncio
import secrets
import sys
import time
from contextlib import asynccontextmanager
from typing import List, Optional

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from comet.cometnet.manager import CometNetService
from comet.cometnet.protocol import TorrentMetadata
from comet.core.logger import logger
from comet.core.models import settings


class BroadcastRequest(BaseModel):
    """Request model for torrent broadcast endpoint."""

    info_hash: str
    title: str
    size: int
    tracker: str = ""
    imdb_id: Optional[str] = None
    file_index: Optional[int] = None
    seeders: Optional[int] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    sources: Optional[List[str]] = None
    parsed: Optional[dict] = None


class BroadcastBatchRequest(BaseModel):
    """Request model for batch torrent broadcast."""

    torrents: List[BroadcastRequest]


class CreatePoolRequest(BaseModel):
    """Request model for pool creation."""

    pool_id: str
    display_name: str
    description: str = ""
    join_mode: str = "invite"


class JoinPoolRequest(BaseModel):
    """Request model for joining a pool."""

    invite_code: str
    node_url: Optional[str] = None


class CreateInviteRequest(BaseModel):
    """Request model for creating pool invite."""

    expires_in: Optional[int] = None
    max_uses: Optional[int] = None


class AddMemberRequest(BaseModel):
    """Request model for adding a pool member."""

    member_key: str
    role: str = "member"


class UpdateMemberRoleRequest(BaseModel):
    """Request model for updating a member's role."""

    role: str


# API key for authentication (optional, from settings)
_api_key: Optional[str] = getattr(settings, "COMETNET_API_KEY", None) or None


async def verify_api_key(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    """
    Verify API key if COMETNET_API_KEY is configured.

    When API key is set, all protected endpoints require X-API-Key header.
    If no API key is configured, all endpoints are open (for internal cluster use).
    """
    if not _api_key:
        # No API key configured - allow all requests (internal mode)
        return True

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
    - If COMETNET_API_KEY is set, all endpoints except /health require authentication.
    - Set X-API-Key header with the configured API key to access protected endpoints.
    - When running in Docker, keep port 8766 internal to the Docker network.
    """

    def __init__(
        self,
        ws_port: int = 8765,
        http_port: int = 8766,
        bootstrap_nodes: Optional[List[str]] = None,
        manual_peers: Optional[List[str]] = None,
        max_peers: int = 50,
        min_peers: int = 3,
        keys_dir: Optional[str] = None,
        advertise_url: Optional[str] = None,
    ):
        self.ws_port = ws_port
        self.http_port = http_port

        self.service = CometNetService(
            enabled=True,
            listen_port=ws_port,
            bootstrap_nodes=bootstrap_nodes or [],
            manual_peers=manual_peers or [],
            max_peers=max_peers,
            min_peers=min_peers,
            keys_dir=keys_dir,
            advertise_url=advertise_url,
        )

        self._broadcasts_received = 0
        self._broadcasts_success = 0
        self._start_time = time.time()

        self.app = self._create_app()

    def _create_app(self) -> FastAPI:
        """Create the FastAPI application with endpoints."""

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            await self.service.start()
            logger.log(
                "COMETNET",
                f"Standalone server started - WS:{self.ws_port} HTTP:{self.http_port}",
            )

            yield

            await self.service.stop()
            logger.log("COMETNET", "Standalone server stopped")

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
            except Exception as e:
                raise HTTPException(status_code=400, detail=str(e))

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
            try:
                success = await self.service.join_pool_with_invite(
                    pool_id, request.invite_code, request.node_url
                )
                if not success:
                    raise HTTPException(status_code=403, detail="Failed to join pool")
                return {"status": "success"}
            except Exception as e:
                raise HTTPException(status_code=400, detail=str(e))

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
            except PermissionError as e:
                raise HTTPException(status_code=403, detail=str(e))
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))

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

            try:
                metadata = TorrentMetadata(**request.model_dump())

                await self.service.broadcast_torrent(metadata)
                self._broadcasts_success += 1
                logger.log(
                    "COMETNET",
                    f"HTTP Broadcast: {request.title} ({request.info_hash[:8]}...)",
                )

                return {"status": "queued", "info_hash": request.info_hash}

            except Exception as e:
                logger.warning(f"Failed to broadcast torrent: {e}")
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

            queued = 0
            errors = []

            for torrent in request.torrents:
                try:
                    metadata = TorrentMetadata(**torrent.model_dump())

                    await self.service.broadcast_torrent(metadata)
                    queued += 1
                    self._broadcasts_success += 1

                except Exception as e:
                    errors.append({"info_hash": torrent.info_hash, "error": str(e)})

            if queued > 0:
                logger.log(
                    "COMETNET", f"HTTP Batch Broadcast: Queued {queued} torrents"
                )

            return {
                "status": "completed",
                "queued": queued,
                "errors": errors,
                "total": len(request.torrents),
            }

        @app.exception_handler(Exception)
        async def generic_exception_handler(request: Request, exc: Exception):
            logger.warning(f"Unhandled exception: {exc}")
            return JSONResponse(
                status_code=500,
                content={"detail": "Internal server error"},
            )

        return app

    async def run(self):
        """Run the standalone server."""
        config = uvicorn.Config(
            self.app,
            host="0.0.0.0",
            port=self.http_port,
            log_config=None,
            access_log=False,
        )
        server = uvicorn.Server(config)
        await server.serve()


def main():
    """Main entry point for standalone CometNet."""
    logger.log("COMETNET", "=" * 60)
    logger.log("COMETNET", "Starting CometNet Standalone Server")
    logger.log("COMETNET", "=" * 60)

    ws_port = settings.COMETNET_LISTEN_PORT
    http_port = settings.COMETNET_HTTP_PORT

    logger.log("COMETNET", f"WebSocket P2P Port: {ws_port}")
    logger.log("COMETNET", f"HTTP API Port: {http_port}")
    logger.log("COMETNET", f"API Key: {'Set' if _api_key else 'Not Set'}")
    logger.log("COMETNET", f"Keys Directory: {settings.COMETNET_KEYS_DIR}")
    logger.log("COMETNET", f"Bootstrap Nodes: {len(settings.COMETNET_BOOTSTRAP_NODES)}")
    logger.log("COMETNET", f"Manual Peers: {len(settings.COMETNET_MANUAL_PEERS)}")
    logger.log("COMETNET", f"Max Peers: {settings.COMETNET_MAX_PEERS}")

    if settings.COMETNET_ADVERTISE_URL:
        logger.log("COMETNET", f"Advertise URL: {settings.COMETNET_ADVERTISE_URL}")

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
        logger.log("COMETNET", "Shutdown complete")
        sys.exit(0)


if __name__ == "__main__":
    main()
