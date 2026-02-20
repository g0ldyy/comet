"""
CometNet API Endpoints

Provides WebSocket endpoint for P2P connections and stats API.
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from comet.cometnet.manager import get_cometnet_service
from comet.core.logger import logger

router = APIRouter(prefix="/cometnet", tags=["CometNet"])


@router.websocket("/ws")
async def cometnet_websocket(websocket: WebSocket):
    """
    WebSocket endpoint for CometNet P2P connections.

    This is the entry point for incoming peer connections.
    """
    service = get_cometnet_service()

    if not service or not service._running:
        await websocket.close(code=1013, reason="CometNet not enabled")
        return

    await websocket.accept()

    try:
        await service.handle_websocket_connection(websocket)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"CometNet WebSocket error: {e}")


@router.get("/health")
async def cometnet_health():
    """
    Health check endpoint for CometNet.

    Returns a simple status for load balancers and monitoring systems.
    HTTP 200 = healthy, HTTP 503 = unhealthy
    """
    service = get_cometnet_service()

    if not service:
        return JSONResponse(
            content={"status": "disabled", "message": "CometNet not initialized"},
            status_code=200,
        )

    if not service._running:
        return JSONResponse(
            content={"status": "stopped", "message": "CometNet not running"},
            status_code=503,
        )

    stats = await service.get_stats()
    connection_stats = stats.get("connection_stats", {})
    connected_peers = connection_stats.get("connected_peers", 0)
    min_peers = service.min_peers

    # Consider unhealthy if we have 0 peers and we expect some
    if connected_peers == 0 and min_peers > 0:
        return JSONResponse(
            content={
                "status": "degraded",
                "message": "No peers connected",
                "connected_peers": 0,
                "uptime_seconds": stats.get("uptime_seconds", 0),
            },
            status_code=200,  # Still 200 - operational but degraded
        )

    return JSONResponse(
        content={
            "status": "healthy",
            "connected_peers": connected_peers,
            "uptime_seconds": stats.get("uptime_seconds", 0),
            "gossip_queue_size": stats.get("gossip_stats", {}).get("queue_size", 0),
        },
        status_code=200,
    )
