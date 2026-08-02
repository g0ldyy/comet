from __future__ import annotations

import asyncio
import time
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, Request

from comet.api.v1.contracts import (
    ApiSuccess,
    CommandResultData,
    ProxyConnectionView,
    ProxyHistoryEntry,
    ProxyHistoryPageData,
    ProxySnapshotData,
    ProxySummary,
    StreamActivityData,
)
from comet.api.v1.cursors import decode_timestamp_cursor, encode_timestamp_cursor
from comet.api.v1.responses import ApiProblem, success_response
from comet.api.v1.security import require_admin_session, require_csrf
from comet.api.v1.stream_activity import (
    StreamActivityRange,
    activity_buckets,
    activity_window,
    current_bucket_index,
    earliest_timestamp,
)
from comet.core.models import database, settings
from comet.observability import log

router = APIRouter(prefix="/admin/proxy", tags=["API v1 Proxy"])
_HISTORY_WINDOW_SECONDS = 7 * 24 * 60 * 60
_CANCEL_TIMEOUT_SECONDS = 3


def _history_entry(row) -> ProxyHistoryEntry:
    return ProxyHistoryEntry(**dict(row))


@router.get(
    "/snapshot",
    response_model=ApiSuccess[ProxySnapshotData],
)
async def proxy_snapshot(
    request: Request,
    _session_token: Annotated[str, Depends(require_admin_session)],
):
    now = time.time()
    start = now - _HISTORY_WINDOW_SECONDS
    active_rows, totals, history_summary = await asyncio.gather(
        database.fetch_all(
            """
            SELECT *
            FROM active_connections
            ORDER BY started_at DESC, id
            """,
            force_primary=True,
        ),
        database.fetch_one(
            """
            SELECT
                COALESCE(SUM(current_speed), 0) AS current_speed,
                COALESCE(SUM(bytes_transferred), 0) AS session_bytes,
                COUNT(*) AS active_connections,
                COALESCE((
                    SELECT total_bytes FROM bandwidth_stats WHERE id = 1
                ), 0) AS all_time_bytes
            FROM active_connections
            """,
            force_primary=True,
        ),
        database.fetch_one(
            """
            SELECT
                COALESCE(SUM(CASE WHEN outcome = 'completed' THEN 1 ELSE 0 END), 0)
                    AS completed,
                COALESCE(SUM(CASE WHEN outcome = 'failed' THEN 1 ELSE 0 END), 0)
                    AS failed,
                COALESCE(SUM(bytes_transferred), 0) AS bytes_transferred,
                COALESCE(AVG(duration), 0) AS average_duration
            FROM proxy_connection_history
            WHERE finished_at >= :start
            """,
            {"start": start},
            force_primary=True,
        ),
    )
    active = [
        ProxyConnectionView(
            id=row["id"],
            ip=row["ip"],
            content=row["content"],
            service=row["service"],
            instance_id=row["instance_id"],
            process_id=row["process_id"],
            started_at=row["started_at"],
            updated_at=row["updated_at"],
            duration=max(0, now - row["started_at"]),
            bytes_transferred=row["bytes_transferred"],
            current_speed=row["current_speed"],
            average_speed=(
                row["bytes_transferred"] / (now - row["started_at"])
                if now > row["started_at"]
                else 0
            ),
            peak_speed=row["peak_speed"],
            cancellation_pending=bool(row["cancel_requested"]),
        )
        for row in active_rows
    ]
    return success_response(
        request,
        ProxySnapshotData(
            collected_at=now,
            enabled=settings.PROXY_DEBRID_STREAM,
            summary=ProxySummary(
                active_connections=totals["active_connections"],
                current_speed=totals["current_speed"],
                session_bytes=int(totals["session_bytes"]),
                all_time_bytes=totals["all_time_bytes"],
                completed_7d=history_summary["completed"],
                failed_7d=history_summary["failed"],
                bytes_7d=int(history_summary["bytes_transferred"]),
                average_duration_7d=history_summary["average_duration"],
            ),
            active=active,
        ),
    )


