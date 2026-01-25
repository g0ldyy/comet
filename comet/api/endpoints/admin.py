import secrets
import time
import uuid

import orjson
from fastapi import APIRouter, Cookie, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from comet.core.logger import log_capture
from comet.core.models import database, settings
from comet.services.bandwidth import bandwidth_monitor
from comet.utils.formatting import format_bytes

router = APIRouter()
templates = Jinja2Templates("comet/templates")


async def create_admin_session():
    session_id = str(uuid.uuid4())
    created_at = time.time()
    expires_at = created_at + 86400  # 24 hours

    await database.execute(
        """
            INSERT INTO admin_sessions (session_id, created_at, expires_at)
            VALUES (:session_id, :created_at, :expires_at)
        """,
        {"session_id": session_id, "created_at": created_at, "expires_at": expires_at},
    )
    return session_id


async def verify_admin_session(admin_session: str = Cookie(None)):
    if not admin_session:
        return False

    current_time = time.time()

    # Check if session exists and is valid
    session = await database.fetch_one(
        """
            SELECT session_id FROM admin_sessions 
            WHERE session_id = :session_id AND expires_at > :current_time
        """,
        {"session_id": admin_session, "current_time": current_time},
    )

    return session is not None


async def require_admin_auth(admin_session: str = Cookie(None)):
    if not await verify_admin_session(admin_session):
        raise HTTPException(status_code=401, detail="Authentication required")


@router.get(
    "/admin",
    tags=["Admin"],
    summary="Admin Login Page",
    description="Renders the admin login page.",
)
async def admin_root(
    request: Request, admin_session: str = Cookie(None, description="Admin session ID")
):
    if await verify_admin_session(admin_session):
        return RedirectResponse("/admin/dashboard")
    return templates.TemplateResponse("admin_login.html", {"request": request})


@router.post(
    "/admin/login",
    tags=["Admin"],
    summary="Admin Login",
    description="Authenticates the admin user.",
)
async def admin_login(
    request: Request, password: str = Form(..., description="Admin password")
):
    is_correct = secrets.compare_digest(password, settings.ADMIN_DASHBOARD_PASSWORD)

    if not is_correct:
        return templates.TemplateResponse(
            "admin_login.html", {"request": request, "error": "Invalid password"}
        )

    session_id = await create_admin_session()
    response = RedirectResponse("/admin/dashboard", status_code=303)
    response.set_cookie(
        key="admin_session",
        value=session_id,
        httponly=True,
        secure=False,
        samesite="lax",
        max_age=86400,
    )
    return response


@router.get(
    "/admin/dashboard",
    tags=["Admin"],
    summary="Admin Dashboard",
    description="Renders the admin dashboard.",
)
async def admin_dashboard(
    request: Request, admin_session: str = Cookie(None, description="Admin session ID")
):
    try:
        await require_admin_auth(admin_session)
        return templates.TemplateResponse("admin_dashboard.html", {"request": request})
    except HTTPException:
        return RedirectResponse("/admin", status_code=303)


@router.post(
    "/admin/logout",
    tags=["Admin"],
    summary="Admin Logout",
    description="Logs out the admin user.",
)
async def admin_logout(
    admin_session: str = Cookie(None, description="Admin session ID"),
):
    if admin_session:
        # Remove session from database
        await database.execute(
            "DELETE FROM admin_sessions WHERE session_id = :session_id",
            {"session_id": admin_session},
        )

    response = RedirectResponse("/admin", status_code=303)
    response.delete_cookie("admin_session")
    return response


