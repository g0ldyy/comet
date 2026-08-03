"""Bounded shared cleanup for immutable NZB artifacts."""

import asyncio
import hashlib
import os
import stat
import time
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

from comet.services.lock import DistributedLock
from comet.usenet.identity import is_sha256_hex
from comet.usenet.materialized_artifacts import PUBLICATION_LEDGER_LOCK_KEY

_MAX_GC_BATCH = 256
_UNREFERENCED_GRACE_SECONDS = 5 * 60
_ORPHAN_TEMP_PREFIX = ".publish-"
_MAX_ORPHAN_HASH_BYTES_PER_SWEEP = 1024 * 1024 * 1024
_ORPHAN_SCAN_MULTIPLIER = 8
_LEASE_TABLES = (
    "artifact_reader_leases",
    "artifact_publication_leases",
)


@dataclass(frozen=True, slots=True)
class ArtifactGcResult:
    expired_leases: int
    expired_grants: int
    deleted_artifacts: int
    reconciled_orphans: int


class SharedArtifactGarbageCollector:
    def __init__(self, artifact_dir: str | Path, database):
        self._artifact_dir = Path(artifact_dir)
        self._database = database

    async def collect(
        self,
        *,
        now: float | None = None,
        limit: int = _MAX_GC_BATCH,
    ) -> ArtifactGcResult:
        current_time = time.time() if now is None else now
        expired_leases = 0
        for table_name in _LEASE_TABLES:
            expired_leases += await self._delete_expired_lease_rows(
                table_name,
                current_time,
                limit,
            )
        expired_grants = await self._delete_expired_grants(current_time, limit)
        await self._synchronize_materialized_refcounts()
        deleted_artifacts = await self._delete_unreferenced_artifacts(
            current_time,
            limit,
        )
        reconciled_orphans = await self._reconcile_crashed_publications(
            current_time,
            limit,
        )
        return ArtifactGcResult(
            expired_leases,
            expired_grants,
            deleted_artifacts,
            reconciled_orphans,
        )

    async def prune(self, artifact_sha256: str) -> bool:
        """Delete one currently unreferenced artifact through the canonical GC path."""
        if not is_sha256_hex(artifact_sha256):
            raise ValueError("artifact identity is invalid")
        await self._synchronize_materialized_refcounts()
        return await self._delete_one(artifact_sha256, time.time())

    async def _reconcile_crashed_publications(
        self,
        current_time: float,
        limit: int,
    ) -> int:
        directory = self._artifact_dir / "materialized"
        try:
            candidates = await asyncio.to_thread(
                self._orphan_candidates,
                directory,
                current_time,
                limit,
            )
        except OSError:
            return 0
        if not candidates:
            return 0
        lock = DistributedLock(
            PUBLICATION_LEDGER_LOCK_KEY,
            timeout=60,
            retry_interval=0.05,
            database=self._database,
        )
        if not await lock.acquire():
            return 0

        async def reconcile() -> int:
            active = await self._database.fetch_one(
                """
                SELECT 1
                FROM artifact_publication_leases
                WHERE expires_at >= :now
                LIMIT 1
                """,
                {"now": current_time},
                force_primary=True,
            )
            if active is not None:
                return 0
            removed = 0
            for path in candidates:
                if path.name.startswith(_ORPHAN_TEMP_PREFIX):
                    try:
                        await asyncio.to_thread(
                            self._unlink_old_regular,
                            path,
                            current_time,
                        )
                    except (OSError, ValueError):
                        continue
                    removed += 1
                    continue
                artifact_sha256 = path.name.removesuffix(".bin")
                registered = await self._database.fetch_one(
                    """
                    SELECT 1
                    FROM nzb_artifacts
                    WHERE artifact_sha256 = :artifact_sha256
                    LIMIT 1
                    """,
                    {"artifact_sha256": artifact_sha256},
                    force_primary=True,
                )
                if registered is not None:
                    continue
                try:
                    await asyncio.to_thread(
                        self._unlink_verified_orphan,
                        path,
                        artifact_sha256,
                        current_time,
                    )
                except (OSError, ValueError):
                    continue
                removed += 1
            return removed

        try:
            return await lock.run(reconcile())
        finally:
            await lock.release()

    @staticmethod
    def _orphan_candidates(
        directory: Path,
        current_time: float,
        limit: int,
    ) -> tuple[Path, ...]:
        cutoff = current_time - _UNREFERENCED_GRACE_SECONDS
        candidates = []
        for entry in islice(
            directory.iterdir(),
            limit * _ORPHAN_SCAN_MULTIPLIER,
        ):
            name = entry.name
            identity = name.removesuffix(".bin")
            if not (
                name.startswith(_ORPHAN_TEMP_PREFIX)
                or name.endswith(".bin")
                and is_sha256_hex(identity)
            ):
                continue
            metadata = entry.lstat()
            if (
                stat.S_ISREG(metadata.st_mode)
                and metadata.st_nlink == 1
                and metadata.st_mtime < cutoff
            ):
                candidates.append((entry, metadata.st_size))
        selected = []
        selected_bytes = 0
        for entry, byte_size in sorted(
            candidates,
            key=lambda item: item[0].name,
        ):
            if len(selected) >= limit:
                break
            if (
                selected
                and selected_bytes + byte_size > _MAX_ORPHAN_HASH_BYTES_PER_SWEEP
            ):
                break
            selected.append(entry)
            selected_bytes += byte_size
        return tuple(selected)

    @staticmethod
    def _open_old_regular(
        path: Path,
        current_time: float,
    ) -> tuple[int, tuple[int, ...]]:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_mtime >= current_time - _UNREFERENCED_GRACE_SECONDS
            ):
                raise ValueError("orphan materialization changed")
            return descriptor, SharedArtifactGarbageCollector._physical_identity(
                metadata
            )
        except Exception:
            os.close(descriptor)
            raise

    @classmethod
    def _unlink_old_regular(
        cls,
        path: Path,
        current_time: float,
    ) -> None:
        descriptor, initial = cls._open_old_regular(path, current_time)
        try:
            if cls._physical_identity(os.fstat(descriptor)) != initial:
                raise ValueError("orphan materialization changed")
            path.unlink()
        finally:
            os.close(descriptor)
        cls._sync_directory(path.parent)

    @classmethod
    def _unlink_verified_orphan(
        cls,
        path: Path,
        artifact_sha256: str,
        current_time: float,
    ) -> None:
        descriptor, initial = cls._open_old_regular(path, current_time)
        digest = hashlib.sha256()
        try:
            with os.fdopen(descriptor, "rb", closefd=False) as artifact:
                for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
                    digest.update(chunk)
            if (
                digest.hexdigest() != artifact_sha256
                or cls._physical_identity(os.fstat(descriptor)) != initial
            ):
                raise ValueError("orphan materialization identity mismatch")
            path.unlink()
        finally:
            os.close(descriptor)
        cls._sync_directory(path.parent)

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        descriptor = os.open(
            directory,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _physical_identity(metadata: os.stat_result) -> tuple[int, ...]:
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_nlink,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )

    async def _delete_expired_lease_rows(
        self,
        table_name: str,
        current_time: float,
        limit: int,
    ) -> int:
        rows = await self._database.fetch_all(
            f"""
            SELECT lease_id
            FROM {table_name}
            WHERE expires_at < :now
            ORDER BY expires_at, lease_id
            LIMIT :limit
            """,
            {"now": current_time, "limit": limit},
            force_primary=True,
        )
        deleted = 0
        for row in rows:
            removed = await self._database.fetch_one(
                f"""
                DELETE FROM {table_name}
                WHERE lease_id = :lease_id
                  AND expires_at < :now
                RETURNING lease_id
                """,
                {"lease_id": row["lease_id"], "now": current_time},
                force_primary=True,
            )
            deleted += removed is not None
        return deleted

    async def _synchronize_materialized_refcounts(self) -> None:
        # Playback-preparation deletion cascades its association rows. Refresh
        # the denormalized count before selecting GC candidates so a crash or
        # expired preparation can only delay, never accelerate, deletion.
        await self._database.execute(
            """
            UPDATE nzb_artifacts
            SET refcount = (
                SELECT COUNT(*)
                FROM asset_preparation_artifacts AS links
                WHERE links.artifact_sha256 =
                      nzb_artifacts.artifact_sha256
            )
            WHERE storage_kind = 'materialized_asset'
            """,
            force_primary=True,
        )

    async def _delete_expired_grants(
        self,
        current_time: float,
        limit: int,
    ) -> int:
        rows = await self._database.fetch_all(
            """
            SELECT grants.grant_id
            FROM nzb_artifact_grants AS grants
            WHERE grants.expires_at < :now
              AND NOT EXISTS (
                  SELECT 1
                  FROM nzb_provider_exports AS exports
                  WHERE exports.grant_id = grants.grant_id
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM provider_preparations AS preparations
                  WHERE preparations.artifact_grant_id = grants.grant_id
              )
            ORDER BY grants.expires_at, grants.grant_id
            LIMIT :limit
            """,
            {"now": current_time, "limit": limit},
            force_primary=True,
        )
        deleted = 0
        for row in rows:
            async with self._database.transaction():
                removed = await self._database.fetch_one(
                    """
                    DELETE FROM nzb_artifact_grants
                    WHERE grant_id = :grant_id
                      AND expires_at < :now
                      AND NOT EXISTS (
                          SELECT 1
                          FROM nzb_provider_exports AS exports
                          WHERE exports.grant_id =
                                nzb_artifact_grants.grant_id
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM provider_preparations AS preparations
                          WHERE preparations.artifact_grant_id =
                                nzb_artifact_grants.grant_id
                      )
                    RETURNING artifact_sha256
                    """,
                    {"grant_id": row["grant_id"], "now": current_time},
                    force_primary=True,
                )
                if removed is None:
                    continue
                artifact_sha256 = removed["artifact_sha256"]
                await self._database.execute(
                    """
                    UPDATE nzb_artifacts
                    SET refcount = (
                        SELECT COUNT(*)
                        FROM nzb_artifact_grants AS grants
                        WHERE grants.artifact_sha256 = :artifact_sha256
                    )
                    WHERE artifact_sha256 = :artifact_sha256
                    """,
                    {"artifact_sha256": artifact_sha256},
                    force_primary=True,
                )
                deleted += 1
        return deleted

    async def _delete_unreferenced_artifacts(
        self,
        current_time: float,
        limit: int,
    ) -> int:
        rows = await self._database.fetch_all(
            """
            SELECT artifact_sha256
            FROM nzb_artifacts
            WHERE publication_state IN ('published', 'tombstoned')
              AND refcount = 0
              AND last_used_at < :cutoff
              AND (
                  (
                      storage_kind = 'nzb'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM nzb_artifact_grants AS grants
                          WHERE grants.artifact_sha256 =
                                nzb_artifacts.artifact_sha256
                      )
                  )
                  OR (
                      storage_kind = 'materialized_asset'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM asset_preparation_artifacts AS links
                          WHERE links.artifact_sha256 =
                                nzb_artifacts.artifact_sha256
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
                  WHERE leases.artifact_sha256 =
                        nzb_artifacts.artifact_sha256
                    AND leases.expires_at >= :now
              )
            ORDER BY last_used_at, artifact_sha256
            LIMIT :limit
            """,
            {
                "cutoff": current_time - _UNREFERENCED_GRACE_SECONDS,
                "now": current_time,
                "limit": limit,
            },
            force_primary=True,
        )
        deleted = 0
        for row in rows:
            if await self._delete_one(row["artifact_sha256"], current_time):
                deleted += 1
        return deleted

    async def _delete_one(
        self,
        artifact_sha256: str,
        current_time: float,
    ) -> bool:
        try:
            # Commit the tombstone before touching the filesystem. If this
            # process dies after unlink, a later sweep sees a durable tombstone
            # instead of rolling the row back to a false "published" state.
            async with self._database.transaction():
                candidate = await self._database.fetch_one(
                    """
                    UPDATE nzb_artifacts
                    SET publication_state = 'tombstoned',
                        tombstoned_at = COALESCE(tombstoned_at, :now)
                    WHERE artifact_sha256 = :artifact_sha256
                      AND storage_kind IN ('nzb', 'materialized_asset')
                      AND publication_state IN ('published', 'tombstoned')
                      AND refcount = 0
                      AND last_used_at < :cutoff
                      AND (
                          (
                              storage_kind = 'nzb'
                              AND NOT EXISTS (
                                  SELECT 1
                                  FROM nzb_artifact_grants AS grants
                                  WHERE grants.artifact_sha256 =
                                        nzb_artifacts.artifact_sha256
                              )
                          )
                          OR (
                              storage_kind = 'materialized_asset'
                              AND NOT EXISTS (
                                  SELECT 1
                                  FROM asset_preparation_artifacts AS links
                                  WHERE links.artifact_sha256 =
                                        nzb_artifacts.artifact_sha256
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
                          WHERE leases.artifact_sha256 =
                                nzb_artifacts.artifact_sha256
                            AND leases.expires_at >= :now
                      )
                    RETURNING storage_kind, relative_path, byte_size
                    """,
                    {
                        "artifact_sha256": artifact_sha256,
                        "now": current_time,
                        "cutoff": (current_time - _UNREFERENCED_GRACE_SECONDS),
                    },
                    force_primary=True,
                )
                if candidate is None:
                    return False
                expected_relative = (
                    f"nzb/{artifact_sha256}.nzb"
                    if candidate["storage_kind"] == "nzb"
                    else f"materialized/{artifact_sha256}.bin"
                )
                if candidate["relative_path"] != expected_relative:
                    raise ValueError("artifact GC path mismatch")

            path = self._artifact_dir / expected_relative
            # Re-lock the durable tombstone through unlink and metadata
            # deletion. A concurrent resurrection updates this same row first,
            # causing the conditional claim below to fail after it waits.
            async with self._database.transaction():
                claimed = await self._database.fetch_one(
                    """
                    UPDATE nzb_artifacts
                    SET tombstoned_at = tombstoned_at
                    WHERE artifact_sha256 = :artifact_sha256
                      AND storage_kind IN ('nzb', 'materialized_asset')
                      AND publication_state = 'tombstoned'
                      AND refcount = 0
                      AND last_used_at < :cutoff
                      AND (
                          (
                              storage_kind = 'nzb'
                              AND NOT EXISTS (
                                  SELECT 1
                                  FROM nzb_artifact_grants AS grants
                                  WHERE grants.artifact_sha256 =
                                        nzb_artifacts.artifact_sha256
                              )
                          )
                          OR (
                              storage_kind = 'materialized_asset'
                              AND NOT EXISTS (
                                  SELECT 1
                                  FROM asset_preparation_artifacts AS links
                                  WHERE links.artifact_sha256 =
                                        nzb_artifacts.artifact_sha256
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
                          WHERE leases.artifact_sha256 =
                                nzb_artifacts.artifact_sha256
                            AND leases.expires_at >= :now
                      )
                    RETURNING storage_kind, relative_path, byte_size
                    """,
                    {
                        "artifact_sha256": artifact_sha256,
                        "now": current_time,
                        "cutoff": (current_time - _UNREFERENCED_GRACE_SECONDS),
                    },
                    force_primary=True,
                )
                if claimed is None:
                    return False
                if (
                    claimed["storage_kind"] != candidate["storage_kind"]
                    or claimed["relative_path"] != expected_relative
                    or claimed["byte_size"] != candidate["byte_size"]
                ):
                    raise ValueError("artifact GC path mismatch")
                await asyncio.to_thread(
                    self._unlink_exact_regular,
                    path,
                    candidate["byte_size"],
                )
                await self._database.execute(
                    """
                    UPDATE nzb_contents
                    SET artifact_sha256 = (
                        SELECT replacement.artifact_sha256
                        FROM nzb_artifacts AS current
                        JOIN nzb_artifacts AS replacement
                          ON replacement.source_manifest_identity =
                             current.source_manifest_identity
                         AND replacement.storage_kind = 'nzb'
                         AND replacement.publication_state = 'published'
                         AND replacement.artifact_sha256 <>
                             current.artifact_sha256
                        WHERE current.artifact_sha256 = :artifact_sha256
                        ORDER BY replacement.created_at,
                                 replacement.artifact_sha256
                        LIMIT 1
                    )
                    WHERE artifact_sha256 = :artifact_sha256
                      AND EXISTS (
                          SELECT 1
                          FROM nzb_artifacts AS current
                          JOIN nzb_artifacts AS replacement
                            ON replacement.source_manifest_identity =
                               current.source_manifest_identity
                           AND replacement.storage_kind = 'nzb'
                           AND replacement.publication_state = 'published'
                           AND replacement.artifact_sha256 <>
                               current.artifact_sha256
                          WHERE current.artifact_sha256 = :artifact_sha256
                      )
                    """,
                    {"artifact_sha256": artifact_sha256},
                    force_primary=True,
                )
                await self._database.execute(
                    """
                    DELETE FROM nzb_contents
                    WHERE artifact_sha256 = :artifact_sha256
                    """,
                    {"artifact_sha256": artifact_sha256},
                    force_primary=True,
                )
                removed = await self._database.fetch_one(
                    """
                    DELETE FROM nzb_artifacts
                    WHERE artifact_sha256 = :artifact_sha256
                      AND publication_state = 'tombstoned'
                      AND refcount = 0
                      AND (
                          (
                              storage_kind = 'nzb'
                              AND NOT EXISTS (
                                  SELECT 1
                                  FROM nzb_artifact_grants AS grants
                                  WHERE grants.artifact_sha256 =
                                        nzb_artifacts.artifact_sha256
                              )
                          )
                          OR (
                              storage_kind = 'materialized_asset'
                              AND NOT EXISTS (
                                  SELECT 1
                                  FROM asset_preparation_artifacts AS links
                                  WHERE links.artifact_sha256 =
                                        nzb_artifacts.artifact_sha256
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
                          WHERE leases.artifact_sha256 =
                                nzb_artifacts.artifact_sha256
                            AND leases.expires_at >= :now
                      )
                    RETURNING artifact_sha256
                    """,
                    {
                        "artifact_sha256": artifact_sha256,
                        "now": current_time,
                    },
                    force_primary=True,
                )
                return removed is not None
        except (OSError, ValueError):
            return False

    @staticmethod
    def _unlink_exact_regular(path: Path, expected_size: int) -> None:
        try:
            artifact_stat = path.lstat()
        except FileNotFoundError:
            return
        if (
            not stat.S_ISREG(artifact_stat.st_mode)
            or artifact_stat.st_size != expected_size
            or artifact_stat.st_nlink != 1
            or artifact_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise ValueError("artifact GC refuses an inconsistent object")
        path.unlink()
        directory = os.open(
            path.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
