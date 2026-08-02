from __future__ import annotations

import asyncio
import time
from typing import Annotated, Literal

import orjson
from fastapi import APIRouter, Depends, Path, Query, Request

from comet.api.v1.contracts import (
    ApiSuccess,
    CommandResultData,
    StreamActivityData,
    UsenetArtifactPageData,
    UsenetArtifactPruneData,
    UsenetArtifactView,
    UsenetControlData,
    UsenetEngineRuntimeView,
    UsenetEngineStats,
    UsenetHistoryPageData,
    UsenetHistorySummary,
    UsenetInventorySummary,
    UsenetOperationHistoryView,
    UsenetOperationView,
    UsenetPreparationView,
    UsenetSnapshotData,
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
from comet.services.operator_commands import dispatch_usenet_command
from comet.usenet.artifact_gc import SharedArtifactGarbageCollector

router = APIRouter(prefix="/admin/usenet", tags=["API v1 Usenet"])
_HISTORY_SECONDS = 7 * 24 * 60 * 60
_RUNTIME_STALE_SECONDS = 10
_CANCEL_TIMEOUT_SECONDS = 6
_ARTIFACT_GRACE_SECONDS = 5 * 60

_ELIGIBLE_ARTIFACT_SQL = """
    artifacts.publication_state IN ('published', 'tombstoned')
    AND artifacts.refcount = 0
    AND artifacts.last_used_at < :artifact_cutoff
    AND (
        (
            artifacts.storage_kind = 'nzb'
            AND NOT EXISTS (
                SELECT 1
                FROM nzb_artifact_grants AS grants
                WHERE grants.artifact_sha256 = artifacts.artifact_sha256
            )
        )
        OR (
            artifacts.storage_kind = 'materialized_asset'
            AND NOT EXISTS (
                SELECT 1
                FROM asset_preparation_artifacts AS links
                WHERE links.artifact_sha256 = artifacts.artifact_sha256
            )
            AND NOT EXISTS (
                SELECT 1
                FROM artifact_publication_leases AS publications
                WHERE publications.expires_at >= :now
            )
        )
    )
    AND NOT EXISTS (
        SELECT 1
        FROM artifact_reader_leases AS leases
        WHERE leases.artifact_sha256 = artifacts.artifact_sha256
          AND leases.expires_at >= :now
    )
"""


def _runtime(row) -> UsenetEngineRuntimeView:
    stats = (
        UsenetEngineStats(**orjson.loads(row["stats_json"]))
        if row["stats_json"] is not None
        else None
    )
    return UsenetEngineRuntimeView(
        instance_id=row["instance_id"],
        process_id=row["process_id"],
        healthy=bool(row["healthy"]),
        mode=row["mode"],
        collected_at=row["collected_at"],
        stats=stats,
    )


def _active_operation(row, now: float) -> UsenetOperationView:
    values = dict(row)
    cancel_requested = values.pop("cancel_requested")
    return UsenetOperationView(
        **values,
        duration=max(0, now - row["started_at"]),
        cancellation_pending=bool(cancel_requested),
    )


def _history_operation(row) -> UsenetOperationHistoryView:
    return UsenetOperationHistoryView(**dict(row))


@router.get("/snapshot", response_model=ApiSuccess[UsenetSnapshotData])
async def usenet_snapshot(
    request: Request,
    _session_token: Annotated[str, Depends(require_admin_session)],
):
    now = time.time()
    values = {
        "now": now,
        "artifact_cutoff": now - _ARTIFACT_GRACE_SECONDS,
    }
    runtimes, active, preparations, inventory, history = await asyncio.gather(
        database.fetch_all(
            """
            SELECT *
            FROM usenet_engine_runtimes
            WHERE collected_at >= :cutoff
            ORDER BY instance_id
            """,
            {"cutoff": now - _RUNTIME_STALE_SECONDS},
            force_primary=True,
        ),
        database.fetch_all(
            """
            SELECT *
            FROM usenet_active_operations
            WHERE updated_at >= :cutoff
            ORDER BY started_at DESC, id
            """,
            {"cutoff": now - 30},
            force_primary=True,
        ),
        database.fetch_all(
            """
            SELECT
                preparations.preparation_id AS id,
                preparations.provider_kind,
                candidates.media_id,
                candidates.title,
                preparations.state,
                preparations.created_at,
                preparations.updated_at
            FROM provider_preparations AS preparations
            JOIN rendered_release_candidates AS candidates
              ON candidates.candidate_id = preparations.candidate_id
            WHERE preparations.state != 'terminal'
              AND candidates.transport = 'usenet'
            ORDER BY preparations.updated_at DESC, preparations.preparation_id
            LIMIT 100
            """,
            force_primary=True,
        ),
        database.fetch_one(
            f"""
            SELECT
                COUNT(*) AS artifacts,
                COALESCE(SUM(
                    CASE WHEN artifacts.storage_kind = 'nzb'
                    THEN artifacts.byte_size ELSE 0 END
                ), 0) AS nzb_bytes,
                COALESCE(SUM(
                    CASE WHEN artifacts.storage_kind = 'materialized_asset'
                    THEN artifacts.byte_size ELSE 0 END
                ), 0) AS materialized_bytes,
                (
                    SELECT COUNT(*)
                    FROM artifact_reader_leases
                    WHERE expires_at >= :now
                ) AS active_readers,
                COALESCE(SUM(
                    CASE WHEN {_ELIGIBLE_ARTIFACT_SQL} THEN 1 ELSE 0 END
                ), 0) AS eligible_for_prune
            FROM nzb_artifacts AS artifacts
            """,
            values,
            force_primary=True,
        ),
        database.fetch_one(
            """
            SELECT
                COUNT(*) AS streams_7d,
                COALESCE(SUM(CASE WHEN outcome = 'failed' THEN 1 ELSE 0 END), 0)
                    AS failed_7d,
                COALESCE(SUM(bytes_transferred), 0) AS bytes_7d
            FROM usenet_operation_history
            WHERE finished_at >= :cutoff
            """,
            {"cutoff": now - _HISTORY_SECONDS},
            force_primary=True,
        ),
    )
    inventory = dict(inventory)
    inventory["nzb_bytes"] = int(inventory["nzb_bytes"])
    inventory["materialized_bytes"] = int(inventory["materialized_bytes"])
    history = dict(history)
    history["bytes_7d"] = int(history["bytes_7d"])
    return success_response(
        request,
        UsenetSnapshotData(
            collected_at=now,
            enabled=settings.USENET_ENABLED,
            runtimes=[_runtime(row) for row in runtimes],
            active=[_active_operation(row, now) for row in active],
            preparations=[UsenetPreparationView(**dict(row)) for row in preparations],
            inventory=UsenetInventorySummary(**inventory),
            history=UsenetHistorySummary(**history),
        ),
    )


@router.get("/activity", response_model=ApiSuccess[StreamActivityData])
async def usenet_activity(
    request: Request,
    _session_token: Annotated[str, Depends(require_admin_session)],
    time_range: Annotated[
        StreamActivityRange,
        Query(alias="range"),
    ] = "auto",
):
    now = time.time()
    retention_start = now - _HISTORY_SECONDS
    history_start, active_start = await asyncio.gather(
        database.fetch_val(
            """
            SELECT MIN(started_at)
            FROM usenet_operation_history
            WHERE finished_at >= :retention_start
            """,
            {"retention_start": retention_start},
            force_primary=True,
        ),
        database.fetch_val(
            "SELECT MIN(started_at) FROM usenet_active_operations",
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
    history_rows, active_totals = await asyncio.gather(
        database.fetch_all(
            """
            SELECT
                CAST(FLOOR((finished_at - :start) / :bucket) AS INTEGER)
                    AS bucket_index,
                COALESCE(SUM(bytes_transferred), 0) AS bytes_transferred,
                COALESCE(SUM(CASE WHEN outcome = 'completed' THEN 1 ELSE 0 END), 0)
                    AS completed,
                COALESCE(SUM(CASE WHEN outcome = 'failed' THEN 1 ELSE 0 END), 0)
                    AS failed,
                COALESCE(SUM(CASE WHEN outcome = 'cancelled' THEN 1 ELSE 0 END), 0)
                    AS interrupted
            FROM usenet_operation_history
            WHERE finished_at >= :start AND finished_at < :end
            GROUP BY bucket_index
            ORDER BY bucket_index
            """,
            bucket_values,
            force_primary=True,
        ),
        database.fetch_one(
            """
            SELECT
                COUNT(*) AS active,
                COALESCE(SUM(
                    CASE WHEN started_at >= :start THEN bytes_transferred ELSE 0 END
                ), 0) AS bytes_transferred
            FROM usenet_active_operations
            """,
            {"start": window.started_at},
            force_primary=True,
        ),
    )
    buckets = activity_buckets(window)
    for row in history_rows:
        bucket = buckets[row["bucket_index"]]
        bucket.bytes_transferred = int(row["bytes_transferred"])
        bucket.completed = row["completed"]
        bucket.failed = row["failed"]
        bucket.interrupted = row["interrupted"]
    current = buckets[current_bucket_index(window)]
    current.active = active_totals["active"]
    current.bytes_transferred += int(active_totals["bytes_transferred"])

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


@router.get("/history", response_model=ApiSuccess[UsenetHistoryPageData])
async def usenet_history(
    request: Request,
    _session_token: Annotated[str, Depends(require_admin_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
    outcome: Annotated[
        Literal["completed", "cancelled", "failed"] | None,
        Query(),
    ] = None,
):
    values: dict[str, object] = {
        "limit": limit + 1,
        "cutoff": time.time() - _HISTORY_SECONDS,
    }
    predicates = ["finished_at >= :cutoff"]
    if cursor is not None:
        finished_at, identifier = decode_timestamp_cursor(
            cursor,
            subject="Usenet history",
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
        FROM usenet_operation_history
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
        UsenetHistoryPageData(
            items=[_history_operation(row) for row in page],
            next_cursor=next_cursor,
        ),
    )


@router.get("/artifacts", response_model=ApiSuccess[UsenetArtifactPageData])
async def usenet_artifacts(
    request: Request,
    _session_token: Annotated[str, Depends(require_admin_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
    storage_kind: Annotated[
        Literal["nzb", "materialized_asset"] | None,
        Query(),
    ] = None,
):
    now = time.time()
    values: dict[str, object] = {
        "limit": limit + 1,
        "now": now,
        "artifact_cutoff": now - _ARTIFACT_GRACE_SECONDS,
    }
    predicates = ["1 = 1"]
    if cursor is not None:
        last_used_at, identifier = decode_timestamp_cursor(
            cursor,
            subject="Usenet artifacts",
        )
        values.update({"last_used_at": last_used_at, "cursor_id": identifier})
        predicates.append(
            "(artifacts.last_used_at < :last_used_at OR "
            "(artifacts.last_used_at = :last_used_at "
            "AND artifacts.artifact_sha256 < :cursor_id))"
        )
    if storage_kind is not None:
        values["storage_kind"] = storage_kind
        predicates.append("artifacts.storage_kind = :storage_kind")
    rows = await database.fetch_all(
        f"""
        SELECT
            artifacts.artifact_sha256,
            artifacts.storage_kind,
            artifacts.publication_state,
            artifacts.byte_size,
            artifacts.logical_length,
            artifacts.refcount,
            artifacts.created_at,
            artifacts.last_used_at,
            (
                SELECT COUNT(*)
                FROM artifact_reader_leases AS leases
                WHERE leases.artifact_sha256 = artifacts.artifact_sha256
                  AND leases.expires_at >= :now
            ) AS active_readers,
            CASE WHEN {_ELIGIBLE_ARTIFACT_SQL} THEN 1 ELSE 0 END
                AS eligible_for_prune
        FROM nzb_artifacts AS artifacts
        WHERE {" AND ".join(predicates)}
        ORDER BY artifacts.last_used_at DESC, artifacts.artifact_sha256 DESC
        LIMIT :limit
        """,
        values,
        force_primary=True,
    )
    page = rows[:limit]
    next_cursor = (
        encode_timestamp_cursor(
            page[-1]["last_used_at"],
            page[-1]["artifact_sha256"],
        )
        if len(rows) > limit
        else None
    )
    return success_response(
        request,
        UsenetArtifactPageData(
            items=[
                UsenetArtifactView(
                    **{
                        **dict(row),
                        "eligible_for_prune": bool(row["eligible_for_prune"]),
                    }
                )
                for row in page
            ],
            next_cursor=next_cursor,
        ),
    )


@router.post(
    "/operations/{operation_id}/cancel",
    response_model=ApiSuccess[CommandResultData],
)
async def cancel_usenet_operation(
    request: Request,
    _session_token: Annotated[str, Depends(require_csrf)],
    operation_id: Annotated[
        str,
        Path(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$"),
    ],
):
    operation = await database.fetch_one(
        """
        UPDATE usenet_active_operations
        SET cancel_requested = 1
        WHERE id = :id
        RETURNING instance_id
        """,
        {"id": operation_id},
        force_primary=True,
    )
    if operation is None:
        raise ApiProblem(
            status_code=404,
            code="usenet_operation_not_found",
            message="The Usenet operation is no longer active.",
        )
    deadline = time.monotonic() + _CANCEL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        terminal = await database.fetch_one(
            """
            SELECT outcome
            FROM usenet_operation_history
            WHERE id = :id
            """,
            {"id": operation_id},
            force_primary=True,
        )
        if terminal is not None:
            return success_response(
                request,
                CommandResultData(
                    resource_id=operation_id,
                    outcome=terminal["outcome"],
                ),
            )
        await asyncio.sleep(0.05)
    raise ApiProblem(
        status_code=503,
        code="command_timeout",
        message="The owning Usenet worker did not acknowledge cancellation in time.",
    )


@router.post(
    "/runtimes/{instance_id}/{process_id}/{action}",
    response_model=ApiSuccess[UsenetControlData],
)
async def control_usenet_runtime(
    request: Request,
    _session_token: Annotated[str, Depends(require_csrf)],
    instance_id: Annotated[
        str,
        Path(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$"),
    ],
    process_id: Annotated[int, Path(ge=1)],
    action: Annotated[Literal["drain", "resume"], Path()],
):
    runtime = await database.fetch_one(
        """
        SELECT 1
        FROM usenet_engine_runtimes
        WHERE instance_id = :instance_id
          AND process_id = :process_id
          AND collected_at >= :cutoff
        """,
        {
            "instance_id": instance_id,
            "process_id": process_id,
            "cutoff": time.time() - _RUNTIME_STALE_SECONDS,
        },
        force_primary=True,
    )
    if runtime is None:
        raise ApiProblem(
            status_code=404,
            code="usenet_runtime_not_found",
            message="The Usenet runtime is unavailable.",
        )
    result = await dispatch_usenet_command(
        f"usenet.{action}",
        instance_id=instance_id,
        process_id=process_id,
    )
    if result is None:
        raise ApiProblem(
            status_code=503,
            code="command_timeout",
            message="The Usenet runtime did not acknowledge the command.",
        )
    if result["outcome"] != "succeeded":
        raise ApiProblem(
            status_code=409,
            code=result["error_code"] or "invalid_runtime_state",
            message="The Usenet runtime rejected the command.",
        )
    log.info(
        "usenet.runtime.changed",
        "Usenet runtime state changed",
        operation=action,
        worker_pid=process_id,
    )
    return success_response(
        request,
        UsenetControlData(
            action=action,
            instance_id=instance_id,
            outcome="succeeded",
        ),
    )


@router.post(
    "/artifacts/{artifact_sha256}/prune",
    response_model=ApiSuccess[UsenetArtifactPruneData],
)
async def prune_usenet_artifact(
    request: Request,
    _session_token: Annotated[str, Depends(require_csrf)],
    artifact_sha256: Annotated[
        str,
        Path(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"),
    ],
):
    pruned = await SharedArtifactGarbageCollector(
        settings.USENET_ARTIFACT_DIR,
        database,
    ).prune(artifact_sha256)
    if not pruned:
        raise ApiProblem(
            status_code=409,
            code="artifact_in_use",
            message="The artifact is still referenced or inside its safety window.",
        )
    log.info(
        "usenet.artifact.pruned",
        "Usenet artifact pruned",
        content_id=artifact_sha256,
        operation="prune",
    )
    return success_response(
        request,
        UsenetArtifactPruneData(
            artifact_sha256=artifact_sha256,
            pruned=True,
        ),
    )
