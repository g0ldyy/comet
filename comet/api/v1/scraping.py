from __future__ import annotations

import time
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, Request

from comet.api.v1.contracts import (
    ApiSuccess,
    ScraperControlData,
    ScraperQueueEntry,
    ScraperQueueMutationData,
    ScraperQueuePageData,
    ScraperQueueSummary,
    ScraperRunsPageData,
    ScraperRuntimeView,
    ScraperRunView,
    ScrapingSnapshotData,
)
from comet.api.v1.cursors import decode_timestamp_cursor, encode_timestamp_cursor
from comet.api.v1.responses import ApiProblem, success_response
from comet.api.v1.security import require_admin_session, require_csrf
from comet.background_scraper.worker import (
    BACKGROUND_SCRAPER_RUNS_PROJECTION,
    background_scraper,
)
from comet.core.models import database, settings
from comet.observability import log
from comet.services.operator_commands import dispatch_scraper_command

router = APIRouter(prefix="/admin/scraping", tags=["API v1 Scraping"])
QueueKind = Literal["item", "episode"]
QueueAction = Literal["retry", "defer", "abandon"]
ControlAction = Literal["start", "stop", "pause", "resume", "drain", "cancel_drain"]
_QUEUE_STATUSES = ("discovered", "running", "success", "failed", "dead", "deferred")