@router.get(
    "/activity",
    response_model=ApiSuccess[StreamActivityData],
)
async def proxy_activity(
    request: Request,
    _session_token: Annotated[str, Depends(require_admin_session)],
    time_range: Annotated[
        StreamActivityRange,
        Query(alias="range"),
    ] = "auto",
):
    now = time.time()
    retention_start = now - _HISTORY_WINDOW_SECONDS
    history_start, active_start = await asyncio.gather(
        database.fetch_val(
            """
            SELECT MIN(started_at)
            FROM proxy_connection_history
            WHERE finished_at >= :retention_start
            """,
            {"retention_start": retention_start},
            force_primary=True,
        ),
        database.fetch_val(
            "SELECT MIN(started_at) FROM active_connections",
            force_primary=True,
        ),
    )
    first_activity = earliest_timestamp(history_start, active_start)
    window = activity_window(now, time_range, first_activity)
    bucket_values = {
        "start": window.started_at,
        "end": window.ended_at,
        "bucket": window.bucket_seconds,
    }
    history_rows, concurrency_rows, active_rows = await asyncio.gather(
        database.fetch_all(
            """
            SELECT
                CAST(FLOOR((finished_at - :start) / :bucket) AS INTEGER)
                    AS bucket_index,
                MIN(started_at) AS earliest_started_at,
                COALESCE(SUM(bytes_transferred), 0) AS bytes_transferred,
                COALESCE(SUM(CASE WHEN outcome = 'completed' THEN 1 ELSE 0 END), 0)
                    AS completed,
                COALESCE(SUM(CASE WHEN outcome = 'failed' THEN 1 ELSE 0 END), 0)
                    AS failed,
                COALESCE(SUM(
                    CASE WHEN outcome IN ('cancelled', 'inactive') THEN 1 ELSE 0 END
                ), 0) AS interrupted
            FROM proxy_connection_history
            WHERE finished_at >= :start AND finished_at < :end
            GROUP BY bucket_index
            ORDER BY bucket_index
            """,
            bucket_values,
            force_primary=True,
        ),
        database.fetch_all(
            """
            WITH sample_totals AS (
                SELECT sampled_at, SUM(active_connections) AS active_connections
                FROM proxy_traffic_samples
                WHERE sampled_at >= :start AND sampled_at < :end
                GROUP BY sampled_at
            )
            SELECT
                CAST(FLOOR((sampled_at - :start) / :bucket) AS INTEGER)
                    AS bucket_index,
                MAX(active_connections) AS peak_active
            FROM sample_totals
            GROUP BY bucket_index
            ORDER BY bucket_index
            """,
            bucket_values,
            force_primary=True,
        ),
        database.fetch_all(
            """
            SELECT started_at, bytes_transferred
            FROM active_connections
            """,
            force_primary=True,
        ),
    )
    buckets = activity_buckets(window)
    for row in history_rows:
        finished_index = row["bucket_index"]
        bucket = buckets[finished_index]
        bucket.bytes_transferred = int(row["bytes_transferred"])
        bucket.completed = row["completed"]
        bucket.failed = row["failed"]
        bucket.interrupted = row["interrupted"]
        started_index = int(
            (max(row["earliest_started_at"], window.started_at) - window.started_at)
            / window.bucket_seconds
        )
        for index in range(started_index, finished_index + 1):
            buckets[index].peak_active = max(buckets[index].peak_active or 0, 1)
    for row in concurrency_rows:
        bucket = buckets[row["bucket_index"]]
        bucket.peak_active = max(bucket.peak_active or 0, row["peak_active"])
    for index, bucket in enumerate(buckets):
        bucket_end = min(bucket.started_at + window.bucket_seconds, window.ended_at)
        active = sum(row["started_at"] < bucket_end for row in active_rows)
        if active:
            bucket.peak_active = max(bucket.peak_active or 0, active)
    current = buckets[current_bucket_index(window)]
    current.active = len(active_rows)
    current.bytes_transferred += sum(
        row["bytes_transferred"]
        for row in active_rows
        if row["started_at"] >= window.started_at
    )
    current.peak_active = max(current.peak_active or 0, current.active)

    return success_response(
        request,
        StreamActivityData(
            collected_at=now,
            selection=time_range,
            activity_started_at=first_activity,
            window_started_at=window.started_at,
            window_ended_at=window.ended_at,
            bucket_seconds=window.bucket_seconds,
            buckets=buckets,
        ),
    )


