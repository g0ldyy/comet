import asyncio
import hashlib
import hmac
import secrets
import time

import orjson
from fastapi import APIRouter, Cookie, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from comet.background_scraper.worker import background_scraper
from comet.core.logger import log_capture, logger
from comet.core.models import database, settings
from comet.services.bandwidth import bandwidth_monitor
from comet.utils.formatting import format_bytes
from comet.utils.signed_session import (encode_signed_session,
                                        verify_signed_session)
from comet.utils.update import UpdateManager

router = APIRouter()
templates = Jinja2Templates("comet/templates")
background_scraper_start_lock = asyncio.Lock()
ADMIN_SESSION_COOKIE = "admin_session"
ADMIN_SESSION_TTL = max(60, settings.ADMIN_DASHBOARD_SESSION_TTL)
ADMIN_SESSION_SECRET = hmac.new(
    settings.ADMIN_DASHBOARD_SESSION_SECRET.encode("utf-8"),
    settings.ADMIN_DASHBOARD_PASSWORD.encode("utf-8"),
    hashlib.sha256,
).digest()


def _handle_background_scraper_task_done(task: asyncio.Task):
    if task.cancelled():
        return

    try:
        error = task.exception()
    except asyncio.CancelledError:
        return
    except Exception as e:
        background_scraper.last_error = str(e)
        logger.error(f"Background scraper task completion handling failed: {e}")
        return

    if error:
        background_scraper.last_error = str(error)
        logger.error(f"Background scraper task failed: {error}")

    if background_scraper.task is task:
        background_scraper.task = None


def create_admin_session():
    return encode_signed_session(secret=ADMIN_SESSION_SECRET, ttl=ADMIN_SESSION_TTL)


def verify_admin_session(admin_session: str | None):
    return verify_signed_session(token=admin_session, secret=ADMIN_SESSION_SECRET)


def require_admin_auth(admin_session: str | None):
    if not verify_admin_session(admin_session):
        raise HTTPException(status_code=401, detail="Authentication required")


@router.get(
    "/admin",
    tags=["Admin"],
    summary="Admin Login Page",
    description="Renders the admin login page.",
)
async def admin_root(
    request: Request,
    admin_session: str = Cookie(None, description="Admin session token"),
):
    if verify_admin_session(admin_session):
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

    session_token = create_admin_session()
    response = RedirectResponse("/admin/dashboard", status_code=303)
    response.set_cookie(
        key=ADMIN_SESSION_COOKIE,
        value=session_token,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        max_age=ADMIN_SESSION_TTL,
    )
    return response


@router.get(
    "/admin/api/update-check",
    tags=["Admin"],
    summary="Check for Updates",
    description="Checks if a new version of Comet is available.",
)
async def update_check(
    admin_session: str = Cookie(None, description="Admin session token"),
):
    require_admin_auth(admin_session)
    return await UpdateManager.check_for_updates()


@router.get(
    "/admin/dashboard",
    tags=["Admin"],
    summary="Admin Dashboard",
    description="Renders the admin dashboard.",
)
async def admin_dashboard(
    request: Request,
    admin_session: str = Cookie(None, description="Admin session token"),
):
    try:
        require_admin_auth(admin_session)
        return templates.TemplateResponse(
            "admin_dashboard.html",
            {
                "request": request,
                "version_info": UpdateManager.get_version_info(),
                "background_scraper_interval": max(
                    1, settings.BACKGROUND_SCRAPER_INTERVAL
                ),
            },
        )
    except HTTPException:
        return RedirectResponse("/admin", status_code=303)


@router.post(
    "/admin/logout",
    tags=["Admin"],
    summary="Admin Logout",
    description="Logs out the admin user.",
)
async def admin_logout():
    response = RedirectResponse("/admin", status_code=303)
    response.delete_cookie(ADMIN_SESSION_COOKIE)
    return response