@router.get(
    "/admin/api/connections",
    tags=["Admin"],
    summary="Active Connections",
    description="Returns a list of active connections and bandwidth usage.",
)
async def admin_api_connections(
    admin_session: str = Cookie(None, description="Admin session ID"),
):
    await require_admin_auth(admin_session)
    rows = await database.fetch_all(
        "SELECT id, ip, content, timestamp FROM active_connections ORDER BY timestamp DESC"
    )

    bandwidth_metrics = bandwidth_monitor.get_all_active_connections()
    global_stats = bandwidth_monitor.get_global_stats()

    connections = []
    for row in rows:
        conn_id = row["id"]
        base_connection = {
            "id": conn_id,
            "ip": row["ip"],
            "content": row["content"],
            "timestamp": row["timestamp"],
            "duration": time.time() - row["timestamp"],
            "formatted_time": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(row["timestamp"])
            ),
            "bytes_transferred": 0,
            "bytes_transferred_formatted": "0 B",
            "current_speed": 0,
            "current_speed_formatted": "0 B/s",
            "peak_speed": 0,
            "peak_speed_formatted": "0 B/s",
            "avg_speed_formatted": "0 B/s",
        }

        if conn_id in bandwidth_metrics:
            metrics = bandwidth_metrics[conn_id]
            avg_speed = (
                metrics.bytes_transferred / metrics.duration
                if metrics.duration > 0
                else 0
            )

            base_connection.update(
                {
                    "bytes_transferred": metrics.bytes_transferred,
                    "bytes_transferred_formatted": format_bytes(
                        metrics.bytes_transferred
                    ),
                    "current_speed": metrics.current_speed,
                    "current_speed_formatted": bandwidth_monitor.format_speed(
                        metrics.current_speed
                    ),
                    "peak_speed": metrics.peak_speed,
                    "peak_speed_formatted": bandwidth_monitor.format_speed(
                        metrics.peak_speed
                    ),
                    "avg_speed_formatted": bandwidth_monitor.format_speed(avg_speed),
                }
            )

        connections.append(base_connection)

    return JSONResponse(
        {
            "connections": connections,
            "global_stats": {
                "total_bytes_alltime": global_stats.get("total_bytes_alltime", 0),
                "total_bytes_alltime_formatted": format_bytes(
                    global_stats.get("total_bytes_alltime", 0)
                ),
                "total_bytes_session": global_stats.get("total_bytes_session", 0),
                "total_bytes_session_formatted": format_bytes(
                    global_stats.get("total_bytes_session", 0)
                ),
                "total_current_speed": global_stats.get("total_current_speed", 0),
                "total_current_speed_formatted": bandwidth_monitor.format_speed(
                    global_stats.get("total_current_speed", 0)
                ),
                "active_connections": global_stats.get("active_connections", 0),
                "peak_concurrent": global_stats.get("peak_concurrent", 0),
            },
        }
    )


@router.get(
    "/admin/api/logs",
    tags=["Admin"],
    summary="Application Logs",
    description="Returns a list of recent application logs.",
)
async def admin_api_logs(
    admin_session: str = Cookie(None, description="Admin session ID"), since: float = 0
):
    await require_admin_auth(admin_session)

    # Get logs since the specified timestamp
    all_logs = log_capture.get_logs()
    new_logs = [log for log in all_logs if log["created"] > since]

    return JSONResponse(
        {"logs": new_logs, "total_logs": len(all_logs), "new_logs": len(new_logs)}
    )


@router.get(
    "/admin/api/metrics",
    tags=["Admin"],
    summary="Application Metrics",
    description="Returns application metrics including torrents, searches, and cache stats.",
)
async def admin_api_metrics(
    admin_session: str = Cookie(None, description="Admin session ID"),
):
    if not settings.PUBLIC_METRICS_API:
        await require_admin_auth(admin_session)

    current_time = time.time()

    # Try to get from cache
    cached_metrics = await database.fetch_one(
        "SELECT data, timestamp FROM metrics_cache WHERE id = 1"
    )
    if cached_metrics:
        data = orjson.loads(cached_metrics["data"])
        data["last_updated"] = cached_metrics["timestamp"]
        return JSONResponse(data)

    # Fallback if cache is empty (service hasn't run yet)
    return JSONResponse({
        "status": "loading", 
        "message": "Metrics are being calculated in the background. Please refresh in a moment.",
        "torrents": {"total": 0, "by_tracker": [], "size_distribution": [], "quality": {}, "media_distribution": []},
        "searches": {"total_unique": 0, "last_24h": 0, "last_7d": 0, "last_30d": 0},
        "scrapers": {"active_locks": 0},
        "debrid_cache": {"total": 0, "by_service": []}
    })
