"""Shared bounded operational-event history."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal

import orjson

from comet.core.db_transactions import write_transaction

EventLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


@dataclass(frozen=True, slots=True)
class EventWrite:
    created_at: float
    instance_id: str
    process_id: int
    role: str
    level: EventLevel
    category: str
    event: str
    message: str
    request_id: str | None
    run_id: str | None
    connection_id: str | None
    media_type: str | None
    provider_name: str | None
    outcome: str | None
    error_code: str | None
    details: dict[str, str | bool | int | float]


@dataclass(frozen=True, slots=True)
class EventFilters:
    search: str | None = None
    category: str | None = None
    level: EventLevel | None = None
    instance_id: str | None = None
    role: str | None = None
    request_id: str | None = None
    run_id: str | None = None
    connection_id: str | None = None
    media_type: str | None = None
    provider_name: str | None = None
    outcome: str | None = None
    started_at: float | None = None
    ended_at: float | None = None


@dataclass(frozen=True, slots=True)
class EventPage:
    items: tuple[dict[str, Any], ...]
    next_cursor: int | None
    dropped_events: int


class EventStore:
    def __init__(self, database):
        self._database = database

    def _write_transaction(self):
        return write_transaction(self._database)

    async def append(self, events: list[EventWrite], *, dropped: int = 0) -> None:
        if not events and dropped == 0:
            return
        event_count = len(events)
        async with self._write_transaction():
            current = await self._database.fetch_val(
                """
                UPDATE operational_event_state
                SET current_event_id = current_event_id + :event_count,
                    dropped_events = dropped_events + :dropped
                WHERE id = 1
                RETURNING current_event_id
                """,
                {"event_count": event_count, "dropped": dropped},
                force_primary=True,
            )
            if events:
                next_id = current - event_count + 1
                await self._database.execute_many(
                    """
                    INSERT INTO operational_events (
                        id, created_at, instance_id, process_id, role,
                        level, category, event, message,
                        request_id, run_id, connection_id,
                        media_type, provider_name, outcome, error_code,
                        details_json
                    ) VALUES (
                        :id, :created_at, :instance_id, :process_id, :role,
                        :level, :category, :event, :message,
                        :request_id, :run_id, :connection_id,
                        :media_type, :provider_name, :outcome, :error_code,
                        :details_json
                    )
                    """,
                    [
                        {
                            "id": next_id + offset,
                            "created_at": item.created_at,
                            "instance_id": item.instance_id,
                            "process_id": item.process_id,
                            "role": item.role,
                            "level": item.level,
                            "category": item.category,
                            "event": item.event,
                            "message": item.message,
                            "request_id": item.request_id,
                            "run_id": item.run_id,
                            "connection_id": item.connection_id,
                            "media_type": item.media_type,
                            "provider_name": item.provider_name,
                            "outcome": item.outcome,
                            "error_code": item.error_code,
                            "details_json": orjson.dumps(item.details).decode("utf-8"),
                        }
                        for offset, item in enumerate(events)
                    ],
                    force_primary=True,
                )

    async def page(
        self,
        filters: EventFilters,
        *,
        limit: int,
        before: int | None = None,
        after: int | None = None,
        ascending: bool = False,
        include_dropped: bool = True,
    ) -> EventPage:
        clauses: list[str] = []
        values: dict[str, object] = {"limit": limit + 1}
        for column in (
            "category",
            "level",
            "instance_id",
            "role",
            "request_id",
            "run_id",
            "connection_id",
            "media_type",
            "provider_name",
            "outcome",
        ):
            value = getattr(filters, column)
            if value is not None:
                clauses.append(f"{column} = :{column}")
                values[column] = value
        if filters.started_at is not None:
            clauses.append("created_at >= :started_at")
            values["started_at"] = filters.started_at
        if filters.ended_at is not None:
            clauses.append("created_at <= :ended_at")
            values["ended_at"] = filters.ended_at
        if filters.search is not None:
            searchable = (
                "event",
                "message",
                "category",
                "error_code",
                "provider_name",
                "outcome",
                "role",
                "request_id",
                "run_id",
                "connection_id",
                "media_type",
                "details_json",
            )
            clauses.append(
                "("
                + " OR ".join(
                    f"LOWER(COALESCE({column}, '')) LIKE :search"
                    for column in searchable
                )
                + ")"
            )
            values["search"] = f"%{filters.search.lower()}%"
        if before is not None:
            clauses.append("id < :before")
            values["before"] = before
        if after is not None:
            clauses.append("id > :after")
            values["after"] = after
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        direction = "ASC" if ascending else "DESC"
        rows = await self._database.fetch_all(
            f"""
            SELECT *
            FROM operational_events
            {where}
            ORDER BY id {direction}
            LIMIT :limit
            """,
            values,
            force_primary=True,
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = tuple(self._event(row) for row in rows)
        dropped = (
            await self._database.fetch_val(
                "SELECT dropped_events FROM operational_event_state WHERE id = 1",
                force_primary=True,
            )
            if include_dropped
            else 0
        )
        return EventPage(
            items=items,
            next_cursor=items[-1]["id"] if has_more and items else None,
            dropped_events=dropped,
        )

    async def prune(
        self,
        *,
        max_age_seconds: float = 24 * 60 * 60,
        summary_age_seconds: float = 7 * 24 * 60 * 60,
        max_rows: int = 100_000,
        now: float | None = None,
    ) -> int:
        current_time = time.time() if now is None else now
        cutoff = current_time - max_age_seconds
        summary_cutoff = current_time - summary_age_seconds
        async with self._write_transaction():
            threshold = await self._database.fetch_val(
                """
                SELECT COALESCE(MAX(id), 0) - :max_rows
                FROM operational_events
                """,
                {"max_rows": max_rows},
                force_primary=True,
            )
            count = await self._database.fetch_val(
                """
                SELECT COUNT(*)
                FROM operational_events
                WHERE (
                    created_at < :cutoff
                    AND category NOT IN ('PLAYBACK', 'STREAM', 'USENET')
                )
                OR created_at < :summary_cutoff
                OR id <= :threshold
                """,
                {
                    "cutoff": cutoff,
                    "summary_cutoff": summary_cutoff,
                    "threshold": threshold,
                },
                force_primary=True,
            )
            await self._database.execute(
                """
                DELETE FROM operational_events
                WHERE (
                    created_at < :cutoff
                    AND category NOT IN ('PLAYBACK', 'STREAM', 'USENET')
                )
                OR created_at < :summary_cutoff
                OR id <= :threshold
                """,
                {
                    "cutoff": cutoff,
                    "summary_cutoff": summary_cutoff,
                    "threshold": threshold,
                },
                force_primary=True,
            )
        return count

    @staticmethod
    def _event(row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "created_at": row["created_at"],
            "instance_id": row["instance_id"],
            "process_id": row["process_id"],
            "role": row["role"],
            "level": row["level"],
            "category": row["category"],
            "event": row["event"],
            "message": row["message"],
            "request_id": row["request_id"],
            "run_id": row["run_id"],
            "connection_id": row["connection_id"],
            "media_type": row["media_type"],
            "provider_name": row["provider_name"],
            "outcome": row["outcome"],
            "error_code": row["error_code"],
            "details": orjson.loads(row["details_json"]),
        }
