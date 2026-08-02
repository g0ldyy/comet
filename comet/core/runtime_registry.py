"""Shared replica and process identity with bounded heartbeat retention."""

from __future__ import annotations

import asyncio
import os
import re
import socket
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

import orjson

_INSTANCE_PATTERN = re.compile(r"^[0-9a-f]{32}$")
RuntimeRole = Literal[
    "supervisor",
    "web_master",
    "web_worker",
    "executor_worker",
    "cometnet",
    "usenet_engine",
]


def restart_target_pid() -> int | None:
    marker = os.environ.get("COMET_RUNTIME_SUPERVISOR_PID") or os.environ.get(
        "COMET_WEB_MASTER_PID"
    )
    if marker is None or not marker.isdecimal():
        return None
    if marker == "0":
        return os.getpid()
    process_id = int(marker)
    return process_id if process_id > 1 else None


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    instance_id: str
    started_at: float
    hostname: str

    @classmethod
    def current(cls) -> RuntimeIdentity:
        instance_id = os.environ.get("COMET_RUNTIME_INSTANCE_ID")
        if instance_id is None:
            instance_id = uuid.uuid4().hex
            os.environ["COMET_RUNTIME_INSTANCE_ID"] = instance_id
        if _INSTANCE_PATTERN.fullmatch(instance_id) is None:
            raise ValueError("runtime instance ID is invalid")

        raw_started_at = os.environ.get("COMET_RUNTIME_STARTED_AT")
        if raw_started_at is None:
            started_at = time.time()
            os.environ["COMET_RUNTIME_STARTED_AT"] = str(started_at)
        else:
            try:
                started_at = float(raw_started_at)
            except ValueError:
                raise ValueError("runtime start time is invalid") from None
            if not 0 < started_at <= time.time() + 60:
                raise ValueError("runtime start time is invalid")

        return cls(
            instance_id=instance_id,
            started_at=started_at,
            hostname=socket.gethostname(),
        )


class RuntimeRegistry:
    def __init__(self, database, settings, *, identity: RuntimeIdentity | None = None):
        self._database = database
        self._settings = settings
        self.identity = identity or RuntimeIdentity.current()

    async def heartbeat(
        self,
        *,
        role: RuntimeRole,
        readiness: dict[str, Any],
        process_id: int | None = None,
        observed_at: float | None = None,
    ) -> None:
        pid = os.getpid() if process_id is None else process_id
        now = time.time() if observed_at is None else observed_at
        readiness_json = orjson.dumps(readiness, option=orjson.OPT_SORT_KEYS).decode(
            "utf-8"
        )
        from comet.core.live_settings import pending_restart_keys

        pending_restart_keys_json = orjson.dumps(pending_restart_keys()).decode()

        async with self._database.transaction():
            await self._database.execute(
                """
                INSERT INTO runtime_instances (
                    instance_id, alias, hostname, started_at, last_heartbeat,
                    commit_hash, branch, build_date, applied_revision,
                    pending_restart_keys_json, readiness_json, restart_capable
                ) VALUES (
                    :instance_id, :alias, :hostname, :started_at, :last_heartbeat,
                    :commit_hash, :branch, :build_date, :applied_revision,
                    :pending_restart_keys_json, :readiness_json, :restart_capable
                )
                ON CONFLICT (instance_id) DO UPDATE SET
                    alias = excluded.alias,
                    hostname = excluded.hostname,
                    last_heartbeat = excluded.last_heartbeat,
                    commit_hash = excluded.commit_hash,
                    branch = excluded.branch,
                    build_date = excluded.build_date,
                    applied_revision = excluded.applied_revision,
                    pending_restart_keys_json = excluded.pending_restart_keys_json,
                    readiness_json = excluded.readiness_json,
                    restart_capable = excluded.restart_capable
                """,
                {
                    "instance_id": self.identity.instance_id,
                    "alias": self._settings.RUNTIME_INSTANCE_ALIAS,
                    "hostname": self.identity.hostname,
                    "started_at": self.identity.started_at,
                    "last_heartbeat": now,
                    "commit_hash": self._settings.COMET_COMMIT_HASH,
                    "branch": self._settings.COMET_BRANCH,
                    "build_date": self._settings.COMET_BUILD_DATE,
                    "applied_revision": self._settings.APPLIED_SETTINGS_REVISION,
                    "pending_restart_keys_json": pending_restart_keys_json,
                    "readiness_json": readiness_json,
                    "restart_capable": int(restart_target_pid() is not None),
                },
            )
            await self._database.execute(
                """
                INSERT INTO runtime_processes (
                    instance_id, process_id, role, started_at, last_heartbeat
                ) VALUES (
                    :instance_id, :process_id, :role, :started_at, :last_heartbeat
                )
                ON CONFLICT (instance_id, process_id) DO UPDATE SET
                    role = excluded.role,
                    last_heartbeat = excluded.last_heartbeat
                """,
                {
                    "instance_id": self.identity.instance_id,
                    "process_id": pid,
                    "role": role,
                    "started_at": self.identity.started_at,
                    "last_heartbeat": now,
                },
            )

    async def expire_stale(self, *, observed_at: float | None = None) -> None:
        now = time.time() if observed_at is None else observed_at
        cutoff = now - self._settings.RUNTIME_STALE_SECONDS
        await self._database.execute(
            """
            DELETE FROM runtime_processes
            WHERE last_heartbeat < :cutoff
            """,
            {"cutoff": cutoff},
        )
        await self._database.execute(
            """
            DELETE FROM runtime_instances
            WHERE last_heartbeat < :cutoff
            """,
            {"cutoff": cutoff},
        )

    async def withdraw(self, *, process_id: int | None = None) -> None:
        pid = os.getpid() if process_id is None else process_id
        async with self._database.transaction():
            await self._database.execute(
                """
                DELETE FROM runtime_processes
                WHERE instance_id = :instance_id
                  AND process_id = :process_id
                """,
                {
                    "instance_id": self.identity.instance_id,
                    "process_id": pid,
                },
            )
            await self._database.execute(
                """
                DELETE FROM runtime_instances
                WHERE instance_id = :instance_id
                  AND NOT EXISTS (
                      SELECT 1
                      FROM runtime_processes
                      WHERE instance_id = :instance_id
                  )
                """,
                {"instance_id": self.identity.instance_id},
            )

    async def run_heartbeat(
        self,
        *,
        role: RuntimeRole,
        readiness: Callable[[], Awaitable[dict[str, Any]]],
    ) -> None:
        while True:
            await self.heartbeat(role=role, readiness=await readiness())
            await self.expire_stale()
            await asyncio.sleep(self._settings.RUNTIME_HEARTBEAT_INTERVAL)