def _run(row) -> ScraperRunView:
    return ScraperRunView(
        run_id=row["run_id"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        status=row["status"],
        processed=row["processed"],
        success=row["success"],
        failed=row["failed"],
        torrents_found=row["torrents_found"],
        duration_ms=row["duration_ms"],
        worker_count=row["worker_count"],
        error_code="background_failure" if row["last_error"] is not None else None,
    )


@router.get(
    "/snapshot",
    response_model=ApiSuccess[ScrapingSnapshotData],
)
async def scraping_snapshot(
    request: Request,
    _session_token: Annotated[str, Depends(require_admin_session)],
):
    now = time.time()
    success_cutoff = now - settings.BACKGROUND_SCRAPER_SUCCESS_TTL
    queue_rows = await database.fetch_all(
        """
        WITH queue AS (
            SELECT
                'item' AS kind,
                status,
                next_retry_at,
                last_success_at,
                created_at,
                consecutive_failures
            FROM background_scraper_items
            UNION ALL
            SELECT
                'episode' AS kind,
                status,
                next_retry_at,
                last_success_at,
                created_at,
                consecutive_failures
            FROM background_scraper_episodes
            WHERE season >= 1 AND episode >= 1
        )
        SELECT
            kind,
            status,
            COUNT(*) AS count,
            MIN(
                CASE
                    WHEN (next_retry_at IS NULL OR next_retry_at <= :now)
                     AND (last_success_at IS NULL OR last_success_at <= :success_cutoff)
                     AND (status != 'dead' OR consecutive_failures < :max_retries)
                    THEN COALESCE(next_retry_at, created_at, :now)
                END
            ) AS oldest_ready_at,
            SUM(
                CASE
                    WHEN (next_retry_at IS NULL OR next_retry_at <= :now)
                     AND (last_success_at IS NULL OR last_success_at <= :success_cutoff)
                     AND (status != 'dead' OR consecutive_failures < :max_retries)
                    THEN 1 ELSE 0
                END
            ) AS ready
        FROM queue
        GROUP BY kind, status
        """,
        {
            "now": now,
            "success_cutoff": success_cutoff,
            "max_retries": (
                settings.BACKGROUND_SCRAPER_MAX_RETRIES
                if settings.BACKGROUND_SCRAPER_MAX_RETRIES >= 0
                else 1_000_000
            ),
        },
        force_primary=True,
    )
    counts = {status: 0 for status in _QUEUE_STATUSES}
    item_count = 0
    episode_count = 0
    ready = 0
    oldest_ready_at = None
    for row in queue_rows:
        count = row["count"]
        if row["kind"] == "item":
            item_count += count
        else:
            episode_count += count
        counts[row["status"]] = counts.get(row["status"], 0) + count
        ready += row["ready"]
        if row["oldest_ready_at"] is not None:
            oldest_ready_at = (
                row["oldest_ready_at"]
                if oldest_ready_at is None
                else min(oldest_ready_at, row["oldest_ready_at"])
            )
    runtimes = await database.fetch_all(
        """
        SELECT *
        FROM background_scraper_runtimes
        WHERE last_heartbeat >= :cutoff
        ORDER BY state, instance_id, process_id
        """,
        {"cutoff": now - 2},
        force_primary=True,
    )
    run_summary = await database.fetch_one(
        """
        SELECT
            COUNT(*) AS runs,
            COALESCE(SUM(processed_count), 0) AS processed,
            COALESCE(SUM(failed_count), 0) AS failed,
            COALESCE(SUM(torrents_found_count), 0) AS torrents_found
        FROM background_scraper_runs
        WHERE started_at >= :cutoff
        """,
        {"cutoff": now - 24 * 60 * 60},
        force_primary=True,
    )
    latest = await database.fetch_one(
        f"""
        SELECT {BACKGROUND_SCRAPER_RUNS_PROJECTION}
        FROM background_scraper_runs
        ORDER BY started_at DESC, run_id DESC
        LIMIT 1
        """,
        force_primary=True,
    )
    return success_response(
        request,
        ScrapingSnapshotData(
            collected_at=now,
            runtimes=[
                ScraperRuntimeView(
                    **{
                        **dict(row),
                        "draining": bool(row["draining"]),
                    }
                )
                for row in runtimes
            ],
            queue=ScraperQueueSummary(
                items=item_count,
                episodes=episode_count,
                ready=ready,
                running=counts["running"],
                deferred=counts["deferred"],
                failed=counts["failed"],
                dead=counts["dead"],
                oldest_ready_at=oldest_ready_at,
                low_watermark=settings.BACKGROUND_SCRAPER_QUEUE_LOW_WATERMARK,
                high_watermark=settings.BACKGROUND_SCRAPER_QUEUE_HIGH_WATERMARK,
                hard_cap=settings.BACKGROUND_SCRAPER_QUEUE_HARD_CAP,
            ),
            runs_24h=run_summary["runs"],
            processed_24h=run_summary["processed"],
            failed_24h=run_summary["failed"],
            torrents_found_24h=run_summary["torrents_found"],
            latest_run=_run(latest) if latest is not None else None,
        ),
    )


@router.get(
    "/queue/{kind}",
    response_model=ApiSuccess[ScraperQueuePageData],
)
async def scraper_queue(
    request: Request,
    _session_token: Annotated[str, Depends(require_admin_session)],
    kind: Annotated[QueueKind, Path()],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
    status: Annotated[str | None, Query(max_length=32)] = None,
    search: Annotated[str | None, Query(max_length=128)] = None,
):
    if status is not None and status not in _QUEUE_STATUSES:
        raise ApiProblem(
            status_code=422,
            code="invalid_queue_status",
            message="The requested queue status is invalid.",
        )
    values: dict[str, object] = {"limit": limit + 1}
    predicates = ["1 = 1"]
    if cursor is not None:
        updated_at, identifier = decode_timestamp_cursor(
            cursor,
            subject="scraper",
        )
        values.update({"cursor_updated_at": updated_at, "cursor_id": identifier})
        column = "i.media_id" if kind == "item" else "e.episode_media_id"
        updated_column = (
            "COALESCE(i.updated_at, 0)"
            if kind == "item"
            else "COALESCE(e.updated_at, 0)"
        )
        predicates.append(
            f"({updated_column} < :cursor_updated_at OR "
            f"({updated_column} = :cursor_updated_at AND {column} < :cursor_id))"
        )
    if status is not None:
        values["status"] = status
        predicates.append(("i.status" if kind == "item" else "e.status") + " = :status")
    if search is not None and search.strip():
        escaped = (
            search.strip()
            .lower()
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        values["search"] = f"%{escaped}%"
        predicates.append(
            "(LOWER(i.media_id) LIKE :search ESCAPE '\\' "
            "OR LOWER(i.title) LIKE :search ESCAPE '\\')"
        )
    where = " AND ".join(predicates)
    if kind == "item":
        rows = await database.fetch_all(
            f"""
            SELECT
                'item' AS kind,
                i.media_id AS id,
                NULL AS parent_id,
                i.media_type,
                i.title,
                i.year,
                NULL AS season,
                NULL AS episode,
                i.priority_score,
                i.status,
                i.consecutive_failures,
                i.last_scraped_at,
                i.last_success_at,
                i.last_failure_at,
                i.next_retry_at,
                i.total_torrents_found,
                i.created_at,
                i.updated_at
            FROM background_scraper_items i
            WHERE {where}
            ORDER BY COALESCE(i.updated_at, 0) DESC, i.media_id DESC
            LIMIT :limit
            """,
            values,
            force_primary=True,
        )
    else:
        rows = await database.fetch_all(
            f"""
            SELECT
                'episode' AS kind,
                e.episode_media_id AS id,
                e.series_id AS parent_id,
                'series' AS media_type,
                i.title,
                i.year,
                e.season,
                e.episode,
                i.priority_score,
                e.status,
                e.consecutive_failures,
                e.last_scraped_at,
                e.last_success_at,
                e.last_failure_at,
                e.next_retry_at,
                e.total_torrents_found,
                e.created_at,
                e.updated_at
            FROM background_scraper_episodes e
            JOIN background_scraper_items i ON i.media_id = e.series_id
            WHERE {where}
            ORDER BY COALESCE(e.updated_at, 0) DESC, e.episode_media_id DESC
            LIMIT :limit
            """,
            values,
            force_primary=True,
        )
    page = rows[:limit]
    next_cursor = (
        encode_timestamp_cursor(page[-1]["updated_at"] or 0, page[-1]["id"])
        if len(rows) > limit
        else None
    )
    return success_response(
        request,
        ScraperQueuePageData(
            items=[ScraperQueueEntry(**dict(row)) for row in page],
            next_cursor=next_cursor,
        ),
    )


@router.get(
    "/runs",
    response_model=ApiSuccess[ScraperRunsPageData],
)
async def scraper_runs(
    request: Request,
    _session_token: Annotated[str, Depends(require_admin_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
):
    values: dict[str, object] = {"limit": limit + 1}
    predicate = ""
    if cursor is not None:
        started_at, run_id = decode_timestamp_cursor(
            cursor,
            subject="scraper",
        )
        values.update({"started_at": started_at, "run_id": run_id})
        predicate = (
            "WHERE started_at < :started_at OR "
            "(started_at = :started_at AND run_id < :run_id)"
        )
    rows = await database.fetch_all(
        f"""
        SELECT {BACKGROUND_SCRAPER_RUNS_PROJECTION}
        FROM background_scraper_runs
        {predicate}
        ORDER BY started_at DESC, run_id DESC
        LIMIT :limit
        """,
        values,
        force_primary=True,
    )
    page = rows[:limit]
    next_cursor = (
        encode_timestamp_cursor(page[-1]["started_at"], page[-1]["run_id"])
        if len(rows) > limit
        else None
    )
    return success_response(
        request,
        ScraperRunsPageData(
            items=[_run(row) for row in page],
            next_cursor=next_cursor,
        ),
    )


@router.post(
    "/queue/{kind}/{resource_id}/{action}",
    response_model=ApiSuccess[ScraperQueueMutationData],
)
async def mutate_scraper_queue(
    request: Request,
    _session_token: Annotated[str, Depends(require_csrf)],
    kind: Annotated[QueueKind, Path()],
    resource_id: Annotated[
        str,
        Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9:_-]+$"),
    ],
    action: Annotated[QueueAction, Path()],
):
    now = time.time()
    if action == "retry":
        state = ("discovered", 0, now)
    elif action == "defer":
        state = (
            "deferred",
            None,
            now + settings.BACKGROUND_SCRAPER_DEFER_COOLDOWN,
        )
    else:
        state = (
            "dead",
            (
                settings.BACKGROUND_SCRAPER_MAX_RETRIES
                if settings.BACKGROUND_SCRAPER_MAX_RETRIES >= 0
                else 1_000_000
            ),
            None,
        )
    table, key = (
        ("background_scraper_items", "media_id")
        if kind == "item"
        else ("background_scraper_episodes", "episode_media_id")
    )
    failures_sql = (
        "consecutive_failures" if state[1] is None else ":consecutive_failures"
    )
    clear_success = action == "retry"
    parent_predicate = (
        """
          AND EXISTS (
              SELECT 1
              FROM background_scraper_items parent
              WHERE parent.media_id = background_scraper_episodes.series_id
                AND parent.status != 'running'
          )
        """
        if kind == "episode" and clear_success
        else ""
    )
    async with database.transaction():
        updated = await database.fetch_one(
            f"""
            UPDATE {table}
            SET status = :status,
                consecutive_failures = {failures_sql},
                last_success_at = {"NULL" if clear_success else "last_success_at"},
                next_retry_at = :next_retry_at,
                updated_at = :updated_at
            WHERE {key} = :resource_id
              AND status != 'running'
              {parent_predicate}
            RETURNING {key}, {"series_id" if kind == "episode" else "NULL AS series_id"}
            """,
            {
                "resource_id": resource_id,
                "status": state[0],
                "consecutive_failures": state[1],
                "next_retry_at": state[2],
                "updated_at": now,
            },
            force_primary=True,
        )
        if updated is not None and kind == "episode" and clear_success:
            await database.execute(
                """
                UPDATE background_scraper_items
                SET status = 'discovered',
                    consecutive_failures = 0,
                    last_success_at = NULL,
                    next_retry_at = :now,
                    updated_at = :now
                WHERE media_id = :series_id
                """,
                {"series_id": updated["series_id"], "now": now},
                force_primary=True,
            )
    if updated is None:
        exists = await database.fetch_one(
            f"SELECT status FROM {table} WHERE {key} = :resource_id",
            {"resource_id": resource_id},
            force_primary=True,
        )
        if exists is None:
            raise ApiProblem(
                status_code=404,
                code="queue_item_not_found",
                message="The requested queue entry was not found.",
            )
        raise ApiProblem(
            status_code=409,
            code="queue_item_owned",
            message="A running queue entry cannot be changed.",
        )
    log.info(
        "background.queue.changed",
        "Background queue entry changed",
        content_id=resource_id,
        media_type=kind,
        operation=action,
    )
    return success_response(
        request,
        ScraperQueueMutationData(
            kind=kind,
            resource_id=resource_id,
            action=action,
            affected=1,
        ),
    )


@router.post(
    "/queue/requeue-dead",
    response_model=ApiSuccess[ScraperQueueMutationData],
)
async def requeue_dead(
    request: Request,
    _session_token: Annotated[str, Depends(require_csrf)],
):
    requeued = await background_scraper.requeue_dead_items(database)
    affected = requeued["items"] + requeued["episodes"]
    return success_response(
        request,
        ScraperQueueMutationData(
            kind="all",
            resource_id="dead",
            action="requeue_dead",
            affected=affected,
        ),
    )


@router.post(
    "/control/{action}",
    response_model=ApiSuccess[ScraperControlData],
)
async def control_scraper(
    request: Request,
    _session_token: Annotated[str, Depends(require_csrf)],
    action: Annotated[ControlAction, Path()],
):
    results = await dispatch_scraper_command(f"scraper.{action}")
    if not results:
        raise ApiProblem(
            status_code=503,
            code="command_timeout",
            message="No background scraper owner acknowledged the command.",
        )
    outcome = (
        "succeeded"
        if all(result["outcome"] == "succeeded" for result in results)
        else "rejected"
    )
    if outcome == "rejected":
        raise ApiProblem(
            status_code=409,
            code="invalid_runtime_state",
            message="The background scraper cannot perform that action now.",
        )
    log.info(
        "background.control.changed",
        "Background scraper control changed",
        operation=action,
        worker_count=len(results),
        outcome="ok",
    )
    return success_response(
        request,
        ScraperControlData(
            action=action,
            owners=len(results),
            outcome=outcome,
        ),
    )
