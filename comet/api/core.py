import random
import string
import secrets
import uuid
import time
import re

from loguru import logger as loguru_logger

from fastapi import APIRouter, Request, HTTPException, Form, Cookie
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic

from comet.utils.models import settings, web_config, database
from comet.utils.general import config_check
from comet.utils.log_levels import get_level_info
from comet.utils.bandwidth_monitor import bandwidth_monitor
from comet.debrid.manager import get_debrid_extension

templates = Jinja2Templates("comet/templates")
main = APIRouter()
security = HTTPBasic()


class LogCapture:
    def __init__(self):
        self.logs = []
        self.max_logs = 1000

    def add_log(self, record):
        # Format the log record similar to loguru format
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created))
        level_name = record.levelname

        # Handle special loguru levels
        if hasattr(record, "extra") and "level_name" in record.extra:
            level_name = record.extra["level_name"]

        level_info = get_level_info(level_name)

        log_entry = {
            "timestamp": timestamp,
            "level": level_name,
            "icon": level_info["icon"],
            "color": level_info["color"],
            "module": getattr(record, "module", "unknown"),
            "function": getattr(record, "funcName", "unknown"),
            "message": record.getMessage(),
            "created": record.created,
        }

        self.logs.append(log_entry)
        if len(self.logs) > self.max_logs:
            self.logs.pop(0)

    def get_logs(self):
        return self.logs


# Global log capture instance
log_capture = LogCapture()


# Loguru handler to capture logs
class LoguruHandler:
    def __init__(self, log_capture):
        self.log_capture = log_capture

    def write(self, message):
        if message.strip():
            # Try to extract timestamp, level, module, function, and message
            # This is a simplified parser for loguru format
            pattern = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| ([🌠👾👻🎬🔒📰🕸️⚠️❌💀]?) ?(\w+) \| (\w+)\.(\w+) - (.+)"
            match = re.match(pattern, message.strip())

            if match:
                timestamp_str, icon, level, module, function, msg = match.groups()

                level_info = get_level_info(level)

                log_entry = {
                    "timestamp": timestamp_str,
                    "level": level,
                    "icon": icon or level_info["icon"],
                    "color": level_info["color"],
                    "module": module,
                    "function": function,
                    "message": msg,
                    "created": time.time(),
                }

                self.log_capture.add_log_entry(log_entry)


# Add method to log capture to handle parsed entries
def add_log_entry_to_capture(self, log_entry):
    self.logs.append(log_entry)
    if len(self.logs) > self.max_logs:
        self.logs.pop(0)


# Monkey patch the method
log_capture.add_log_entry = add_log_entry_to_capture.__get__(log_capture, LogCapture)

# Set up loguru handler
loguru_handler = LoguruHandler(log_capture)

# Add our handler to loguru
loguru_logger.add(
    loguru_handler.write,
    level="DEBUG",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level.icon} {level} | {module}.{function} - {message}",
)


async def create_admin_session() -> str:
    session_id = str(uuid.uuid4())
    created_at = time.time()
    expires_at = created_at + 10  # 24 hours

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

    # First, clean up expired sessions
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


@main.get("/")
async def root():
    return RedirectResponse("/configure")


@main.get("/health")
async def health():
    return {"status": "ok"}


@main.get("/configure")
@main.get("/{b64config}/configure")
async def configure(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "CUSTOM_HEADER_HTML": settings.CUSTOM_HEADER_HTML
            if settings.CUSTOM_HEADER_HTML
            else "",
            "webConfig": web_config,
            "proxyDebridStream": settings.PROXY_DEBRID_STREAM,
        },
    )


