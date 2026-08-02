import os
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from databases import Database

from comet.core.db_router import ReplicaAwareDatabase
from comet.core.schema_migrations import (
    MigrationContext,
    _ensure_usenet_schema,
)
from comet.usenet.artifact_gc import (
    SharedArtifactGarbageCollector,
)
from comet.usenet.artifact_leases import ArtifactReaderLease


class ArtifactReaderLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_can_retry_after_a_transient_database_failure(self):
        class Database:
            def __init__(self):
                self.attempts = 0

            async def execute(self, *_args, **_kwargs):
                self.attempts += 1
                if self.attempts == 1:
                    raise RuntimeError("transient database failure")

        database = Database()
        lease = ArtifactReaderLease(
            database,
            lease_id=str(uuid.uuid4()),
            artifact_sha256="a" * 64,
        )

        with self.assertRaisesRegex(RuntimeError, "transient database failure"):
            await lease.close()
        await lease.close()
        await lease.close()

        self.assertEqual(database.attempts, 2)


class SharedArtifactGarbageCollectorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name) / "artifacts"
        (self.root / "nzb").mkdir(parents=True)
        self.database = ReplicaAwareDatabase(
            Database(f"sqlite+aiosqlite:///{self.temporary.name}/artifact-gc.db")
        )
        await self.database.connect()
        context = MigrationContext(
            self.database,
            is_sqlite=True,
            is_postgres=False,
        )
        await _ensure_usenet_schema(context)

    async def asyncTearDown(self):
        await self.database.disconnect()
        self.temporary.cleanup()

    async def test_orphan_sweep_uses_the_byte_budget_for_small_files(self):
        materialized = self.root / "materialized"
        materialized.mkdir()
        for index in range(16):
            path = materialized / f"{index:064x}.bin"
            path.write_bytes(b"x")
            os.utime(path, (1, 1))

        candidates = SharedArtifactGarbageCollector._orphan_candidates(
            materialized,
            current_time=1_000,
            limit=16,
        )

        self.assertEqual(len(candidates), 16)

    async def _artifact(
        self,
        character: str,
        *,
        grant_expires_at: float | None,
    ) -> tuple[str, str | None, Path]:
        artifact_sha256 = character * 64
        relative_path = f"nzb/{artifact_sha256}.nzb"
        path = self.root / relative_path
        path.write_bytes(character.encode())
        await self.database.execute(
            """
            INSERT INTO nzb_artifacts (
                artifact_sha256, byte_size, relative_path,
                source_manifest_identity, created_at, last_used_at
            ) VALUES (
                :artifact_sha256, 1, :relative_path, :nm1, 1, 1
            )
            """,
            {
                "artifact_sha256": artifact_sha256,
                "relative_path": relative_path,
                "nm1": "nm1:" + "b" * 64,
            },
        )
        if grant_expires_at is None:
            return artifact_sha256, None, path
        grant_id = str(uuid.uuid4())
        await self.database.execute(
            """
            INSERT INTO nzb_artifact_grants (
                grant_id, artifact_sha256, owner_configuration_partition,
                created_at, last_used_at, expires_at
            ) VALUES (
                :grant_id, :artifact_sha256, :partition, 1, 1, :expires_at
            )
            """,
            {
                "grant_id": grant_id,
                "artifact_sha256": artifact_sha256,
                "partition": "f" * 64,
                "expires_at": grant_expires_at,
            },
        )
        await self.database.execute(
            """
            UPDATE nzb_artifacts
            SET refcount = 1
            WHERE artifact_sha256 = :artifact_sha256
            """,
            {"artifact_sha256": artifact_sha256},
        )
        return artifact_sha256, grant_id, path

    async def test_expired_unreferenced_grant_and_file_are_deleted(self):
        artifact_sha256, _grant_id, path = await self._artifact(
            "a",
            grant_expires_at=100,
        )

        result = await SharedArtifactGarbageCollector(
            self.root,
            self.database,
        ).collect(now=1_000)

        self.assertEqual(result.expired_grants, 1)
        self.assertEqual(result.deleted_artifacts, 1)
        self.assertFalse(path.exists())
        self.assertIsNone(
            await self.database.fetch_one(
                """
                SELECT artifact_sha256
                FROM nzb_artifacts
                WHERE artifact_sha256 = :artifact_sha256
                """,
                {"artifact_sha256": artifact_sha256},
            )
        )

    async def test_live_reader_defers_unlink_until_its_expiry(self):
        artifact_sha256, _grant_id, path = await self._artifact(
            "b",
            grant_expires_at=100,
        )
        await self.database.execute(
            """
            INSERT INTO artifact_reader_leases (
                lease_id, artifact_sha256, runtime_owner,
                acquired_at, heartbeat_at, expires_at
            ) VALUES (
                :lease_id, :artifact_sha256, :runtime_owner, 1, 1, 2_000
            )
            """,
            {
                "lease_id": str(uuid.uuid4()),
                "artifact_sha256": artifact_sha256,
                "runtime_owner": str(uuid.uuid4()),
            },
        )
        collector = SharedArtifactGarbageCollector(self.root, self.database)

        first = await collector.collect(now=1_000)
        self.assertEqual(first.expired_grants, 1)
        self.assertEqual(first.deleted_artifacts, 0)
        self.assertTrue(path.exists())

        second = await collector.collect(now=2_001)
        self.assertEqual(second.expired_leases, 1)
        self.assertEqual(second.deleted_artifacts, 1)
        self.assertFalse(path.exists())

    async def test_content_handle_moves_to_an_equivalent_live_artifact(self):
        stale_sha, _grant_id, stale_path = await self._artifact(
            "d",
            grant_expires_at=None,
        )
        live_sha, _grant_id, live_path = await self._artifact(
            "e",
            grant_expires_at=2_000,
        )
        await self.database.execute(
            """
            INSERT INTO nzb_contents (
                manifest_identity, parser_version,
                posting_set_identity, artifact_sha256,
                manifest_json, inspection_state,
                created_at, last_used_at
            ) VALUES (
                :nm1, 1, :nh1, :artifact_sha256,
                '[]', 'parsed', 1, 1
            )
            """,
            {
                "nm1": "nm1:" + "b" * 64,
                "nh1": "nh1:" + "c" * 40,
                "artifact_sha256": stale_sha,
            },
        )

        result = await SharedArtifactGarbageCollector(
            self.root,
            self.database,
        ).collect(now=1_000)

        self.assertEqual(result.deleted_artifacts, 1)
        self.assertFalse(stale_path.exists())
        self.assertTrue(live_path.exists())
        content = await self.database.fetch_one(
            "SELECT artifact_sha256 FROM nzb_contents"
        )
        self.assertEqual(content["artifact_sha256"], live_sha)

    async def test_non_regular_object_fails_closed_and_keeps_metadata(self):
        artifact_sha256 = "c" * 64
        relative_path = f"nzb/{artifact_sha256}.nzb"
        outside = Path(self.temporary.name) / "outside.nzb"
        outside.write_bytes(b"outside")
        (self.root / relative_path).symlink_to(outside)
        await self.database.execute(
            """
            INSERT INTO nzb_artifacts (
                artifact_sha256, byte_size, relative_path,
                source_manifest_identity, created_at, last_used_at
            ) VALUES (
                :artifact_sha256, 1, :relative_path, :nm1, 1, 1
            )
            """,
            {
                "artifact_sha256": artifact_sha256,
                "relative_path": relative_path,
                "nm1": "nm1:" + "b" * 64,
            },
        )

        result = await SharedArtifactGarbageCollector(
            self.root,
            self.database,
        ).collect(now=1_000)

        self.assertEqual(result.deleted_artifacts, 0)
        self.assertTrue(outside.exists())
        row = await self.database.fetch_one(
            """
            SELECT publication_state
            FROM nzb_artifacts
            WHERE artifact_sha256 = :artifact_sha256
            """,
            {"artifact_sha256": artifact_sha256},
        )
        self.assertEqual(row["publication_state"], "tombstoned")

    async def test_inconsistent_regular_objects_fail_closed(self):
        changed_sha, _grant_id, changed_path = await self._artifact(
            "a",
            grant_expires_at=None,
        )
        changed_path.write_bytes(b"unexpected replacement")
        linked_sha, _grant_id, linked_path = await self._artifact(
            "b",
            grant_expires_at=None,
        )
        outside_link = Path(self.temporary.name) / "linked-artifact"
        os.link(linked_path, outside_link)

        result = await SharedArtifactGarbageCollector(
            self.root,
            self.database,
        ).collect(now=1_000)

        self.assertEqual(result.deleted_artifacts, 0)
        self.assertTrue(changed_path.is_file())
        self.assertTrue(linked_path.is_file())
        self.assertTrue(outside_link.is_file())
        rows = await self.database.fetch_all(
            """
            SELECT artifact_sha256, publication_state
            FROM nzb_artifacts
            WHERE artifact_sha256 IN (:changed_sha, :linked_sha)
            ORDER BY artifact_sha256
            """,
            {"changed_sha": changed_sha, "linked_sha": linked_sha},
        )
        self.assertEqual(
            [(row["artifact_sha256"], row["publication_state"]) for row in rows],
            [(changed_sha, "tombstoned"), (linked_sha, "tombstoned")],
        )

    async def test_unlink_crash_leaves_a_durable_tombstone_for_retry(self):
        artifact_sha256, _grant_id, path = await self._artifact(
            "e",
            grant_expires_at=None,
        )
        collector = SharedArtifactGarbageCollector(self.root, self.database)
        unlink = collector._unlink_exact_regular

        def unlink_then_crash(artifact_path, expected_size):
            unlink(artifact_path, expected_size)
            raise RuntimeError("simulated process failure after unlink")

        with patch.object(collector, "_unlink_exact_regular", unlink_then_crash):
            with self.assertRaisesRegex(RuntimeError, "simulated process failure"):
                await collector.collect(now=1_000)

        self.assertFalse(path.exists())
        interrupted = await self.database.fetch_one(
            """
            SELECT publication_state
            FROM nzb_artifacts
            WHERE artifact_sha256 = :artifact_sha256
            """,
            {"artifact_sha256": artifact_sha256},
        )
        self.assertEqual(interrupted["publication_state"], "tombstoned")

        retried = await collector.collect(now=1_001)
        self.assertEqual(retried.deleted_artifacts, 1)
        self.assertIsNone(
            await self.database.fetch_one(
                """
                SELECT artifact_sha256
                FROM nzb_artifacts
                WHERE artifact_sha256 = :artifact_sha256
                """,
                {"artifact_sha256": artifact_sha256},
            )
        )

    async def test_final_claim_rechecks_the_unreferenced_grace_window(self):
        artifact_sha256, _grant_id, path = await self._artifact(
            "f",
            grant_expires_at=None,
        )
        await self.database.execute(
            """
            UPDATE nzb_artifacts
            SET last_used_at = 900
            WHERE artifact_sha256 = :artifact_sha256
            """,
            {"artifact_sha256": artifact_sha256},
        )

        removed = await SharedArtifactGarbageCollector(
            self.root,
            self.database,
        )._delete_one(artifact_sha256, 1_000)

        self.assertFalse(removed)
        self.assertTrue(path.is_file())
        row = await self.database.fetch_one(
            """
            SELECT publication_state
            FROM nzb_artifacts
            WHERE artifact_sha256 = :artifact_sha256
            """,
            {"artifact_sha256": artifact_sha256},
        )
        self.assertEqual(row["publication_state"], "published")

    async def test_batch_limit_is_closed_and_bounded(self):
        collector = SharedArtifactGarbageCollector(self.root, self.database)
        with self.assertRaisesRegex(ValueError, "GC limit"):
            await collector.collect(now=1_000, limit=0)
        with self.assertRaisesRegex(ValueError, "GC limit"):
            await collector.collect(now=1_000, limit=257)
