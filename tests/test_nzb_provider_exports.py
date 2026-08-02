import unittest
import uuid
from tempfile import TemporaryDirectory
from unittest.mock import patch

from databases import Database

from comet.core.db_router import ReplicaAwareDatabase
from comet.core.schema_migrations import (
    MigrationContext,
    _ensure_usenet_schema,
)
from comet.usenet import provider_exports
from comet.usenet.provider_exports import (
    NzbProviderExportError,
    NzbProviderExportRepository,
    export_base_url,
)


class NzbProviderExportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory()
        self.database = ReplicaAwareDatabase(
            Database(f"sqlite+aiosqlite:///{self.temporary.name}/exports.db")
        )
        await self.database.connect()
        context = MigrationContext(self.database, is_sqlite=True, is_postgres=False)
        await _ensure_usenet_schema(context)
        self.partition = b"p" * 32
        self.grant_id = str(uuid.uuid4())
        self.provider_id = str(uuid.uuid4())
        await self.database.execute(
            """
            INSERT INTO nzb_artifacts (
                artifact_sha256, byte_size, relative_path,
                source_manifest_identity, created_at, last_used_at
            ) VALUES (:sha, 5, :path, :nm1, 1, 1)
            """,
            {
                "sha": "a" * 64,
                "path": "nzb/" + "a" * 64 + ".nzb",
                "nm1": "nm1:" + "b" * 64,
            },
        )
        await self.database.execute(
            """
            INSERT INTO nzb_artifact_grants (
                grant_id, artifact_sha256, owner_configuration_partition, created_at, last_used_at, expires_at
            ) VALUES (:grant_id, :sha, :partition, 1, 1, 1000)
            """,
            {
                "grant_id": self.grant_id,
                "sha": "a" * 64,
                "partition": self.partition.hex(),
            },
        )

    async def asyncTearDown(self):
        await self.database.disconnect()
        self.temporary.cleanup()

    async def test_export_token_is_stable_and_resolves_only_an_active_grant(self):
        repository = NzbProviderExportRepository(self.database)
        fingerprint = "b" * 64

        first = await repository.get_or_create(
            owner_configuration_partition=self.partition,
            grant_id=self.grant_id,
            provider_configuration_id=self.provider_id,
            credential_fingerprint=fingerprint,
            now=10,
        )
        second = await repository.get_or_create(
            owner_configuration_partition=self.partition,
            grant_id=self.grant_id,
            provider_configuration_id=self.provider_id,
            credential_fingerprint=fingerprint,
            now=20,
        )
        self.assertRegex(first, r"^nx1\.[0-9a-f]{32}$")
        self.assertEqual(first, second)
        reused = await self.database.fetch_one(
            "SELECT request_count, last_used_at FROM nzb_provider_exports"
        )
        self.assertEqual(reused["request_count"], 0)
        self.assertEqual(reused["last_used_at"], 20)

        resolved = await repository.resolve(first.removeprefix("nx1."), now=30)
        self.assertEqual(resolved.grant_id, self.grant_id)
        self.assertEqual(resolved.owner_configuration_partition, self.partition)
        usage = await self.database.fetch_one(
            "SELECT request_count, last_used_at FROM nzb_provider_exports"
        )
        self.assertEqual(usage["request_count"], 1)
        self.assertEqual(usage["last_used_at"], 30)

        await self.database.execute("UPDATE nzb_provider_exports SET active = FALSE")
        with self.assertRaisesRegex(NzbProviderExportError, "unavailable"):
            await repository.resolve(first.removeprefix("nx1."), now=31)

    async def test_gc_revokes_and_deletes_only_idle_unreferenced_exports(self):
        repository = NzbProviderExportRepository(self.database)
        capability = await repository.get_or_create(
            owner_configuration_partition=self.partition,
            grant_id=self.grant_id,
            provider_configuration_id=self.provider_id,
            credential_fingerprint="b" * 64,
            now=10,
        )
        current_time = provider_exports._EXPORT_IDLE_TTL_SECONDS + 11

        self.assertEqual(
            await repository.garbage_collect(now=current_time),
            (1, 1),
        )
        self.assertIsNone(
            await self.database.fetch_one(
                "SELECT export_token FROM nzb_provider_exports"
            )
        )
        with self.assertRaisesRegex(NzbProviderExportError, "unavailable"):
            await repository.resolve(
                capability.removeprefix("nx1."),
                now=current_time,
            )
        with self.assertRaisesRegex(ValueError, "GC limit"):
            await repository.garbage_collect(now=current_time, limit=257)

    async def test_gc_waits_for_a_live_provider_preparation(self):
        repository = NzbProviderExportRepository(self.database)
        await repository.get_or_create(
            owner_configuration_partition=self.partition,
            grant_id=self.grant_id,
            provider_configuration_id=self.provider_id,
            credential_fingerprint="b" * 64,
            now=10,
        )
        candidate_id = str(uuid.uuid4())
        locator_id = str(uuid.uuid4())
        preparation_id = str(uuid.uuid4())
        await self.database.execute(
            """
            INSERT INTO rendered_release_candidates (
                candidate_id, owner_configuration_partition,
                external_candidate_id, media_id, transport, title,
                byte_size, parsed_json, created_at, updated_at,
                last_rendered_at
            ) VALUES (
                :candidate_id, :partition, 'candidate', 'tt1', 'usenet',
                'Example', 5, '{}', 1, 1, 1
            )
            """,
            {
                "candidate_id": candidate_id,
                "partition": self.partition.hex(),
            },
        )
        await self.database.execute(
            """
            INSERT INTO rendered_release_locators (
                locator_id, candidate_id, external_locator_id, locator_kind,
                locator_json, policy_json, created_at, updated_at,
                last_rendered_at
            ) VALUES (
                :locator_id, :candidate_id, 'locator', 'nzb_artifact',
                '{}', '{}', 1, 1, 1
            )
            """,
            {"locator_id": locator_id, "candidate_id": candidate_id},
        )
        await self.database.execute(
            """
            INSERT INTO provider_preparations (
                preparation_id, owner_configuration_partition,
                provider_configuration_id, credential_fingerprint,
                provider_kind, candidate_id, locator_id, artifact_grant_id,
                selection_json, mutation_idempotency_key,
                provider_payload_json, state, created_at, updated_at
            ) VALUES (
                :preparation_id, :partition, :provider_id, :fingerprint,
                'stremthru_newz', :candidate_id, :locator_id, :grant_id,
                '[0]', :mutation_key, '{}', 'submitted', 1, 1
            )
            """,
            {
                "preparation_id": preparation_id,
                "partition": self.partition.hex(),
                "provider_id": self.provider_id,
                "fingerprint": "b" * 64,
                "candidate_id": candidate_id,
                "locator_id": locator_id,
                "grant_id": self.grant_id,
                "mutation_key": "c" * 64,
            },
        )
        current_time = provider_exports._EXPORT_IDLE_TTL_SECONDS + 11

        self.assertEqual(
            await repository.garbage_collect(now=current_time),
            (0, 0),
        )
        await self.database.execute(
            """
            UPDATE provider_preparations
            SET state = 'terminal', terminal_at = :now, gc_after_at = :future
            WHERE preparation_id = :preparation_id
            """,
            {
                "now": current_time,
                "future": current_time + 1,
                "preparation_id": preparation_id,
            },
        )
        self.assertEqual(
            await repository.garbage_collect(now=current_time),
            (0, 0),
        )
        await self.database.execute(
            """
            UPDATE provider_preparations
            SET gc_after_at = :past
            WHERE preparation_id = :preparation_id
            """,
            {"past": current_time - 1, "preparation_id": preparation_id},
        )
        self.assertEqual(
            await repository.garbage_collect(now=current_time),
            (1, 1),
        )

    def test_operator_base_never_uses_request_state(self):
        with (
            patch(
                "comet.usenet.provider_exports.settings.USENET_EXPORT_BASE_URL",
                "http://bridge.internal:8080/root/",
            ),
            patch(
                "comet.usenet.provider_exports.settings.PUBLIC_BASE_URL",
                "https://public.example",
            ),
        ):
            self.assertEqual(export_base_url(), "http://bridge.internal:8080/root")
        with (
            patch(
                "comet.usenet.provider_exports.settings.USENET_EXPORT_BASE_URL", None
            ),
            patch("comet.usenet.provider_exports.settings.PUBLIC_BASE_URL", None),
        ):
            with self.assertRaisesRegex(NzbProviderExportError, "unavailable"):
                export_base_url()
        with patch(
            "comet.usenet.provider_exports.settings.USENET_EXPORT_BASE_URL",
            "https://public.example:99999",
        ):
            with self.assertRaisesRegex(NzbProviderExportError, "invalid"):
                export_base_url()
        with patch(
            "comet.usenet.provider_exports.settings.USENET_EXPORT_BASE_URL",
            "https://[invalid",
        ):
            with self.assertRaisesRegex(NzbProviderExportError, "invalid"):
                export_base_url()
