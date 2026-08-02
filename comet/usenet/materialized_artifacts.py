"""Lifecycle ledger for immutable native materializations on shared storage."""

from __future__ import annotations

import asyncio
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from comet.services.lock import DistributedLock
from comet.usenet.artifact_leases import (
    READER_LEASE_SECONDS,
    RUNTIME_OWNER,
    ArtifactReaderLease,
    RenewableArtifactLease,
)
from comet.usenet.identity import is_sha256_hex, partition_hex
from comet.usenet.limits import MAX_NZB_FILES, MAX_USENET_LOGICAL_BYTES

_MAX_PREPARATION_ARTIFACTS = MAX_NZB_FILES
_PUBLICATION_LEASE_SECONDS = 5 * 60
_PUBLICATION_HEARTBEAT_SECONDS = 60
PUBLICATION_LEDGER_LOCK_KEY = "usenet:artifact-publication-ledger:v1"


class MaterializedArtifactError(RuntimeError):
    """The shared materialization ledger or physical object is inconsistent."""


def _canonical_preparation_id(value: object) -> str:
    try:
        return str(uuid.UUID(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("invalid playback preparation") from exc


def _validate_manifest_identity(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 68
        or not value.startswith("nm1:")
        or any(character not in "0123456789abcdef" for character in value[4:])
    ):
        raise ValueError("invalid materialization source identity")
    return value


@dataclass(frozen=True, slots=True)
class MaterializedArtifact:
    artifact_sha256: str
    byte_size: int
    selected_asset_id: str
    strong_asset_revision: str

    def __post_init__(self) -> None:
        if (
            not is_sha256_hex(self.artifact_sha256)
            or isinstance(self.byte_size, bool)
            or not isinstance(self.byte_size, int)
            or not 1 <= self.byte_size <= MAX_USENET_LOGICAL_BYTES
            or not is_sha256_hex(self.selected_asset_id)
            or not is_sha256_hex(self.strong_asset_revision)
        ):
            raise ValueError("invalid materialized artifact")


class ArtifactPublicationLease(RenewableArtifactLease):
    """Guard publication/reuse until its physical object enters the SQL ledger."""

    def __init__(self, database, lease_id: str, preparation_id: str):
        self.preparation_id = preparation_id
        super().__init__(
            database,
            table_name="artifact_publication_leases",
            lease_id=lease_id,
            identity_value=preparation_id,
            lease_seconds=_PUBLICATION_LEASE_SECONDS,
            heartbeat_seconds=_PUBLICATION_HEARTBEAT_SECONDS,
        )


class MaterializedArtifactRepository:
    """Associate published content-addressed files with durable preparations."""

    def __init__(self, artifact_dir: str | Path, database):
        self._artifact_dir = Path(artifact_dir)
        self._database = database

    def _path(self, artifact_sha256: str) -> Path:
        return self._artifact_dir / "materialized" / f"{artifact_sha256}.bin"

    async def acquire_publication_lease(
        self,
        preparation_id: str,
        *,
        owner_configuration_partition: bytes,
        now: float | None = None,
    ) -> ArtifactPublicationLease:
        """Serialize a renewable publication claim against orphan cleanup."""
        preparation_id = _canonical_preparation_id(preparation_id)
        partition = partition_hex(owner_configuration_partition)
        current_time = time.time() if now is None else now
        lease_id = str(uuid.uuid4())
        lock = DistributedLock(
            PUBLICATION_LEDGER_LOCK_KEY,
            timeout=60,
            retry_interval=0.05,
            database=self._database,
        )
        if not await lock.acquire(wait_timeout=30):
            raise MaterializedArtifactError("materialized artifact publication is busy")
        try:
            async with self._database.transaction():
                await self._database.execute(
                    """
                    DELETE FROM artifact_publication_leases
                    WHERE expires_at < :now
                    """,
                    {"now": current_time},
                    force_primary=True,
                )
                preparation = await self._database.fetch_one(
                    """
                    UPDATE asset_preparations
                    SET last_used_at = :now
                    WHERE preparation_id = :preparation_id
                      AND owner_configuration_partition = :partition
                      AND absolute_expires_at > :now
                    RETURNING preparation_id
                    """,
                    {
                        "preparation_id": preparation_id,
                        "partition": partition,
                        "now": current_time,
                    },
                    force_primary=True,
                )
                if preparation is None:
                    raise MaterializedArtifactError(
                        "playback preparation is unavailable"
                    )
                await self._database.execute(
                    """
                    INSERT INTO artifact_publication_leases (
                        lease_id, preparation_id, runtime_owner,
                        acquired_at, heartbeat_at, expires_at
                    ) VALUES (
                        :lease_id, :preparation_id, :runtime_owner,
                        :now, :now, :expires_at
                    )
                    """,
                    {
                        "lease_id": lease_id,
                        "preparation_id": preparation_id,
                        "runtime_owner": RUNTIME_OWNER,
                        "now": current_time,
                        "expires_at": current_time + _PUBLICATION_LEASE_SECONDS,
                    },
                    force_primary=True,
                )
        finally:
            await lock.release()
        return ArtifactPublicationLease(
            self._database,
            lease_id,
            preparation_id,
        )

    @staticmethod
    def _validate_physical(path: Path, expected_size: int) -> None:
        artifact_stat = path.lstat()
        if (
            not stat.S_ISREG(artifact_stat.st_mode)
            or artifact_stat.st_size != expected_size
            or artifact_stat.st_nlink != 1
            or artifact_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise MaterializedArtifactError("materialized artifact storage is corrupt")

    @staticmethod
    def _canonical_artifacts(
        artifacts: tuple[MaterializedArtifact, ...],
    ) -> tuple[MaterializedArtifact, ...]:
        if (
            not isinstance(artifacts, tuple)
            or not 1 <= len(artifacts) <= _MAX_PREPARATION_ARTIFACTS
            or any(not isinstance(item, MaterializedArtifact) for item in artifacts)
        ):
            raise ValueError("invalid preparation materializations")
        by_identity: dict[str, MaterializedArtifact] = {}
        for artifact in artifacts:
            previous = by_identity.setdefault(artifact.artifact_sha256, artifact)
            if (
                previous.byte_size != artifact.byte_size
                or previous.strong_asset_revision != artifact.strong_asset_revision
            ):
                raise ValueError("conflicting preparation materializations")
        return tuple(by_identity[identity] for identity in sorted(by_identity))

    async def register_for_preparation(
        self,
        preparation_id: str,
        *,
        owner_configuration_partition: bytes,
        source_nm1: str,
        artifacts: tuple[MaterializedArtifact, ...],
        now: float | None = None,
    ) -> None:
        """Register already-published files and pin them to one preparation."""
        preparation_id = _canonical_preparation_id(preparation_id)
        partition = partition_hex(owner_configuration_partition)
        source_nm1 = _validate_manifest_identity(source_nm1)
        artifacts = self._canonical_artifacts(artifacts)
        current_time = time.time() if now is None else now
        try:
            for artifact in artifacts:
                await asyncio.to_thread(
                    self._validate_physical,
                    self._path(artifact.artifact_sha256),
                    artifact.byte_size,
                )
        except OSError as exc:
            raise MaterializedArtifactError(
                "materialized artifact storage is unavailable"
            ) from exc

        async with self._database.transaction():
            preparation = await self._database.fetch_one(
                """
                UPDATE asset_preparations
                SET last_used_at = :now
                WHERE preparation_id = :preparation_id
                  AND owner_configuration_partition = :partition
                  AND absolute_expires_at > :now
                RETURNING preparation_id
                """,
                {
                    "preparation_id": preparation_id,
                    "partition": partition,
                    "now": current_time,
                },
                force_primary=True,
            )
            if preparation is None:
                raise MaterializedArtifactError("playback preparation is unavailable")
            for artifact in artifacts:
                relative_path = f"materialized/{artifact.artifact_sha256}.bin"
                row = await self._database.fetch_one(
                    """
                    INSERT INTO nzb_artifacts (
                        artifact_sha256, byte_size, storage_kind,
                        relative_path, publication_state, refcount,
                        source_manifest_identity, selected_asset_id,
                        strong_asset_revision, logical_length,
                        created_at, last_used_at, tombstoned_at
                    ) VALUES (
                        :artifact_sha256, :byte_size, 'materialized_asset',
                        :relative_path, 'published', 0,
                        :source_nm1, :selected_asset_id,
                        :strong_asset_revision, :byte_size,
                        :now, :now, NULL
                    )
                    ON CONFLICT (artifact_sha256) DO UPDATE SET
                        publication_state = 'published',
                        last_used_at = EXCLUDED.last_used_at,
                        tombstoned_at = NULL
                    WHERE nzb_artifacts.storage_kind = 'materialized_asset'
                      AND nzb_artifacts.byte_size = EXCLUDED.byte_size
                      AND nzb_artifacts.relative_path = EXCLUDED.relative_path
                      AND nzb_artifacts.strong_asset_revision =
                          EXCLUDED.strong_asset_revision
                    RETURNING artifact_sha256
                    """,
                    {
                        "artifact_sha256": artifact.artifact_sha256,
                        "byte_size": artifact.byte_size,
                        "relative_path": relative_path,
                        "source_nm1": source_nm1,
                        "selected_asset_id": artifact.selected_asset_id,
                        "strong_asset_revision": artifact.strong_asset_revision,
                        "now": current_time,
                    },
                    force_primary=True,
                )
                if row is None:
                    raise MaterializedArtifactError(
                        "materialized artifact identity conflicts with its ledger"
                    )
                await self._database.execute(
                    """
                    INSERT INTO asset_preparation_artifacts (
                        preparation_id, artifact_sha256, created_at
                    ) VALUES (
                        :preparation_id, :artifact_sha256, :now
                    )
                    ON CONFLICT (preparation_id, artifact_sha256) DO NOTHING
                    """,
                    {
                        "preparation_id": preparation_id,
                        "artifact_sha256": artifact.artifact_sha256,
                        "now": current_time,
                    },
                    force_primary=True,
                )
                await self._database.execute(
                    """
                    UPDATE nzb_artifacts
                    SET refcount = (
                        SELECT COUNT(*)
                        FROM asset_preparation_artifacts AS links
                        WHERE links.artifact_sha256 = :artifact_sha256
                    )
                    WHERE artifact_sha256 = :artifact_sha256
                      AND storage_kind = 'materialized_asset'
                    """,
                    {"artifact_sha256": artifact.artifact_sha256},
                    force_primary=True,
                )

    async def acquire_for_preparation(
        self,
        preparation_id: str,
        *,
        owner_configuration_partition: bytes,
        now: float | None = None,
    ) -> tuple[ArtifactReaderLease, ...]:
        """Lease every shared object used by an owner-bound preparation."""
        preparation_id = _canonical_preparation_id(preparation_id)
        current_time = time.time() if now is None else now
        partition = partition_hex(owner_configuration_partition)
        leased: list[tuple[str, str, int, Path]] = []
        async with self._database.transaction():
            rows = await self._database.fetch_all(
                """
                SELECT artifacts.artifact_sha256, artifacts.byte_size,
                       artifacts.relative_path
                FROM asset_preparation_artifacts AS links
                JOIN asset_preparations AS preparations
                  ON preparations.preparation_id = links.preparation_id
                JOIN nzb_artifacts AS artifacts
                  ON artifacts.artifact_sha256 = links.artifact_sha256
                WHERE links.preparation_id = :preparation_id
                  AND preparations.owner_configuration_partition = :partition
                  AND preparations.absolute_expires_at > :now
                  AND artifacts.storage_kind = 'materialized_asset'
                  AND artifacts.publication_state = 'published'
                ORDER BY artifacts.artifact_sha256
                LIMIT :limit
                """,
                {
                    "preparation_id": preparation_id,
                    "partition": partition,
                    "now": current_time,
                    "limit": _MAX_PREPARATION_ARTIFACTS + 1,
                },
                force_primary=True,
            )
            if len(rows) > _MAX_PREPARATION_ARTIFACTS:
                raise MaterializedArtifactError(
                    "playback preparation has too many materializations"
                )
            for row in rows:
                artifact_sha256 = row["artifact_sha256"]
                admitted = await self._database.fetch_one(
                    """
                    UPDATE nzb_artifacts
                    SET last_used_at = :now
                    WHERE artifact_sha256 = :artifact_sha256
                      AND storage_kind = 'materialized_asset'
                      AND publication_state = 'published'
                      AND EXISTS (
                          SELECT 1
                          FROM asset_preparation_artifacts AS links
                          JOIN asset_preparations AS preparations
                            ON preparations.preparation_id =
                               links.preparation_id
                          WHERE links.artifact_sha256 =
                                nzb_artifacts.artifact_sha256
                            AND links.preparation_id = :preparation_id
                            AND preparations.owner_configuration_partition =
                                :partition
                            AND preparations.absolute_expires_at > :now
                      )
                    RETURNING byte_size, relative_path
                    """,
                    {
                        "artifact_sha256": artifact_sha256,
                        "preparation_id": preparation_id,
                        "partition": partition,
                        "now": current_time,
                    },
                    force_primary=True,
                )
                if admitted is None:
                    raise MaterializedArtifactError(
                        "materialized artifact is unavailable"
                    )
                relative_path = admitted["relative_path"]
                expected_relative = f"materialized/{artifact_sha256}.bin"
                if relative_path != expected_relative:
                    raise MaterializedArtifactError(
                        "materialized artifact path is corrupt"
                    )
                lease_id = str(uuid.uuid4())
                await self._database.execute(
                    """
                    INSERT INTO artifact_reader_leases (
                        lease_id, artifact_sha256, runtime_owner,
                        acquired_at, heartbeat_at, expires_at
                    ) VALUES (
                        :lease_id, :artifact_sha256, :runtime_owner,
                        :now, :now, :expires_at
                    )
                    """,
                    {
                        "lease_id": lease_id,
                        "artifact_sha256": artifact_sha256,
                        "runtime_owner": RUNTIME_OWNER,
                        "now": current_time,
                        "expires_at": current_time + READER_LEASE_SECONDS,
                    },
                    force_primary=True,
                )
                leased.append(
                    (
                        lease_id,
                        artifact_sha256,
                        admitted["byte_size"],
                        self._path(artifact_sha256),
                    )
                )

        try:
            for _lease_id, _identity, byte_size, path in leased:
                await asyncio.to_thread(
                    self._validate_physical,
                    path,
                    byte_size,
                )
        except (OSError, MaterializedArtifactError) as exc:
            for lease_id, _identity, _size, _path in leased:
                await self._database.execute(
                    """
                    DELETE FROM artifact_reader_leases
                    WHERE lease_id = :lease_id
                      AND runtime_owner = :runtime_owner
                    """,
                    {
                        "lease_id": lease_id,
                        "runtime_owner": RUNTIME_OWNER,
                    },
                    force_primary=True,
                )
            if isinstance(exc, MaterializedArtifactError):
                raise
            raise MaterializedArtifactError(
                "materialized artifact storage is unavailable"
            ) from exc
        return tuple(
            ArtifactReaderLease(
                self._database,
                lease_id=lease_id,
                artifact_sha256=artifact_sha256,
            )
            for lease_id, artifact_sha256, _byte_size, _path in leased
        )

    async def retain_for_preparation(
        self,
        preparation_id: str,
        *,
        owner_configuration_partition: bytes,
        artifact_sha256s: tuple[str, ...],
        now: float | None = None,
    ) -> None:
        """Release intermediate objects after the final byte path is known."""
        preparation_id = _canonical_preparation_id(preparation_id)
        if (
            not isinstance(artifact_sha256s, tuple)
            or len(artifact_sha256s) > _MAX_PREPARATION_ARTIFACTS
            or any(not is_sha256_hex(value) for value in artifact_sha256s)
        ):
            raise ValueError("invalid preparation materializations")
        retained = frozenset(artifact_sha256s)
        current_time = time.time() if now is None else now
        async with self._database.transaction():
            preparation = await self._database.fetch_one(
                """
                UPDATE asset_preparations
                SET last_used_at = :now
                WHERE preparation_id = :preparation_id
                  AND owner_configuration_partition = :partition
                  AND absolute_expires_at > :now
                RETURNING preparation_id
                """,
                {
                    "preparation_id": preparation_id,
                    "partition": partition_hex(owner_configuration_partition),
                    "now": current_time,
                },
                force_primary=True,
            )
            if preparation is None:
                raise MaterializedArtifactError("playback preparation is unavailable")
            rows = await self._database.fetch_all(
                """
                SELECT artifact_sha256
                FROM asset_preparation_artifacts
                WHERE preparation_id = :preparation_id
                """,
                {"preparation_id": preparation_id},
                force_primary=True,
            )
            linked = {row["artifact_sha256"] for row in rows}
            if not retained.issubset(linked):
                raise MaterializedArtifactError(
                    "required materialized artifact is not registered"
                )
            removed = linked - retained
            for artifact_sha256 in removed:
                await self._database.execute(
                    """
                    DELETE FROM asset_preparation_artifacts
                    WHERE preparation_id = :preparation_id
                      AND artifact_sha256 = :artifact_sha256
                    """,
                    {
                        "preparation_id": preparation_id,
                        "artifact_sha256": artifact_sha256,
                    },
                    force_primary=True,
                )
                await self._database.execute(
                    """
                    UPDATE nzb_artifacts
                    SET refcount = (
                        SELECT COUNT(*)
                        FROM asset_preparation_artifacts AS links
                        WHERE links.artifact_sha256 = :artifact_sha256
                    )
                    WHERE artifact_sha256 = :artifact_sha256
                      AND storage_kind = 'materialized_asset'
                    """,
                    {"artifact_sha256": artifact_sha256},
                    force_primary=True,
                )
