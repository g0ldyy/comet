from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import orjson

from comet.core.models import database, settings
from comet.core.runtime_registry import RuntimeIdentity
from comet.observability import create_detached_task, log
from comet.usenet.engine_client import EngineClient
from comet.usenet.engine_transport import EngineUnavailable

_SYNC_SECONDS = 0.5
_ENGINE_SNAPSHOT_SECONDS = 2
_HISTORY_RETENTION_SECONDS = 7 * 24 * 60 * 60


@dataclass(slots=True)
class UsenetOperation:
    id: str
    client_ip: str
    content_id: str
    title: str
    member_path: str
    source_kind: str
    started_at: float
    total_bytes: int
    bytes_transferred: int = 0
    updated_at: float = 0
    task: asyncio.Task | None = None
    admin_cancelled: bool = False


class UsenetOperationMonitor:
    def __init__(self) -> None:
        self._identity = RuntimeIdentity.current()
        self._process_id = os.getpid()
        self._operations: dict[str, UsenetOperation] = {}
        self._sync_task: asyncio.Task | None = None
        self._engine = EngineClient(Path(settings.USENET_RUNTIME_DIR) / "engine.json")

    async def initialize(self) -> None:
        if self._sync_task is not None:
            return
        await database.execute(
            """
            DELETE FROM usenet_active_operations
            WHERE instance_id = :instance_id AND process_id = :process_id
            """,
            {
                "instance_id": self._identity.instance_id,
                "process_id": self._process_id,
            },
        )
        self._sync_task = create_detached_task(
            self._sync_loop(),
            name="usenet.operations",
        )

    def reconfigure(self, config) -> None:
        self._engine = EngineClient(Path(config.USENET_RUNTIME_DIR) / "engine.json")

    async def start(
        self,
        *,
        client_ip: str,
        content_id: str,
        title: str,
        member_path: str,
        source_kind: str,
        total_bytes: int,
    ) -> str:
        started_at = time.time()
        operation = UsenetOperation(
            id=uuid.uuid4().hex,
            client_ip=client_ip,
            content_id=content_id,
            title=title,
            member_path=member_path,
            source_kind=source_kind,
            started_at=started_at,
            updated_at=started_at,
            total_bytes=total_bytes,
        )
        await database.execute(
            """
            INSERT INTO usenet_active_operations (
                id, instance_id, process_id, client_ip, content_id, title,
                member_path, source_kind, started_at, updated_at,
                total_bytes, bytes_transferred, cancel_requested
            ) VALUES (
                :id, :instance_id, :process_id, :client_ip, :content_id, :title,
                :member_path, :source_kind, :started_at, :updated_at,
                :total_bytes, 0, 0
            )
            """,
            {
                "id": operation.id,
                "instance_id": self._identity.instance_id,
                "process_id": self._process_id,
                "client_ip": operation.client_ip,
                "content_id": operation.content_id,
                "title": operation.title,
                "member_path": operation.member_path,
                "source_kind": operation.source_kind,
                "started_at": operation.started_at,
                "updated_at": operation.updated_at,
                "total_bytes": operation.total_bytes,
            },
        )
        self._operations[operation.id] = operation
        return operation.id

    def add_bytes(self, operation_id: str, byte_count: int) -> None:
        operation = self._operations[operation_id]
        operation.bytes_transferred += byte_count
        operation.updated_at = time.time()

    def admin_cancelled(self, operation_id: str) -> bool:
        return self._operations[operation_id].admin_cancelled

    async def run_cancellable[T](
        self,
        operation_id: str,
        request: Coroutine[Any, Any, T],
    ) -> T:
        operation = self._operations[operation_id]
        task = asyncio.create_task(request)
        operation.task = task
        if operation.admin_cancelled:
            task.cancel()
        try:
            return await task
        finally:
            operation.task = None

    async def finish(
        self,
        operation_id: str,
        *,
        outcome: str,
        error_code: str | None = None,
    ) -> None:
        operation = self._operations.pop(operation_id, None)
        if operation is None:
            return
        if operation.admin_cancelled:
            outcome = "cancelled"
            error_code = None
        finished_at = time.time()
        async with database.transaction():
            await database.execute(
                """
                INSERT INTO usenet_operation_history (
                    id, instance_id, process_id, client_ip, content_id, title,
                    member_path, source_kind, started_at, finished_at, duration,
                    total_bytes, bytes_transferred, outcome, error_code
                ) VALUES (
                    :id, :instance_id, :process_id, :client_ip, :content_id, :title,
                    :member_path, :source_kind, :started_at, :finished_at, :duration,
                    :total_bytes, :bytes_transferred, :outcome, :error_code
                )
                ON CONFLICT (id) DO NOTHING
                """,
                {
                    "id": operation.id,
                    "instance_id": self._identity.instance_id,
                    "process_id": self._process_id,
                    "client_ip": operation.client_ip,
                    "content_id": operation.content_id,
                    "title": operation.title,
                    "member_path": operation.member_path,
                    "source_kind": operation.source_kind,
                    "started_at": operation.started_at,
                    "finished_at": finished_at,
                    "duration": finished_at - operation.started_at,
                    "total_bytes": operation.total_bytes,
                    "bytes_transferred": operation.bytes_transferred,
                    "outcome": outcome,
                    "error_code": error_code,
                },
            )
            await database.execute(
                "DELETE FROM usenet_active_operations WHERE id = :id",
                {"id": operation.id},
            )

    async def _publish_engine_snapshot(self, now: float) -> None:
        try:
            async with asyncio.timeout(1):
                health, stats = await asyncio.gather(
                    self._engine.health(),
                    self._engine.stats(),
                )
        except (EngineUnavailable, TimeoutError):
            healthy = False
            mode = "unavailable"
            stats_json = None
        else:
            healthy = True
            mode = health["mode"]
            stats_json = orjson.dumps(
                stats,
                option=orjson.OPT_SORT_KEYS,
            ).decode("utf-8")
        await database.execute(
            """
            INSERT INTO usenet_engine_runtimes (
                instance_id, process_id, healthy, mode, stats_json, collected_at
            ) VALUES (
                :instance_id, :process_id, :healthy, :mode, :stats_json, :collected_at
            )
            ON CONFLICT (instance_id) DO UPDATE SET
                process_id = excluded.process_id,
                healthy = excluded.healthy,
                mode = excluded.mode,
                stats_json = excluded.stats_json,
                collected_at = excluded.collected_at
            """,
            {
                "instance_id": self._identity.instance_id,
                "process_id": self._process_id,
                "healthy": int(healthy),
                "mode": mode,
                "stats_json": stats_json,
                "collected_at": now,
            },
        )

    async def _sync_operations(self) -> None:
        operations = tuple(self._operations.values())
        if operations:
            await database.execute_many(
                """
                UPDATE usenet_active_operations
                SET updated_at = :updated_at,
                    bytes_transferred = :bytes_transferred
                WHERE id = :id
                """,
                [
                    {
                        "id": operation.id,
                        "updated_at": operation.updated_at,
                        "bytes_transferred": operation.bytes_transferred,
                    }
                    for operation in operations
                ],
            )
        requested = await database.fetch_all(
            """
            SELECT id
            FROM usenet_active_operations
            WHERE instance_id = :instance_id
              AND process_id = :process_id
              AND cancel_requested = 1
            """,
            {
                "instance_id": self._identity.instance_id,
                "process_id": self._process_id,
            },
            force_primary=True,
        )
        for row in requested:
            operation = self._operations.get(row["id"])
            if operation is not None and not operation.admin_cancelled:
                operation.admin_cancelled = True
                if operation.task is not None:
                    operation.task.cancel()

    async def _sync_loop(self) -> None:
        next_engine_snapshot = 0.0
        next_cleanup = 0.0
        while True:
            now = time.time()
            try:
                await self._sync_operations()
                if now >= next_engine_snapshot:
                    await self._publish_engine_snapshot(now)
                    next_engine_snapshot = now + _ENGINE_SNAPSHOT_SECONDS
                if now >= next_cleanup:
                    await database.execute(
                        """
                        DELETE FROM usenet_operation_history
                        WHERE finished_at < :cutoff
                        """,
                        {"cutoff": now - _HISTORY_RETENTION_SECONDS},
                    )
                    await database.execute(
                        """
                        DELETE FROM usenet_engine_runtimes
                        WHERE collected_at < :cutoff
                        """,
                        {"cutoff": now - 30},
                    )
                    await database.execute(
                        """
                        DELETE FROM usenet_active_operations
                        WHERE updated_at < :cutoff
                        """,
                        {"cutoff": now - 30},
                    )
                    next_cleanup = now + 60 * 60
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.warning(
                    "usenet.telemetry.sync_failed",
                    "Usenet telemetry synchronization failed",
                    error_code="database_warning",
                    exc=error,
                )
            await asyncio.sleep(_SYNC_SECONDS)

    async def shutdown(self) -> None:
        if self._sync_task is not None:
            self._sync_task.cancel()
            await asyncio.gather(self._sync_task, return_exceptions=True)
            self._sync_task = None
        await database.execute(
            """
            DELETE FROM usenet_active_operations
            WHERE instance_id = :instance_id AND process_id = :process_id
            """,
            {
                "instance_id": self._identity.instance_id,
                "process_id": self._process_id,
            },
        )
        await database.execute(
            """
            DELETE FROM usenet_engine_runtimes
            WHERE instance_id = :instance_id AND process_id = :process_id
            """,
            {
                "instance_id": self._identity.instance_id,
                "process_id": self._process_id,
            },
        )
        self._operations.clear()


usenet_operation_monitor = UsenetOperationMonitor()