@router.get(
    "/history",
    response_model=ApiSuccess[ProxyHistoryPageData],
)
async def proxy_history(
    request: Request,
    _session_token: Annotated[str, Depends(require_admin_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
    outcome: Annotated[
        Literal["completed", "cancelled", "failed", "inactive"] | None,
        Query(),
    ] = None,
):
    values: dict[str, object] = {
        "limit": limit + 1,
        "cutoff": time.time() - _HISTORY_WINDOW_SECONDS,
    }
    predicates = ["finished_at >= :cutoff"]
    if cursor is not None:
        finished_at, identifier = decode_timestamp_cursor(
            cursor,
            subject="proxy history",
        )
        values.update({"finished_at": finished_at, "cursor_id": identifier})
        predicates.append(
            "(finished_at < :finished_at OR "
            "(finished_at = :finished_at AND id < :cursor_id))"
        )
    if outcome is not None:
        values["outcome"] = outcome
        predicates.append("outcome = :outcome")
    rows = await database.fetch_all(
        f"""
        SELECT *
        FROM proxy_connection_history
        WHERE {" AND ".join(predicates)}
        ORDER BY finished_at DESC, id DESC
        LIMIT :limit
        """,
        values,
        force_primary=True,
    )
    page = rows[:limit]
    next_cursor = (
        encode_timestamp_cursor(page[-1]["finished_at"], page[-1]["id"])
        if len(rows) > limit
        else None
    )
    return success_response(
        request,
        ProxyHistoryPageData(
            items=[_history_entry(row) for row in page],
            next_cursor=next_cursor,
        ),
    )


@router.post(
    "/connections/{connection_id}/cancel",
    response_model=ApiSuccess[CommandResultData],
)
async def cancel_proxy_connection(
    request: Request,
    _session_token: Annotated[str, Depends(require_csrf)],
    connection_id: Annotated[
        str,
        Path(
            pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        ),
    ],
):
    connection = await database.fetch_one(
        """
        UPDATE active_connections
        SET cancel_requested = 1
        WHERE id = :id
        RETURNING instance_id, process_id
        """,
        {"id": connection_id},
        force_primary=True,
    )
    if connection is None:
        raise ApiProblem(
            status_code=404,
            code="proxy_connection_not_found",
            message="The proxy connection is no longer active.",
        )
    deadline = time.monotonic() + _CANCEL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        terminal = await database.fetch_one(
            """
            SELECT outcome
            FROM proxy_connection_history
            WHERE id = :id
            """,
            {"id": connection_id},
            force_primary=True,
        )
        if terminal is not None:
            log.info(
                "proxy.connection.cancelled",
                "Proxy connection cancellation acknowledged",
                content_id=connection_id,
                operation="cancel",
                outcome={
                    "completed": "ok",
                    "cancelled": "cancelled",
                    "failed": "failed",
                    "inactive": "skipped",
                }[terminal["outcome"]],
                worker_pid=connection["process_id"],
            )
            return success_response(
                request,
                CommandResultData(
                    resource_id=connection_id,
                    outcome=terminal["outcome"],
                ),
            )
        await asyncio.sleep(0.05)
    raise ApiProblem(
        status_code=503,
        code="command_timeout",
        message="The owning proxy worker did not acknowledge cancellation in time.",
    )
