"""Authenticated operational logs, live stream, and safe export."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Annotated, Literal

import orjson
from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import Response, StreamingResponse

from comet.api.v1.contracts import ApiSuccess, OperationalEventPageData
from comet.api.v1.responses import success_response
from comet.api.v1.security import admin_session_active, require_admin_session
from comet.core.event_store import EventFilters, EventLevel, EventStore
from comet.core.models import database

router = APIRouter(prefix="/admin", tags=["API v1 Logs"])
_store = EventStore(database)
_POLL_SECONDS = 0.5
_HEARTBEAT_SECONDS = 15
_SESSION_CHECK_SECONDS = 5


def event_filters(
    search: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
    category: Annotated[str | None, Query(min_length=1, max_length=32)] = None,
    level: EventLevel | None = None,
    instance_id: Annotated[str | None, Query(pattern=r"^[0-9a-f]{32}$")] = None,
    role: Annotated[str | None, Query(min_length=1, max_length=32)] = None,
    request_id: Annotated[str | None, Query(pattern=r"^[0-9a-f]{32}$")] = None,
    run_id: Annotated[str | None, Query(pattern=r"^[0-9a-f]{32}$")] = None,
    connection_id: Annotated[str | None, Query(pattern=r"^[0-9a-f]{32}$")] = None,
    media_type: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    provider_name: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
    outcome: Annotated[str | None, Query(min_length=1, max_length=32)] = None,
    started_at: Annotated[float | None, Query(ge=0)] = None,
    ended_at: Annotated[float | None, Query(ge=0)] = None,
) -> EventFilters:
    return EventFilters(
        search=search,
        category=category,
        level=level,
        instance_id=instance_id,
        role=role,
        request_id=request_id,
        run_id=run_id,
        connection_id=connection_id,
        media_type=media_type,
        provider_name=provider_name,
        outcome=outcome,
        started_at=started_at,
        ended_at=ended_at,
    )


@router.get(
    "/logs",
    response_model=ApiSuccess[OperationalEventPageData],
)
async def logs(
    request: Request,
    _session: Annotated[str, Depends(require_admin_session)],
    filters: Annotated[EventFilters, Depends(event_filters)],
    cursor: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
):
    page = await _store.page(
        filters,
        limit=limit,
        before=cursor,
    )
    return success_response(
        request,
        OperationalEventPageData(
            items=list(page.items),
            next_cursor=page.next_cursor,
            dropped_events=page.dropped_events,
        ),
    )


@router.get("/logs/stream")
async def stream_events(
    request: Request,
    session: Annotated[str, Depends(require_admin_session)],
    filters: Annotated[EventFilters, Depends(event_filters)],
    cursor: Annotated[int | None, Query(ge=0)] = None,
    last_event_id: Annotated[int | None, Header(alias="Last-Event-ID", ge=0)] = None,
):
    async def generate():
        position = last_event_id if last_event_id is not None else (cursor or 0)
        clock = asyncio.get_running_loop()
        heartbeat_at = clock.time() + _HEARTBEAT_SECONDS
        session_check_at = clock.time() + _SESSION_CHECK_SECONDS
        while not await request.is_disconnected():
            now = clock.time()
            if now >= session_check_at:
                if not await admin_session_active(session):
                    yield b"event: session_expired\ndata: {}\n\n"
                    return
                session_check_at = now + _SESSION_CHECK_SECONDS
            page = await _store.page(
                filters,
                limit=500,
                after=position,
                ascending=True,
                include_dropped=False,
            )
            for item in page.items:
                position = item["id"]
                yield (
                    f"id: {position}\nevent: operational_event\ndata: ".encode()
                    + orjson.dumps(item)
                    + b"\n\n"
                )
            now = clock.time()
            if now >= heartbeat_at:
                yield b": keep-alive\n\n"
                heartbeat_at = now + _HEARTBEAT_SECONDS
            await asyncio.sleep(_POLL_SECONDS)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "private, no-store",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/logs/export")
async def export_logs(
    _session: Annotated[str, Depends(require_admin_session)],
    filters: Annotated[EventFilters, Depends(event_filters)],
    format: Literal["jsonl", "text"] = "jsonl",
):
    exported_at = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    page = await _store.page(
        filters,
        limit=10_000,
        ascending=True,
    )
    if format == "jsonl":
        body = b"".join(orjson.dumps(item) + b"\n" for item in page.items)
        media_type = "application/x-ndjson"
        filename = f"comet-logs-{exported_at}.jsonl"
    else:
        body = "".join(
            f"{item['created_at']:.3f} {item['level']} {item['category']} "
            f"{item['event']} - {item['message']}\n"
            for item in page.items
        ).encode()
        media_type = "text/plain"
        filename = f"comet-logs-{exported_at}.txt"
    return Response(
        body,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