@router.get(
    "/admin/api/connections",
    tags=["Admin"],
    summary="Active Connections",
    description="Returns a list of active connections and bandwidth usage.",
)
async def admin_api_connections(
    admin_session: str = Cookie(None, description="Admin session token"),
):
    require_admin_auth(admin_session)
    rows = await database.fetch_all(
        "SELECT id, ip, content, timestamp FROM active_connections ORDER BY timestamp DESC"
    )

    bandwidth_metrics = bandwidth_monitor.get_all_active_connections()
    global_stats = bandwidth_monitor.get_global_stats()
    current_time = time.time()

    connections = []
    for row in rows:
        conn_id = row["id"]
        base_connection = {
            "id": conn_id,
            "ip": row["ip"],
            "content": row["content"],
            "timestamp": row["timestamp"],
            "duration": current_time - row["timestamp"],
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
    admin_session: str = Cookie(None, description="Admin session token"),
    since: float = 0,
):
    require_admin_auth(admin_session)

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
    admin_session: str = Cookie(None, description="Admin session token"),
):
    if not settings.PUBLIC_METRICS_API:
        require_admin_auth(admin_session)

    current_time = time.time()

    # Try to get from cache
    cached_metrics = await database.fetch_one(
        "SELECT data, timestamp FROM metrics_cache WHERE id = 1"
    )
    if (
        cached_metrics
        and cached_metrics["timestamp"] + settings.METRICS_CACHE_TTL > current_time
    ):
        return JSONResponse(orjson.loads(cached_metrics["data"]))

    # 📊 TORRENTS METRICS
    total_torrents = await database.fetch_val("SELECT COUNT(*) FROM torrents")

    # Torrents by tracker
    top_trackers = await database.fetch_all("""
        SELECT tracker, COUNT(*) as count, AVG(seeders) as avg_seeders, AVG(size) as avg_size
        FROM torrents 
        GROUP BY tracker 
        ORDER BY count DESC 
    """)

    tracker_stats = []
    for row in top_trackers:
        tracker_stats.append(
            {
                "tracker": row["tracker"],
                "count": row["count"],
                "avg_seeders": row["avg_seeders"],
                "avg_size": row["avg_size"],
            }
        )

    # Size distribution
    size_distribution = await database.fetch_all("""
        SELECT 
            CASE 
                WHEN size < 1073741824 THEN 'Under 1GB'
                WHEN size < 5368709120 THEN '1-5GB'
                WHEN size < 10737418240 THEN '5-10GB'
                WHEN size < 21474836480 THEN '10-20GB'
                ELSE 'Over 20GB'
            END as size_range,
            COUNT(*) as count
        FROM torrents 
        GROUP BY size_range
    """)

    # Top seeders and quality metrics
    quality_stats = await database.fetch_all("""
        SELECT 
            AVG(seeders) as avg_seeders,
            MAX(seeders) as max_seeders,
            MIN(seeders) as min_seeders,
            AVG(size) as avg_size,
            MAX(size) as max_size
        FROM torrents
    """)

    # Media type distribution
    media_distribution = await database.fetch_all("""
        SELECT 
            CASE 
                WHEN season IS NOT NULL THEN 'Series'
                ELSE 'Movies'
            END as media_type,
            COUNT(*) as count
        FROM torrents 
        GROUP BY media_type
    """)

    # 🔍 SEARCH METRICS
    total_unique_searches = await database.fetch_val(
        "SELECT COUNT(*) FROM first_searches"
    )

    # Recent searches (last 24h, 7d, 30d)
    searches_24h = await database.fetch_val(
        "SELECT COUNT(*) FROM first_searches WHERE timestamp >= :time_24h",
        {"time_24h": current_time - 86400},
    )

    searches_7d = await database.fetch_val(
        "SELECT COUNT(*) FROM first_searches WHERE timestamp >= :time_7d",
        {"time_7d": current_time - 604800},
    )

    searches_30d = await database.fetch_val(
        "SELECT COUNT(*) FROM first_searches WHERE timestamp >= :time_30d",
        {"time_30d": current_time - 2592000},
    )

    # 🔧 SCRAPER METRICS
    active_locks = await database.fetch_val(
        "SELECT COUNT(*) FROM scrape_locks WHERE expires_at > :current_time",
        {"current_time": current_time},
    )

    # 💾 DEBRID CACHE METRICS
    total_debrid_cache = await database.fetch_val(
        "SELECT COUNT(*) FROM debrid_availability"
    )

    # Debrid cache by service
    debrid_by_service = await database.fetch_all(
        """
        SELECT debrid_service, COUNT(*) as count, AVG(size) as avg_size, SUM(size) as total_size
        FROM debrid_availability 
        WHERE timestamp >= :min_timestamp
        GROUP BY debrid_service 
        ORDER BY count DESC
    """,
        {"min_timestamp": current_time - settings.DEBRID_CACHE_TTL},
    )

    # Process quality stats
    if quality_stats:
        quality_data = quality_stats[0]
        # PostgreSQL compatibility
        avg_seeders = float(quality_data["avg_seeders"] or 0)
        max_seeders = float(quality_data["max_seeders"] or 0)
        min_seeders = float(quality_data["min_seeders"] or 0)
        avg_size = float(quality_data["avg_size"] or 0)
        max_size = float(quality_data["max_size"] or 0)
    else:
        avg_seeders = max_seeders = min_seeders = avg_size = max_size = 0

    metrics_data = {
        "torrents": {
            "total": total_torrents or 0,
            "by_tracker": [
                {
                    "tracker": row["tracker"],
                    "count": row["count"],
                    "avg_seeders": round(float(row["avg_seeders"] or 0), 1),
                    "avg_size_formatted": format_bytes(row["avg_size"] or 0),
                }
                for row in tracker_stats
            ],
            "size_distribution": [
                {"range": row["size_range"], "count": row["count"]}
                for row in size_distribution
            ],
            "quality": {
                "avg_seeders": round(avg_seeders, 1),
                "max_seeders": int(max_seeders),
                "min_seeders": int(min_seeders),
                "avg_size_formatted": format_bytes(avg_size),
                "max_size_formatted": format_bytes(max_size),
            },
            "media_distribution": [
                {"type": row["media_type"], "count": row["count"]}
                for row in media_distribution
            ],
        },
        "searches": {
            "total_unique": total_unique_searches or 0,
            "last_24h": searches_24h or 0,
            "last_7d": searches_7d or 0,
            "last_30d": searches_30d or 0,
        },
        "scrapers": {
            "active_locks": active_locks or 0,
        },
        "debrid_cache": {
            "total": total_debrid_cache or 0,
            "by_service": [
                {
                    "service": row["debrid_service"],
                    "count": row["count"],
                    "avg_size_formatted": format_bytes(row["avg_size"] or 0),
                    "total_size_formatted": format_bytes(row["total_size"] or 0),
                }
                for row in debrid_by_service
            ],
        },
    }

    # Save to cache
    await database.execute(
        """
            INSERT INTO metrics_cache (id, data, timestamp) 
            VALUES (1, :data, :timestamp)
            ON CONFLICT(id) DO UPDATE SET data = :data, timestamp = :timestamp
        """,
        {"data": orjson.dumps(metrics_data).decode("utf-8"), "timestamp": current_time},
    )

    return JSONResponse(metrics_data)


