import asyncio
import time
import traceback
import orjson

from comet.core.logger import logger
from comet.core.models import database, settings
from comet.utils.formatting import format_bytes


class MetricsService:
    def __init__(self, interval: int = 60):
        self.interval = interval
        self._running = False
        self._task = None

    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.log("METRICS", f"Metrics background service started (interval: {self.interval}s)")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.log("METRICS", "Metrics background service stopped")

    async def _loop(self):
        while self._running:
            try:
                await self.update_metrics()
            except Exception as e:
                logger.error(f"Error updating metrics: {e}")
                logger.debug(traceback.format_exc())
            
            await asyncio.sleep(self.interval)

    async def update_metrics(self):
        current_time = time.time()

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

        # Recent searches - Optimized Query
        search_stats = await database.fetch_one(
            """
            SELECT 
                SUM(CASE WHEN timestamp >= :time_24h THEN 1 ELSE 0 END) as last_24h,
                SUM(CASE WHEN timestamp >= :time_7d THEN 1 ELSE 0 END) as last_7d,
                COUNT(*) as last_30d
            FROM first_searches
            WHERE timestamp >= :time_30d
            """,
            {
                "time_24h": current_time - 86400,
                "time_7d": current_time - 604800,
                "time_30d": current_time - 2592000,
            },
        )

        searches_24h = search_stats["last_24h"] if search_stats and search_stats["last_24h"] else 0
        searches_7d = search_stats["last_7d"] if search_stats and search_stats["last_7d"] else 0
        searches_30d = search_stats["last_30d"] if search_stats and search_stats["last_30d"] else 0

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
                "last_24h": searches_24h,
                "last_7d": searches_7d,
                "last_30d": searches_30d,
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
            "last_updated": current_time,
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
        
        logger.log("METRICS", "Metrics updated successfully")


metrics_service = MetricsService(interval=settings.METRICS_CACHE_TTL or 60)