@main.get("/manifest.json")
@main.get("/{b64config}/manifest.json")
async def manifest(request: Request, b64config: str = None):
    base_manifest = {
        "id": f"{settings.ADDON_ID}.{''.join(random.choice(string.ascii_letters) for _ in range(4))}",
        "description": "Stremio's fastest torrent/debrid search add-on.",
        "version": "2.0.0",
        "catalogs": [],
        "resources": [
            {
                "name": "stream",
                "types": ["movie", "series"],
                "idPrefixes": ["tt", "kitsu"],
            }
        ],
        "types": ["movie", "series", "anime", "other"],
        "logo": "https://i.imgur.com/jmVoVMu.jpeg",
        "background": "https://i.imgur.com/WwnXB3k.jpeg",
        "behaviorHints": {"configurable": True, "configurationRequired": False},
    }

    config = config_check(b64config)
    if not config:
        base_manifest["name"] = "❌ | Comet"
        base_manifest["description"] = (
            f"⚠️ OBSOLETE CONFIGURATION, PLEASE RE-CONFIGURE ON {request.url.scheme}://{request.url.netloc} ⚠️"
        )
        return base_manifest

    debrid_extension = get_debrid_extension(config["debridService"])
    base_manifest["name"] = (
        f"{settings.ADDON_NAME}{(' | ' + debrid_extension) if debrid_extension is not None else ''}"
    )

    return base_manifest


@main.get("/admin")
async def admin_root(request: Request, admin_session: str = Cookie(None)):
    if await verify_admin_session(admin_session):
        return RedirectResponse("/admin/dashboard")
    return templates.TemplateResponse("admin_login.html", {"request": request})


@main.post("/admin/login")
async def admin_login(request: Request, password: str = Form(...)):
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


@main.get("/admin/dashboard")
async def admin_dashboard(request: Request, admin_session: str = Cookie(None)):
    try:
        await require_admin_auth(admin_session)
        return templates.TemplateResponse("admin_dashboard.html", {"request": request})
    except HTTPException:
        return RedirectResponse("/admin", status_code=303)


@main.post("/admin/logout")
async def admin_logout(admin_session: str = Cookie(None)):
    if admin_session:
        # Remove session from database
        await database.execute(
            "DELETE FROM admin_sessions WHERE session_id = :session_id",
            {"session_id": admin_session},
        )

    response = RedirectResponse("/admin", status_code=303)
    response.delete_cookie("admin_session")
    return response


@main.get("/admin/api/connections")
async def admin_api_connections(admin_session: str = Cookie(None)):
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
                    "bytes_transferred_formatted": bandwidth_monitor.format_bytes(
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
                "total_bytes_alltime_formatted": bandwidth_monitor.format_bytes(
                    global_stats.get("total_bytes_alltime", 0)
                ),
                "total_bytes_session": global_stats.get("total_bytes_session", 0),
                "total_bytes_session_formatted": bandwidth_monitor.format_bytes(
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


@main.get("/admin/api/logs")
async def admin_api_logs(admin_session: str = Cookie(None), since: float = 0):
    await require_admin_auth(admin_session)

    # Get logs since the specified timestamp
    all_logs = log_capture.get_logs()
    new_logs = [log for log in all_logs if log["created"] > since]

    return JSONResponse(
        {"logs": new_logs, "total_logs": len(all_logs), "new_logs": len(new_logs)}
    )


@main.get("/admin/api/metrics")
async def admin_api_metrics(admin_session: str = Cookie(None)):
    if not settings.PUBLIC_METRICS_API:
        await require_admin_auth(admin_session)

    current_time = time.time()

    # 📊 TORRENTS METRICS
    total_torrents = await database.fetch_val("SELECT COUNT(*) FROM torrents")

    # Torrents by tracker
    tracker_stats = await database.fetch_all("""
        SELECT tracker, COUNT(*) as count, AVG(seeders) as avg_seeders, AVG(size) as avg_size
        FROM torrents 
        GROUP BY tracker 
        ORDER BY count DESC 
        LIMIT 5
    """)

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
        WHERE timestamp + :cache_ttl >= :current_time
        GROUP BY debrid_service 
        ORDER BY count DESC
    """,
        {"cache_ttl": settings.DEBRID_CACHE_TTL, "current_time": current_time},
    )

    # Format helper function
    def format_bytes(bytes_value):
        if bytes_value is None:
            return "0 B"

        # PostgreSQL compatibility
        bytes_value = float(bytes_value)

        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if bytes_value < 1024.0:
                return f"{bytes_value:.1f} {unit}"
            bytes_value /= 1024.0
        return f"{bytes_value:.1f} PB"

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

    return JSONResponse(
        {
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
    )