@router.get(
    "/admin/api/background-scraper/status",
    tags=["Admin"],
    summary="Background Scraper Status",
    description="Returns background scraper runtime status, queue stats, and latest run data.",
)
async def admin_background_scraper_status(
    admin_session: str = Cookie(None, description="Admin session token"),
):
    require_admin_auth(admin_session)
    return JSONResponse(await background_scraper.get_status())


@router.get(
    "/admin/api/background-scraper/runs",
    tags=["Admin"],
    summary="Background Scraper Runs",
    description="Returns recent background scraper runs.",
)
async def admin_background_scraper_runs(
    admin_session: str = Cookie(None, description="Admin session token"),
    limit: int = 20,
):
    require_admin_auth(admin_session)
    safe_limit = max(1, min(limit, 200))
    return JSONResponse(
        {"runs": await background_scraper.get_recent_runs(limit=safe_limit)}
    )


@router.post(
    "/admin/api/background-scraper/start",
    tags=["Admin"],
    summary="Start Background Scraper",
    description="Starts the background scraper orchestrator.",
)
async def admin_background_scraper_start(
    admin_session: str = Cookie(None, description="Admin session token"),
):
    require_admin_auth(admin_session)
    async with background_scraper_start_lock:
        background_scraper.clear_finished_task()
        if not background_scraper.task:
            task = asyncio.create_task(background_scraper.start())
            task.add_done_callback(_handle_background_scraper_task_done)
            background_scraper.task = task
    return JSONResponse({"success": True, "message": "Background scraper starting"})


@router.post(
    "/admin/api/background-scraper/stop",
    tags=["Admin"],
    summary="Stop Background Scraper",
    description="Stops the background scraper orchestrator.",
)
async def admin_background_scraper_stop(
    admin_session: str = Cookie(None, description="Admin session token"),
):
    require_admin_auth(admin_session)
    await background_scraper.stop()
    return JSONResponse({"success": True, "message": "Background scraper stopped"})


@router.post(
    "/admin/api/background-scraper/pause",
    tags=["Admin"],
    summary="Pause Background Scraper",
    description="Pauses the background scraper orchestrator.",
)
async def admin_background_scraper_pause(
    admin_session: str = Cookie(None, description="Admin session token"),
):
    require_admin_auth(admin_session)
    paused = await background_scraper.pause()
    if not paused:
        return JSONResponse(
            {"success": False, "message": "Background scraper is not running"},
            status_code=400,
        )
    return JSONResponse({"success": True, "message": "Background scraper paused"})


@router.post(
    "/admin/api/background-scraper/resume",
    tags=["Admin"],
    summary="Resume Background Scraper",
    description="Resumes the background scraper orchestrator.",
)
async def admin_background_scraper_resume(
    admin_session: str = Cookie(None, description="Admin session token"),
):
    require_admin_auth(admin_session)
    resumed = await background_scraper.resume()
    if not resumed:
        return JSONResponse(
            {"success": False, "message": "Background scraper is not running"},
            status_code=400,
        )
    return JSONResponse({"success": True, "message": "Background scraper resumed"})


@router.post(
    "/admin/api/background-scraper/requeue-dead",
    tags=["Admin"],
    summary="Requeue Dead Background Scraper Entries",
    description="Requeues dead background scraper media items and episodes for retry.",
)
async def admin_background_scraper_requeue_dead(
    admin_session: str = Cookie(None, description="Admin session token"),
):
    require_admin_auth(admin_session)
    requeued = await background_scraper.requeue_dead_items()
    return JSONResponse(
        {
            "success": True,
            "message": "Dead background scraper entries requeued",
            "requeued": requeued,
        }
    )
