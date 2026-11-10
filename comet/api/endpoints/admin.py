import asyncio
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
from comet.utils.signed_session import (
    derive_session_secret,
    encode_signed_session,
    verify_signed_session,
)
from comet.utils.update import UpdateManager

router = APIRouter()
templates = Jinja2Templates("comet/templates")
background_scraper_start_lock = asyncio.Lock()
_metrics_refresh_task: asyncio.Task | None = None
# (exclusive upper bound in GiB, label); the open-ended last range also holds
# torrents of unknown size.
_SIZE_RANGES = (
    (1, "Under 1GB"),
    (5, "1-5GB"),
    (10, "5-10GB"),
    (20, "10-20GB"),
    (None, "Over 20GB"),
)
_TORRENT_STATS_QUERY = f"""
    SELECT
        tracker,
        COUNT(*) AS count,
        SUM(seeders) AS seeders_sum,
        COUNT(seeders) AS seeders_count,
        MAX(seeders) AS max_seeders,
        MIN(seeders) AS min_seeders,
        SUM(size) AS size_sum,
        COUNT(size) AS size_count,
        MAX(size) AS max_size,
        {", ".join(f"COUNT(*) FILTER (WHERE size < {gb << 30}) AS under_{gb}gb" for gb, _ in _SIZE_RANGES if gb)},
        COUNT(*) FILTER (WHERE season IS NOT NULL) AS series_count
    FROM torrents
    GROUP BY tracker
    ORDER BY count DESC
"""
ADMIN_SESSION_COOKIE = "admin_session"
ADMIN_SESSION_TTL = settings.ADMIN_DASHBOARD_SESSION_TTL
ADMIN_SESSION_SECRET = derive_session_secret(
    settings.ADMIN_DASHBOARD_PASSWORD,
    "admin-dashboard",
)


