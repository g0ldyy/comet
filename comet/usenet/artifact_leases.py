"""Renewable database-visible leases over immutable shared artifacts."""

from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import suppress
from typing import ClassVar

from comet.observability.context import create_detached_task

READER_LEASE_SECONDS = 60
READER_HEARTBEAT_SECONDS = 20
RUNTIME_OWNER = str(uuid.uuid4())


class RenewableArtifactLease:
    """Renew and release one fixed-schema shared database lease."""

    _LEASE_IDENTITIES: ClassVar[dict[str, str]] = {
        "artifact_reader_leases": "artifact_sha256",
        "artifact_publication_leases": "preparation_id",
    }

    def __init__(
        self,
        database,
        *,
        table_name: str,
        lease_id: str,
        identity_value: str,
        lease_seconds: float = READER_LEASE_SECONDS,
        heartbeat_seconds: float = READER_HEARTBEAT_SECONDS,
    ):
        identity_field = self._LEASE_IDENTITIES.get(table_name)
        if identity_field is None:
            raise ValueError("unsupported artifact lease table")
        self._database = database
        self._table_name = table_name
        self._identity_field = identity_field
        self._identity_value = identity_value
        self._lease_seconds = lease_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self.lease_id = lease_id
        self._closed = False
        self._close_lock = asyncio.Lock()
        self._heartbeat_task = create_detached_task(
            self._heartbeat(),
            name="artifact-lease-heartbeat",
        )

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_seconds)
            current_time = time.time()
            try:
                await self._database.execute(
                    f"""
                    UPDATE {self._table_name}
                    SET heartbeat_at = :now, expires_at = :expires_at
                    WHERE lease_id = :lease_id
                      AND {self._identity_field} = :identity_value
                      AND runtime_owner = :runtime_owner
                    """,
                    {
                        "now": current_time,
                        "expires_at": current_time + self._lease_seconds,
                        "lease_id": self.lease_id,
                        "identity_value": self._identity_value,
                        "runtime_owner": RUNTIME_OWNER,
                    },
                    force_primary=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # The last persisted expiry remains authoritative. A later
                # heartbeat can recover after a transient database failure.
                continue

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._heartbeat_task
            await self._database.execute(
                f"""
                DELETE FROM {self._table_name}
                WHERE lease_id = :lease_id
                  AND {self._identity_field} = :identity_value
                  AND runtime_owner = :runtime_owner
                """,
                {
                    "lease_id": self.lease_id,
                    "identity_value": self._identity_value,
                    "runtime_owner": RUNTIME_OWNER,
                },
                force_primary=True,
            )
            self._closed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        await asyncio.shield(self.close())


class ArtifactReaderLease(RenewableArtifactLease):
    """Keep one shared immutable artifact alive until an idempotent close."""

    def __init__(
        self,
        database,
        *,
        lease_id: str,
        artifact_sha256: str,
        lease_seconds: float = READER_LEASE_SECONDS,
        heartbeat_seconds: float = READER_HEARTBEAT_SECONDS,
    ):
        self.artifact_sha256 = artifact_sha256
        super().__init__(
            database,
            table_name="artifact_reader_leases",
            lease_id=lease_id,
            identity_value=artifact_sha256,
            lease_seconds=lease_seconds,
            heartbeat_seconds=heartbeat_seconds,
        )
