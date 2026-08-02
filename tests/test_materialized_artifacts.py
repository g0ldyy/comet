import hashlib
import os
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

from databases import Database

from comet.core.db_router import ReplicaAwareDatabase
from comet.core.schema_migrations import (
    MigrationContext,
    _ensure_managed_table,
    _ensure_usenet_schema,
)
from comet.core.schema_specs import SCRAPE_LOCKS_TABLE_SPEC
from comet.usenet.artifact_gc import SharedArtifactGarbageCollector
from comet.usenet.materialized_artifacts import (
    MaterializedArtifact,
    MaterializedArtifactRepository,
)


class MaterializedArtifactRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name) / "shared"
        (self.root / "materialized").mkdir(parents=True)
        self.database = ReplicaAwareDatabase(
            Database(f"sqlite+aiosqlite:///{self.temporary.name}/materialized.db")
        )
        await self.database.connect()
        await self.database.execute("PRAGMA foreign_keys=ON")
        context = MigrationContext(
            self.database,
            is_sqlite=True,
            is_postgres=False,
        )
        await _ensure_usenet_schema(context)
        await _ensure_managed_table(context, SCRAPE_LOCKS_TABLE_SPEC)
        self.partition = b"a" * 32
        self.preparation_id = str(uuid.uuid4())
        candidate_id = str(uuid.uuid4())
        await self.database.execute(
            """
            INSERT INTO rendered_release_candidates (
                candidate_id, owner_configuration_partition,
                external_candidate_id, media_id, transport, title,
                byte_size, parsed_json, created_at, updated_at,
                last_rendered_at
            ) VALUES (
                :candidate_id, :partition, 'candidate', 'tt123',
                'usenet', 'Release', 3, '{}', 1, 1, 1
            )
            """,
            {
                "candidate_id": candidate_id,
                "partition": self.partition.hex(),
            },
        )
        await self.database.execute(
            """
            INSERT INTO asset_preparations (
                preparation_id, owner_configuration_partition,
                preparation_intent_key,
                candidate_id, provider_configuration_id, provider_kind,
                locator_ids_json, selection_intent_json,
                selection_intent_version, parser_version,
                selector_version, archive_plan_version,
                client, state, target_kind,
                reconstruction_blueprint_json,
                created_at, last_used_at, idle_expires_at,
                absolute_expires_at
            ) VALUES (
                :preparation_id, :partition, :preparation_intent_key,
                :candidate_id, :provider_id, 'comet_native_usenet',
                :locator_ids_json, '[0]', 1, 1, 1, 1,
                'stremio', 'pending', NULL, NULL, 1, 1, 1000, 1000
            )
            """,
            {
                "preparation_id": self.preparation_id,
                "partition": self.partition.hex(),
                "preparation_intent_key": "b" * 64,
                "candidate_id": candidate_id,
                "provider_id": str(uuid.uuid4()),
                "locator_ids_json": f'["{uuid.uuid4()}"]',
            },
        )
        body = b"abc"
        self.identity = hashlib.sha256(body).hexdigest()
        self.path = self.root / "materialized" / f"{self.identity}.bin"
        self.path.write_bytes(body)
        self.path.chmod(0o400)
        self.artifact = MaterializedArtifact(
            self.identity,
            len(body),
            "c" * 64,
            "d" * 64,
        )

    async def asyncTearDown(self):
        await self.database.disconnect()
        self.temporary.cleanup()

    async def _register(self):
        await MaterializedArtifactRepository(
            self.root,
            self.database,
        ).register_for_preparation(
            self.preparation_id,
            owner_configuration_partition=self.partition,
            source_nm1="nm1:" + "f" * 64,
            artifacts=(self.artifact,),
            now=10,
        )

    def test_preparation_ledger_has_no_sixty_four_artifact_shape_assumption(self):
        self.assertEqual(
            MaterializedArtifactRepository._canonical_artifacts((self.artifact,) * 65),
            (self.artifact,),
        )

    async def test_registration_accepts_only_the_current_manifest_identity(self):
        repository = MaterializedArtifactRepository(self.root, self.database)
        for source_identity in (
            "nh1:" + "f" * 40,
            "nm1:" + "F" * 64,
            "nm1:" + "f" * 63,
        ):
            with self.subTest(source_identity=source_identity):
                with self.assertRaisesRegex(ValueError, "source identity"):
                    await repository.register_for_preparation(
                        self.preparation_id,
                        owner_configuration_partition=self.partition,
                        source_nm1=source_identity,
                        artifacts=(self.artifact,),
                        now=10,
                    )

    async def test_second_replica_reopens_the_registered_shared_object(self):
        await self._register()

        readers = await MaterializedArtifactRepository(
            self.root,
            self.database,
        ).acquire_for_preparation(
            self.preparation_id,
            owner_configuration_partition=self.partition,
            now=20,
        )

        self.assertEqual(
            tuple(reader.artifact_sha256 for reader in readers),
            (self.identity,),
        )
        row = await self.database.fetch_one(
            """
            SELECT artifacts.storage_kind, artifacts.relative_path,
                   artifacts.source_manifest_identity,
                   artifacts.selected_asset_id,
                   artifacts.strong_asset_revision,
                   artifacts.logical_length, artifacts.refcount
            FROM nzb_artifacts AS artifacts
            JOIN asset_preparation_artifacts AS links
              ON links.artifact_sha256 = artifacts.artifact_sha256
            WHERE links.preparation_id = :preparation_id
            """,
            {"preparation_id": self.preparation_id},
        )
        self.assertEqual(
            (
                row["storage_kind"],
                row["relative_path"],
                row["source_manifest_identity"],
                row["selected_asset_id"],
                row["strong_asset_revision"],
                row["logical_length"],
                row["refcount"],
            ),
            (
                "materialized_asset",
                f"materialized/{self.identity}.bin",
                "nm1:" + "f" * 64,
                "c" * 64,
                "d" * 64,
                self.artifact.byte_size,
                1,
            ),
        )
        await readers[0].close()
        self.assertIsNone(
            await self.database.fetch_one("SELECT lease_id FROM artifact_reader_leases")
        )

    async def test_reader_lease_defers_gc_after_preparation_expiry(self):
        await self._register()
        reader = (
            await MaterializedArtifactRepository(
                self.root,
                self.database,
            ).acquire_for_preparation(
                self.preparation_id,
                owner_configuration_partition=self.partition,
                now=900,
            )
        )[0]
        # The production SQLite connection hook enables FK cascades. This
        # isolated databases.Database fixture models that cascade explicitly.
        await self.database.execute(
            """
            DELETE FROM asset_preparation_artifacts
            WHERE preparation_id = :preparation_id
            """,
            {"preparation_id": self.preparation_id},
        )
        await self.database.execute(
            """
            DELETE FROM asset_preparations
            WHERE preparation_id = :preparation_id
            """,
            {"preparation_id": self.preparation_id},
        )
        await self.database.execute(
            """
            UPDATE nzb_artifacts
            SET last_used_at = 1
            WHERE artifact_sha256 = :artifact_sha256
            """,
            {"artifact_sha256": self.identity},
        )
        self.assertIsNone(
            await self.database.fetch_one(
                """
                SELECT preparation_id
                FROM asset_preparation_artifacts
                WHERE preparation_id = :preparation_id
                """,
                {"preparation_id": self.preparation_id},
            )
        )
        collector = SharedArtifactGarbageCollector(self.root, self.database)

        retained = await collector.collect(now=950)
        self.assertEqual(retained.deleted_artifacts, 0)
        self.assertTrue(self.path.is_file())

        await reader.close()
        removed = await collector.collect(now=951)
        self.assertEqual(removed.deleted_artifacts, 1)
        self.assertFalse(self.path.exists())

    async def test_publication_lease_serializes_crash_orphan_reconciliation(self):
        orphan_body = b"crashed publication"
        orphan_identity = hashlib.sha256(orphan_body).hexdigest()
        orphan = self.root / "materialized" / f"{orphan_identity}.bin"
        orphan.write_bytes(orphan_body)
        orphan.chmod(0o400)
        os.utime(orphan, (1, 1))
        repository = MaterializedArtifactRepository(
            self.root,
            self.database,
        )
        publication = await repository.acquire_publication_lease(
            self.preparation_id,
            owner_configuration_partition=self.partition,
            now=100,
        )
        collector = SharedArtifactGarbageCollector(self.root, self.database)

        retained = await collector.collect(now=350)
        self.assertEqual(retained.reconciled_orphans, 0)
        self.assertTrue(orphan.is_file())

        await publication.close()
        removed = await collector.collect(now=351)
        self.assertEqual(removed.reconciled_orphans, 1)
        self.assertFalse(orphan.exists())

    async def test_publication_lease_blocks_resurrection_race_with_registered_gc(self):
        await self._register()
        await self.database.execute(
            """
            DELETE FROM asset_preparation_artifacts
            WHERE preparation_id = :preparation_id
            """,
            {"preparation_id": self.preparation_id},
        )
        await self.database.execute(
            """
            UPDATE nzb_artifacts
            SET last_used_at = 1
            WHERE artifact_sha256 = :artifact_sha256
            """,
            {"artifact_sha256": self.identity},
        )
        publication = await MaterializedArtifactRepository(
            self.root,
            self.database,
        ).acquire_publication_lease(
            self.preparation_id,
            owner_configuration_partition=self.partition,
            now=100,
        )
        collector = SharedArtifactGarbageCollector(self.root, self.database)

        retained = await collector.collect(now=350)
        self.assertEqual(retained.deleted_artifacts, 0)
        self.assertTrue(self.path.is_file())

        await publication.close()
        removed = await collector.collect(now=351)
        self.assertEqual(removed.deleted_artifacts, 1)
        self.assertFalse(self.path.exists())

    async def test_final_blueprint_releases_intermediate_materializations(self):
        intermediate_body = b"intermediate"
        intermediate_identity = hashlib.sha256(intermediate_body).hexdigest()
        intermediate_path = self.root / "materialized" / f"{intermediate_identity}.bin"
        intermediate_path.write_bytes(intermediate_body)
        intermediate_path.chmod(0o400)
        intermediate = MaterializedArtifact(
            intermediate_identity,
            len(intermediate_body),
            "1" * 64,
            "2" * 64,
        )
        repository = MaterializedArtifactRepository(
            self.root,
            self.database,
        )
        await repository.register_for_preparation(
            self.preparation_id,
            owner_configuration_partition=self.partition,
            source_nm1="nm1:" + "f" * 64,
            artifacts=(self.artifact, intermediate),
            now=10,
        )

        await repository.retain_for_preparation(
            self.preparation_id,
            owner_configuration_partition=self.partition,
            artifact_sha256s=(self.identity,),
            now=20,
        )

        links = await self.database.fetch_all(
            """
            SELECT artifact_sha256
            FROM asset_preparation_artifacts
            ORDER BY artifact_sha256
            """
        )
        released = await self.database.fetch_one(
            """
            SELECT refcount
            FROM nzb_artifacts
            WHERE artifact_sha256 = :artifact_sha256
            """,
            {"artifact_sha256": intermediate_identity},
        )
        self.assertEqual(
            [row["artifact_sha256"] for row in links],
            [self.identity],
        )
        self.assertEqual(released["refcount"], 0)

        removed = await SharedArtifactGarbageCollector(
            self.root,
            self.database,
        ).collect(now=400)
        self.assertEqual(removed.deleted_artifacts, 1)
        self.assertTrue(self.path.is_file())
        self.assertFalse(intermediate_path.exists())

    async def test_orphan_reconciliation_fails_closed_on_digest_mismatch(self):
        mismatched = self.root / "materialized" / f"{'1' * 64}.bin"
        mismatched.write_bytes(b"not the named digest")
        mismatched.chmod(0o400)
        os.utime(mismatched, (1, 1))
        outside = Path(self.temporary.name) / "outside"
        outside.write_bytes(b"outside")
        symlink = self.root / "materialized" / f"{'2' * 64}.bin"
        symlink.symlink_to(outside)

        result = await SharedArtifactGarbageCollector(
            self.root,
            self.database,
        ).collect(now=1_000)

        self.assertEqual(result.reconciled_orphans, 0)
        self.assertTrue(mismatched.is_file())
        self.assertTrue(symlink.is_symlink())
        self.assertTrue(outside.is_file())