def _decode_cached_metrics(value):
    try:
        payload = orjson.loads(value)
    except (TypeError, orjson.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


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


def require_admin_auth(
    admin_session: str | None = Cookie(None, description="Admin session token"),
):
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
    return templates.TemplateResponse(request=request, name="admin_login.html")


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
            request=request,
            name="admin_login.html",
            context={"error": "Invalid password"},
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
            request=request,
            name="admin_dashboard.html",
            context={
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
        "SELECT id, ip, content, started_at AS timestamp FROM active_connections ORDER BY started_at DESC"
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

    cached = await database.fetch_one(
        "SELECT payload_json, refreshed_at FROM metrics_cache WHERE id = 1"
    )
    payload = _decode_cached_metrics(cached["payload_json"]) if cached else None
    if payload is None:
        # No snapshot yet: wait on the shared refresh instead of starting another.
        return JSONResponse(await asyncio.shield(_start_metrics_refresh()))

    # Stale-while-revalidate: a recompute can outlast METRICS_CACHE_TTL on large
    # databases, so serve the snapshot and let a single worker refresh it.
    stale_before = time.time() - settings.METRICS_CACHE_TTL
    if cached["refreshed_at"] <= stale_before and await _claim_metrics_refresh(
        stale_before
    ):
        _start_metrics_refresh()
    return JSONResponse(payload)


async def _claim_metrics_refresh(stale_before: float) -> bool:
    """Atomically mark a stale snapshot as refreshing, across all workers."""
    return (
        await database.fetch_one(
            """
            UPDATE metrics_cache SET refreshed_at = :now
            WHERE id = 1 AND refreshed_at <= :stale_before
            RETURNING 1
            """,
            {"now": time.time(), "stale_before": stale_before},
            force_primary=True,
        )
        is not None
    )


def _start_metrics_refresh() -> asyncio.Task:
    """Start the metrics recompute unless this worker is already running one."""
    global _metrics_refresh_task
    if _metrics_refresh_task is None or _metrics_refresh_task.done():
        _metrics_refresh_task = asyncio.create_task(_refresh_metrics())
        _metrics_refresh_task.add_done_callback(_log_metrics_refresh_failure)
    return _metrics_refresh_task


def _log_metrics_refresh_failure(task: asyncio.Task):
    if not task.cancelled() and (error := task.exception()):
        logger.error(f"Metrics refresh failed: {error}")


async def _refresh_metrics() -> dict:
    current_time = time.time()
    started = time.monotonic()

    # 📊 TORRENTS METRICS
    # One scan grouped by tracker; global figures are folded from its rows.
    tracker_rows = await database.fetch_all(_TORRENT_STATS_QUERY)

    def total(column: str) -> int:
        return sum(row[column] for row in tracker_rows)

    def extreme(pick, column: str):
        return pick(
            (row[column] for row in tracker_rows if row[column] is not None),
            default=0,
        )

    def average(rows, sum_column: str, count_column: str) -> float:
        count = sum(row[count_column] for row in rows)
        return (
            float(sum(row[sum_column] or 0 for row in rows)) / count if count else 0.0
        )

    total_torrents = total("count")
    series_count = total("series_count")

    size_distribution = []
    below = 0
    for upper_gb, label in _SIZE_RANGES:
        cumulative = total(f"under_{upper_gb}gb") if upper_gb else total_torrents
        if cumulative > below:
            size_distribution.append({"range": label, "count": cumulative - below})
        below = cumulative

    # 🔍 SEARCH METRICS
    search_metrics = await database.fetch_one(
        """
        SELECT
            COUNT(*) AS total_unique_searches,
            COALESCE(SUM(CASE WHEN first_seen_at >= :time_24h THEN 1 ELSE 0 END), 0) AS searches_24h,
            COALESCE(SUM(CASE WHEN first_seen_at >= :time_7d THEN 1 ELSE 0 END), 0) AS searches_7d,
            COALESCE(SUM(CASE WHEN first_seen_at >= :time_30d THEN 1 ELSE 0 END), 0) AS searches_30d
        FROM media_demand
        """,
        {
            "time_24h": current_time - 86400,
            "time_7d": current_time - 604800,
            "time_30d": current_time - 2592000,
        },
    )
    total_unique_searches = (
        search_metrics["total_unique_searches"] if search_metrics else 0
    )
    searches_24h = search_metrics["searches_24h"] if search_metrics else 0
    searches_7d = search_metrics["searches_7d"] if search_metrics else 0
    searches_30d = search_metrics["searches_30d"] if search_metrics else 0

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
        WHERE updated_at >= :min_timestamp
        GROUP BY debrid_service 
        ORDER BY count DESC
    """,
        {"min_timestamp": current_time - settings.DEBRID_CACHE_TTL},
    )

    metrics_data = {
        "torrents": {
            "total": total_torrents,
            "by_tracker": [
                {
                    "tracker": row["tracker"],
                    "count": row["count"],
                    "avg_seeders": round(
                        average((row,), "seeders_sum", "seeders_count"), 1
                    ),
                    "avg_size_formatted": format_bytes(
                        average((row,), "size_sum", "size_count")
                    ),
                }
                for row in tracker_rows
            ],
            "size_distribution": size_distribution,
            "quality": {
                "avg_seeders": round(
                    average(tracker_rows, "seeders_sum", "seeders_count"), 1
                ),
                "max_seeders": extreme(max, "max_seeders"),
                "min_seeders": extreme(min, "min_seeders"),
                "avg_size_formatted": format_bytes(
                    average(tracker_rows, "size_sum", "size_count")
                ),
                "max_size_formatted": format_bytes(extreme(max, "max_size")),
            },
            "media_distribution": [
                {"type": media_type, "count": count}
                for media_type, count in (
                    ("Series", series_count),
                    ("Movies", total_torrents - series_count),
                )
                if count
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
            INSERT INTO metrics_cache (id, payload_json, refreshed_at) 
            VALUES (1, :payload_json, :refreshed_at)
            ON CONFLICT(id) DO UPDATE SET
                payload_json = :payload_json,
                refreshed_at = :refreshed_at
        """,
        {
            "payload_json": orjson.dumps(metrics_data).decode("utf-8"),
            "refreshed_at": current_time,
        },
    )

    logger.log("API", f"Metrics refreshed in {time.monotonic() - started:.1f}s")
    return metrics_data


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
    "/admin/api/background-scraper/drain",
    tags=["Admin"],
    summary="Stop Background Scraper After Current Run",
    description=(
        "Lets the active background scrape run finish, then stops the orchestrator "
        "before another run starts. Stops immediately when no run is active."
    ),
)
async def admin_background_scraper_drain(
    admin_session: str = Cookie(None, description="Admin session token"),
):
    require_admin_auth(admin_session)
    if not background_scraper.is_running:
        return JSONResponse(
            {
                "success": True,
                "state": "stopped",
                "message": "Background scraper is already stopped",
            }
        )

    scheduled = await background_scraper.drain()
    if scheduled:
        state = "scheduled"
        message = "Background scraper will stop after the current run"
    else:
        state = "stopped"
        message = "Background scraper stopped; no run was active"
    return JSONResponse({"success": True, "state": state, "message": message})


@router.delete(
    "/admin/api/background-scraper/drain",
    tags=["Admin"],
    summary="Cancel Scheduled Background Scraper Stop",
    description="Cancels a pending stop-after-current-run request.",
)
async def admin_background_scraper_cancel_drain(
    admin_session: str = Cookie(None, description="Admin session token"),
):
    require_admin_auth(admin_session)
    cancelled = background_scraper.cancel_drain()
    return JSONResponse(
        {
            "success": True,
            "state": "running" if cancelled else "unchanged",
            "message": (
                "Scheduled background scraper stop cancelled"
                if cancelled
                else "No scheduled background scraper stop"
            ),
        }
    )


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
