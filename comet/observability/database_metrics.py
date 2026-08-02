"""Canonical cached database aggregates for operator analytics."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import orjson
from pydantic import BaseModel, ConfigDict

_PUBLIC_PARTITION = "0" * 64
_refresh_lock = asyncio.Lock()


class DatabaseMetricModel(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True)


class DistributionMetric(DatabaseMetricModel):
    label: str
    count: int


class TorrentInventorySummary(DatabaseMetricModel):
    unique_media: int
    seen_24h: int
    seen_7d: int
    average_size: float | None
    maximum_size: int | None


class TorrentInventoryMetrics(DatabaseMetricModel):
    total: int
    size_distribution: tuple[DistributionMetric, ...]
    media_distribution: tuple[DistributionMetric, ...]
    summary: TorrentInventorySummary


class SearchInventoryMetrics(DatabaseMetricModel):
    total_unique: int
    last_24h: int
    last_7d: int
    last_30d: int


class ScraperInventoryMetrics(DatabaseMetricModel):
    active_locks: int


class DebridServiceMetric(DatabaseMetricModel):
    service: str
    count: int
    average_size: float | None
    total_size: int | None


class DebridInventoryMetrics(DatabaseMetricModel):
    total: int
    by_service: tuple[DebridServiceMetric, ...]


class DatabaseMetricsSnapshot(DatabaseMetricModel):
    collected_at: float
    torrents: TorrentInventoryMetrics
    searches: SearchInventoryMetrics
    scrapers: ScraperInventoryMetrics
    debrid_cache: DebridInventoryMetrics


async def current_database_metrics(
    database: Any,
    *,
    cache_ttl: int,
    debrid_cache_ttl: int,
) -> DatabaseMetricsSnapshot:
    """Return one coherent cached snapshot shared by every operator surface."""

    now = time.time()
    cached = await database.fetch_one(
        "SELECT payload_json, refreshed_at FROM metrics_cache WHERE id = 1"
    )
    if cached is not None and cached["refreshed_at"] + cache_ttl > now:
        return DatabaseMetricsSnapshot.model_validate_json(cached["payload_json"])

    async with _refresh_lock:
        now = time.time()
        cached = await database.fetch_one(
            "SELECT payload_json, refreshed_at FROM metrics_cache WHERE id = 1"
        )
        if cached is not None and cached["refreshed_at"] + cache_ttl > now:
            return DatabaseMetricsSnapshot.model_validate_json(cached["payload_json"])
        snapshot = await _collect_database_metrics(
            database,
            now=now,
            debrid_cache_ttl=debrid_cache_ttl,
        )
        await database.execute(
            """
            INSERT INTO metrics_cache (id, payload_json, refreshed_at)
            VALUES (1, :payload_json, :refreshed_at)
            ON CONFLICT(id) DO UPDATE SET
                payload_json = excluded.payload_json,
                refreshed_at = excluded.refreshed_at
            """,
            {
                "payload_json": orjson.dumps(snapshot.model_dump(mode="json")).decode(),
                "refreshed_at": now,
            },
        )
        return snapshot


async def _collect_database_metrics(
    database: Any,
    *,
    now: float,
    debrid_cache_ttl: int,
) -> DatabaseMetricsSnapshot:
    torrent_rows = await database.fetch_all(
        """
        WITH generic_torrents AS (
            SELECT DISTINCT
                candidate.candidate_id,
                candidate.media_id,
                candidate.scope,
                candidate.byte_size,
                candidate.last_seen_at_ms
            FROM release_candidates AS candidate
            JOIN release_locators AS locator
              ON locator.candidate_id = candidate.candidate_id
            WHERE candidate.visibility_partition = :public_partition
              AND candidate.transport = 'bittorrent'
              AND locator.locator_kind = 'torrent'
              AND locator.tombstoned_at_ms IS NULL
        )
        SELECT
            0 AS sort_order,
            'summary' AS dimension,
            '' AS label,
            COUNT(*) AS count,
            COUNT(DISTINCT media_id) AS unique_media,
            COALESCE(SUM(CASE WHEN last_seen_at_ms >= :time_24h_ms THEN 1 ELSE 0 END), 0) AS seen_24h,
            COALESCE(SUM(CASE WHEN last_seen_at_ms >= :time_7d_ms THEN 1 ELSE 0 END), 0) AS seen_7d,
            AVG(byte_size) AS average_size,
            MAX(byte_size) AS maximum_size
        FROM generic_torrents
        UNION ALL
        SELECT
            1 AS sort_order,
            'size' AS dimension,
            CASE
                WHEN byte_size < 1073741824 THEN 'Under 1GB'
                WHEN byte_size < 5368709120 THEN '1-5GB'
                WHEN byte_size < 10737418240 THEN '5-10GB'
                WHEN byte_size < 21474836480 THEN '10-20GB'
                ELSE 'Over 20GB'
            END AS label,
            COUNT(*) AS count,
            CAST(NULL AS BIGINT) AS unique_media,
            CAST(NULL AS BIGINT) AS seen_24h,
            CAST(NULL AS BIGINT) AS seen_7d,
            CAST(NULL AS DOUBLE PRECISION) AS average_size,
            CAST(NULL AS BIGINT) AS maximum_size
        FROM generic_torrents
        GROUP BY label
        UNION ALL
        SELECT
            2 AS sort_order,
            'media' AS dimension,
            CASE WHEN scope = 'movie' THEN 'Movies' ELSE 'Series' END AS label,
            COUNT(*) AS count,
            CAST(NULL AS BIGINT) AS unique_media,
            CAST(NULL AS BIGINT) AS seen_24h,
            CAST(NULL AS BIGINT) AS seen_7d,
            CAST(NULL AS DOUBLE PRECISION) AS average_size,
            CAST(NULL AS BIGINT) AS maximum_size
        FROM generic_torrents
        GROUP BY label
        ORDER BY sort_order, label
        """,
        {
            "public_partition": _PUBLIC_PARTITION,
            "time_24h_ms": int((now - 86_400) * 1_000),
            "time_7d_ms": int((now - 604_800) * 1_000),
        },
    )
    torrent_summary = torrent_rows[0]
    searches = await database.fetch_one(
        """
        SELECT
            COUNT(*) AS total_unique,
            COALESCE(SUM(CASE WHEN first_seen_at >= :time_24h THEN 1 ELSE 0 END), 0) AS last_24h,
            COALESCE(SUM(CASE WHEN first_seen_at >= :time_7d THEN 1 ELSE 0 END), 0) AS last_7d,
            COALESCE(SUM(CASE WHEN first_seen_at >= :time_30d THEN 1 ELSE 0 END), 0) AS last_30d
        FROM media_demand
        """,
        {
            "time_24h": now - 86_400,
            "time_7d": now - 604_800,
            "time_30d": now - 2_592_000,
        },
    )
    active_locks = await database.fetch_val(
        "SELECT COUNT(*) FROM scrape_locks WHERE expires_at > :now",
        {"now": now},
    )
    debrid_total = await database.fetch_val("SELECT COUNT(*) FROM debrid_availability")
    debrid_rows = await database.fetch_all(
        """
        SELECT
            debrid_service AS service,
            COUNT(*) AS count,
            AVG(size) AS average_size,
            SUM(size) AS total_size
        FROM debrid_availability
        WHERE updated_at >= :minimum_timestamp
        GROUP BY debrid_service
        ORDER BY count DESC
        """,
        {"minimum_timestamp": now - debrid_cache_ttl},
    )

    return DatabaseMetricsSnapshot(
        collected_at=now,
        torrents=TorrentInventoryMetrics(
            total=torrent_summary["count"],
            size_distribution=tuple(
                DistributionMetric(label=row["label"], count=row["count"])
                for row in torrent_rows
                if row["dimension"] == "size"
            ),
            media_distribution=tuple(
                DistributionMetric(label=row["label"], count=row["count"])
                for row in torrent_rows
                if row["dimension"] == "media"
            ),
            summary=TorrentInventorySummary(
                unique_media=torrent_summary["unique_media"],
                seen_24h=torrent_summary["seen_24h"],
                seen_7d=torrent_summary["seen_7d"],
                average_size=(
                    None
                    if torrent_summary["average_size"] is None
                    else float(torrent_summary["average_size"])
                ),
                maximum_size=torrent_summary["maximum_size"],
            ),
        ),
        searches=SearchInventoryMetrics(**dict(searches)),
        scrapers=ScraperInventoryMetrics(active_locks=active_locks),
        debrid_cache=DebridInventoryMetrics(
            total=debrid_total,
            by_service=tuple(
                DebridServiceMetric(
                    service=row["service"],
                    count=row["count"],
                    average_size=(
                        None
                        if row["average_size"] is None
                        else float(row["average_size"])
                    ),
                    total_size=int(row["total_size"]),
                )
                for row in debrid_rows
            ),
        ),
    )


__all__ = (
    "DatabaseMetricsSnapshot",
    "current_database_metrics",
)
