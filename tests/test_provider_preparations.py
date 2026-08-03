import unittest
import uuid
from tempfile import TemporaryDirectory

from databases import Database

from comet.core.db_router import ReplicaAwareDatabase
from comet.core.schema_migrations import (
    MigrationContext,
    _ensure_usenet_schema,
)
from comet.core.sources import MAX_SIGNED_BIGINT
from comet.playback.preparations import PlaybackPreparationRepository
from comet.playback.provider_preparations import (
    ProviderPreparationRepository,
    provider_selection_json,
)
from comet.playback.tokens import PlaybackIntent


class ProviderPreparationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory()
        self.database = ReplicaAwareDatabase(
            Database(f"sqlite+aiosqlite:///{self.temporary.name}/preparations.db")
        )
        await self.database.connect()
        context = MigrationContext(self.database, is_sqlite=True, is_postgres=False)
        await _ensure_usenet_schema(context)
        self.partition = b"p" * 32
        self.provider_id = str(uuid.uuid4())
        self.generic_candidate_id = str(uuid.uuid4())
        self.candidate_id = str(uuid.uuid4())
        self.locator_id = str(uuid.uuid4())
        self.grant_id = str(uuid.uuid4())
        await self.database.execute(
            """
            INSERT INTO nzb_artifacts (
                artifact_sha256, byte_size, relative_path,
                source_manifest_identity, created_at, last_used_at
            ) VALUES (:sha, 1, :path, :nm1, 1, 1)
            """,
            {
                "sha": "a" * 64,
                "path": "nzb/" + "a" * 64 + ".nzb",
                "nm1": "nm1:" + "b" * 64,
            },
        )
        await self.database.execute(
            """
            INSERT INTO nzb_contents (
                manifest_identity, parser_version,
                posting_set_identity, artifact_sha256, manifest_json,
                created_at, last_used_at
            ) VALUES (
                :nm1, 2, :nh1, :sha,
                '{"metadata":{},"files":[]}', 1, 1
            )
            """,
            {
                "nm1": "nm1:" + "b" * 64,
                "nh1": "nh1:" + "c" * 40,
                "sha": "a" * 64,
            },
        )
        await self.database.execute(
            """
            INSERT INTO nzb_artifact_grants (
                grant_id, artifact_sha256, owner_configuration_partition, created_at, last_used_at, expires_at
            ) VALUES (:grant, :sha, :partition, 1, 1, 1000)
            """,
            {
                "grant": self.grant_id,
                "sha": "a" * 64,
                "partition": self.partition.hex(),
            },
        )
        await self.database.execute(
            """
            INSERT INTO release_candidates (
                candidate_id, visibility_partition, media_id, transport,
                release_key, scope, season_norm, episode_norm, title,
                parsed_json, attributes_json, created_at_ms,
                updated_at_ms, last_seen_at_ms
            ) VALUES (
                :candidate_id, :partition, 'tt1', 'usenet', 'release',
                'movie', -1, -1, 'Example', '{}',
                '{"identities":[],"source":"test","transport_stats":{}}',
                1, 1, 1
            )
            """,
            {
                "candidate_id": self.generic_candidate_id,
                "partition": self.partition.hex(),
            },
        )
        await self.database.execute(
            """
            INSERT INTO rendered_release_candidates (
                candidate_id, owner_configuration_partition, external_candidate_id,
                media_id, transport, title, parsed_json, created_at, updated_at, last_rendered_at
            ) VALUES (
                :candidate, :partition, :external_candidate_id,
                'tt1', 'usenet', 'Example', '{}', 1, 1, 1
            )
            """,
            {
                "candidate": self.candidate_id,
                "partition": self.partition.hex(),
                "external_candidate_id": self.generic_candidate_id,
            },
        )
        await self.database.execute(
            """
            INSERT INTO rendered_release_locators (
                locator_id, candidate_id, external_locator_id, locator_kind, locator_json,
                policy_json, created_at, updated_at, last_rendered_at
            ) VALUES (:locator, :candidate, 'external', 'nzb_artifact', '{}', '{}', 1, 1, 1)
            """,
            {"locator": self.locator_id, "candidate": self.candidate_id},
        )

    async def asyncTearDown(self):
        await self.database.disconnect()
        self.temporary.cleanup()

    async def test_selected_file_sizes_use_the_signed_bigint_domain(self):
        repository = ProviderPreparationRepository(self.database)
        common = {
            "preparation_id": str(uuid.uuid4()),
            "owner_configuration_partition": self.partition,
            "remote_id": "remote",
            "remote_hash": "a" * 64,
            "file_index": 0,
            "file_size": MAX_SIGNED_BIGINT + 1,
            "locked_link": "locked",
        }

        with self.assertRaisesRegex(ValueError, "invalid provider file"):
            await repository.record_selected_file(**common)
        with self.assertRaisesRegex(ValueError, "invalid provider file"):
            await repository.record_selected_file(
                **{
                    **common,
                    "file_index": MAX_SIGNED_BIGINT + 1,
                    "file_size": 1,
                }
            )

    async def test_mutation_key_joins_retries_and_seals_remote_submission(self):
        repository = ProviderPreparationRepository(self.database)
        arguments = {
            "owner_configuration_partition": self.partition,
            "provider_configuration_id": self.provider_id,
            "credential_fingerprint": "b" * 64,
            "candidate_id": self.candidate_id,
            "locator_id": self.locator_id,
            "artifact_grant_id": self.grant_id,
            "selection_json": provider_selection_json((0,)),
            "mutation_idempotency_key": "c" * 64,
        }
        self.assertIsNone(await repository.get_existing(**arguments))
        first, created = await repository.get_or_create(**arguments, now=10)
        existing = await repository.get_existing(**arguments)
        repeated, retry_created = await repository.get_or_create(**arguments, now=11)

        self.assertTrue(created)
        self.assertEqual(existing.preparation_id, first.preparation_id)
        self.assertFalse(retry_created)
        self.assertEqual(repeated.preparation_id, first.preparation_id)
        self.assertEqual(repeated.state, "mutation_pending")
        with self.assertRaisesRegex(ValueError, "binding conflicts"):
            await repository.get_existing(
                **{**arguments, "selection_json": provider_selection_json((1,))}
            )

        await repository.record_submission(
            first.preparation_id,
            owner_configuration_partition=self.partition,
            remote_id="remote-1",
            remote_hash="hash-1",
            status="queued",
            ownership="created",
            now=12,
        )
        sealed, created_again = await repository.get_or_create(**arguments, now=13)
        self.assertFalse(created_again)
        self.assertEqual(sealed.state, "submitted")
        self.assertEqual(
            sealed.payload,
            {
                "remote_id": "remote-1",
                "remote_hash": "hash-1",
                "status": "queued",
                "ownership": "created",
                "missing_count": 0,
            },
        )

        observed = await repository.record_poll(
            first.preparation_id, owner_configuration_partition=self.partition, now=14
        )
        row = await self.database.fetch_one(
            "SELECT last_polled_at FROM provider_preparations"
        )
        self.assertEqual(row["last_polled_at"], 14)
        self.assertEqual(observed, sealed)

    async def test_submission_status_is_an_opaque_provider_value(self):
        repository = ProviderPreparationRepository(self.database)
        ledger, _created = await repository.get_or_create(
            owner_configuration_partition=self.partition,
            provider_configuration_id=self.provider_id,
            credential_fingerprint="b" * 64,
            candidate_id=self.candidate_id,
            locator_id=self.locator_id,
            artifact_grant_id=self.grant_id,
            selection_json=provider_selection_json((0,)),
            mutation_idempotency_key="d" * 64,
            provider_kind="torbox_usenet",
            now=10,
        )

        await repository.record_submission(
            ledger.preparation_id,
            owner_configuration_partition=self.partition,
            remote_id="17",
            remote_hash="a" * 64,
            status="waiting_for_new_pipeline",
            ownership="created",
            now=11,
        )
        submitted = await repository.get_existing(
            owner_configuration_partition=self.partition,
            provider_configuration_id=self.provider_id,
            credential_fingerprint="b" * 64,
            candidate_id=self.candidate_id,
            locator_id=self.locator_id,
            artifact_grant_id=self.grant_id,
            selection_json=provider_selection_json((0,)),
            mutation_idempotency_key="d" * 64,
            provider_kind="torbox_usenet",
        )

        self.assertEqual(submitted.state, "submitted")
        self.assertEqual(submitted.payload["status"], "waiting_for_new_pipeline")

    async def test_unreconcilable_mutation_is_a_permanent_tombstone(self):
        repository = ProviderPreparationRepository(self.database)
        ledger, _created = await repository.get_or_create(
            owner_configuration_partition=self.partition,
            provider_configuration_id=self.provider_id,
            credential_fingerprint="b" * 64,
            candidate_id=self.candidate_id,
            locator_id=self.locator_id,
            artifact_grant_id=self.grant_id,
            selection_json=provider_selection_json((0,)),
            mutation_idempotency_key="e" * 64,
            provider_kind="stremthru_newz",
            now=10,
        )

        await repository.record_ambiguous_submission(
            ledger.preparation_id,
            owner_configuration_partition=self.partition,
            provider_kind="stremthru_newz",
            now=11,
        )
        await repository.record_ambiguous_submission(
            ledger.preparation_id,
            owner_configuration_partition=self.partition,
            provider_kind="stremthru_newz",
            now=12,
        )
        row = await self.database.fetch_one(
            """
            SELECT state, provider_payload_json, terminal_at, gc_after_at
            FROM provider_preparations
            WHERE preparation_id = :preparation_id
            """,
            {"preparation_id": ledger.preparation_id},
        )

        self.assertEqual(
            (
                row["state"],
                row["provider_payload_json"],
                row["terminal_at"],
                row["gc_after_at"],
            ),
            (
                "terminal",
                '{"status":"ambiguous_submission"}',
                11,
                None,
            ),
        )
        self.assertEqual(await repository.garbage_collect(now=10**9), 0)

    async def test_rejected_stremthru_mutations_restore_retryable_local_state(self):
        repository = ProviderPreparationRepository(self.database)
        arguments = {
            "owner_configuration_partition": self.partition,
            "provider_configuration_id": self.provider_id,
            "credential_fingerprint": "b" * 64,
            "candidate_id": self.candidate_id,
            "locator_id": self.locator_id,
            "artifact_grant_id": self.grant_id,
            "selection_json": provider_selection_json((0,)),
            "mutation_idempotency_key": "8" * 64,
            "provider_kind": "stremthru_newz",
        }
        initial, _created = await repository.get_or_create(**arguments, now=10)

        await repository.discard_rejected_stremthru_submission(
            initial.preparation_id,
            owner_configuration_partition=self.partition,
        )
        recreated, created = await repository.get_or_create(**arguments, now=11)
        await repository.record_submission(
            recreated.preparation_id,
            owner_configuration_partition=self.partition,
            remote_id="remote-1",
            remote_hash="hash-1",
            status="queued",
            ownership="created",
            now=12,
        )
        await repository.record_stremthru_missing(
            recreated.preparation_id,
            owner_configuration_partition=self.partition,
            now=13,
        )
        self.assertTrue(
            await repository.begin_stremthru_resubmission(
                recreated.preparation_id,
                owner_configuration_partition=self.partition,
                now=14,
            )
        )
        await repository.restore_rejected_stremthru_resubmission(
            recreated.preparation_id,
            owner_configuration_partition=self.partition,
            now=15,
        )
        restored, _created = await repository.get_or_create(**arguments, now=16)

        self.assertTrue(created)
        self.assertEqual(
            restored.payload,
            {
                "status": "remote_missing",
                "ownership": "created",
                "missing_count": 1,
            },
        )
        self.assertTrue(
            await repository.begin_stremthru_resubmission(
                recreated.preparation_id,
                owner_configuration_partition=self.partition,
                now=17,
            )
        )

    async def test_torbox_intent_is_sealed_without_an_artifact_grant(self):
        repository = ProviderPreparationRepository(self.database)
        arguments = {
            "owner_configuration_partition": self.partition,
            "provider_configuration_id": self.provider_id,
            "credential_fingerprint": "b" * 64,
            "candidate_id": self.candidate_id,
            "locator_id": self.locator_id,
            "artifact_grant_id": None,
            "selection_json": provider_selection_json((0,)),
            "mutation_idempotency_key": "9" * 64,
            "provider_kind": "torbox_usenet",
        }

        ledger, created = await repository.get_or_create(**arguments, now=10)
        await repository.record_submission(
            ledger.preparation_id,
            owner_configuration_partition=self.partition,
            remote_id="17",
            remote_hash="a" * 32,
            status="completed",
            ownership="created",
            now=11,
        )
        repeated, created_again = await repository.get_or_create(
            **arguments,
            now=12,
        )
        row = await self.database.fetch_one(
            """
            SELECT provider_kind, artifact_grant_id
            FROM provider_preparations
            WHERE preparation_id = :preparation_id
            """,
            {"preparation_id": ledger.preparation_id},
        )

        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(repeated.payload["remote_id"], "17")
        self.assertEqual(
            (row["provider_kind"], row["artifact_grant_id"]),
            ("torbox_usenet", None),
        )

    async def test_nzbdav_intent_seals_the_exact_artifact_grant(self):
        repository = ProviderPreparationRepository(self.database)
        ledger, created = await repository.get_or_create(
            owner_configuration_partition=self.partition,
            provider_configuration_id=self.provider_id,
            credential_fingerprint="b" * 64,
            candidate_id=self.candidate_id,
            locator_id=self.locator_id,
            artifact_grant_id=self.grant_id,
            selection_json=provider_selection_json((0,)),
            mutation_idempotency_key="8" * 64,
            provider_kind="nzbdav",
            now=10,
        )
        await repository.record_submission(
            ledger.preparation_id,
            owner_configuration_partition=self.partition,
            remote_id=str(uuid.uuid4()),
            remote_hash="a" * 64,
            status="queued",
            ownership="created",
            now=11,
        )
        row = await self.database.fetch_one(
            """
            SELECT provider_kind, artifact_grant_id, cleanup_state
            FROM provider_preparations
            WHERE preparation_id = :preparation_id
            """,
            {"preparation_id": ledger.preparation_id},
        )

        self.assertTrue(created)
        self.assertEqual(
            (
                row["provider_kind"],
                row["artifact_grant_id"],
                row["cleanup_state"],
            ),
            ("nzbdav", self.grant_id, "not_required"),
        )

    async def test_nzbdav_resubmission_is_sealed_exactly_once(self):
        repository = ProviderPreparationRepository(self.database)
        ledger, _ = await repository.get_or_create(
            owner_configuration_partition=self.partition,
            provider_configuration_id=self.provider_id,
            credential_fingerprint="b" * 64,
            candidate_id=self.candidate_id,
            locator_id=self.locator_id,
            artifact_grant_id=self.grant_id,
            selection_json=provider_selection_json((0,)),
            mutation_idempotency_key="7" * 64,
            provider_kind="nzbdav",
            now=10,
        )

        first = await repository.begin_nzbdav_resubmission(
            ledger.preparation_id,
            owner_configuration_partition=self.partition,
            now=131,
        )
        second = await repository.begin_nzbdav_resubmission(
            ledger.preparation_id,
            owner_configuration_partition=self.partition,
            now=132,
        )
        row = await self.database.fetch_one(
            """
            SELECT provider_payload_json
            FROM provider_preparations
            WHERE preparation_id = :preparation_id
            """,
            {"preparation_id": ledger.preparation_id},
        )

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(
            row["provider_payload_json"],
            '{"status":"resubmit_mutation_pending"}',
        )

    async def test_rejected_nzbdav_mutations_restore_reconcilable_state(self):
        repository = ProviderPreparationRepository(self.database)
        arguments = {
            "owner_configuration_partition": self.partition,
            "provider_configuration_id": self.provider_id,
            "credential_fingerprint": "b" * 64,
            "candidate_id": self.candidate_id,
            "locator_id": self.locator_id,
            "artifact_grant_id": self.grant_id,
            "selection_json": provider_selection_json((0,)),
            "mutation_idempotency_key": "f" * 64,
            "provider_kind": "nzbdav",
        }
        initial, _created = await repository.get_or_create(**arguments, now=10)

        await repository.discard_rejected_nzbdav_submission(
            initial.preparation_id,
            owner_configuration_partition=self.partition,
        )
        recreated, created = await repository.get_or_create(**arguments, now=11)
        self.assertTrue(
            await repository.begin_nzbdav_resubmission(
                recreated.preparation_id,
                owner_configuration_partition=self.partition,
                now=12,
            )
        )
        await repository.restore_rejected_nzbdav_resubmission(
            recreated.preparation_id,
            owner_configuration_partition=self.partition,
            now=13,
        )
        restored, _created = await repository.get_or_create(**arguments, now=14)

        self.assertTrue(created)
        self.assertEqual(restored.payload, {})
        self.assertTrue(
            await repository.begin_nzbdav_resubmission(
                recreated.preparation_id,
                owner_configuration_partition=self.partition,
                now=15,
            )
        )

    async def test_altmount_retry_cadence_and_selected_path_are_durable(self):
        repository = ProviderPreparationRepository(self.database)
        ledger, _ = await repository.get_or_create(
            owner_configuration_partition=self.partition,
            provider_configuration_id=self.provider_id,
            credential_fingerprint="b" * 64,
            candidate_id=self.candidate_id,
            locator_id=self.locator_id,
            artifact_grant_id=self.grant_id,
            selection_json=provider_selection_json((1, 1, 2)),
            mutation_idempotency_key="6" * 64,
            provider_kind="altmount",
            now=10,
        )

        too_early = await repository.claim_altmount_retry(
            ledger.preparation_id,
            owner_configuration_partition=self.partition,
            deadline_seconds=300,
            now=29,
        )
        claimed = await repository.claim_altmount_retry(
            ledger.preparation_id,
            owner_configuration_partition=self.partition,
            deadline_seconds=300,
            now=30,
        )
        duplicate = await repository.claim_altmount_retry(
            ledger.preparation_id,
            owner_configuration_partition=self.partition,
            deadline_seconds=300,
            now=30,
        )
        final = await repository.claim_altmount_retry(
            ledger.preparation_id,
            owner_configuration_partition=self.partition,
            deadline_seconds=300,
            now=310,
        )
        after_final = await repository.claim_altmount_retry(
            ledger.preparation_id,
            owner_configuration_partition=self.partition,
            deadline_seconds=300,
            now=330,
        )
        await repository.record_altmount_selection(
            ledger.preparation_id,
            owner_configuration_partition=self.partition,
            virtual_path="folder/Example.S01E02.mkv",
            now=331,
        )
        sealed, created = await repository.get_or_create(
            owner_configuration_partition=self.partition,
            provider_configuration_id=self.provider_id,
            credential_fingerprint="b" * 64,
            candidate_id=self.candidate_id,
            locator_id=self.locator_id,
            artifact_grant_id=self.grant_id,
            selection_json=provider_selection_json((1, 1, 2)),
            mutation_idempotency_key="6" * 64,
            provider_kind="altmount",
            now=332,
        )

        self.assertIsNone(too_early)
        self.assertEqual(claimed, "retry")
        self.assertIsNone(duplicate)
        self.assertEqual(final, "final")
        self.assertIsNone(after_final)
        self.assertFalse(created)
        self.assertEqual(sealed.state, "terminal")
        self.assertEqual(
            sealed.payload["virtual_path"],
            "folder/Example.S01E02.mkv",
        )
        self.assertNotIn("download_key", sealed.payload)

    async def test_altmount_retry_exposes_corrupt_durable_state(self):
        repository = ProviderPreparationRepository(self.database)
        ledger, _created = await repository.get_or_create(
            owner_configuration_partition=self.partition,
            provider_configuration_id=self.provider_id,
            credential_fingerprint="b" * 64,
            candidate_id=self.candidate_id,
            locator_id=self.locator_id,
            artifact_grant_id=self.grant_id,
            selection_json=provider_selection_json((0,)),
            mutation_idempotency_key="5" * 64,
            provider_kind="altmount",
            now=10,
        )
        await self.database.execute(
            """
            UPDATE provider_preparations
            SET provider_payload_json = '{"retry_count":"broken"}'
            WHERE preparation_id = :preparation_id
            """,
            {"preparation_id": ledger.preparation_id},
        )

        with self.assertRaisesRegex(ValueError, "retry state is corrupt"):
            await repository.claim_altmount_retry(
                ledger.preparation_id,
                owner_configuration_partition=self.partition,
                deadline_seconds=300,
                now=30,
            )

    async def test_terminal_gc_waits_for_the_last_playback_capability(self):
        provider_repository = ProviderPreparationRepository(self.database)
        ledger, _created = await provider_repository.get_or_create(
            owner_configuration_partition=self.partition,
            provider_configuration_id=self.provider_id,
            credential_fingerprint="b" * 64,
            candidate_id=self.candidate_id,
            locator_id=self.locator_id,
            artifact_grant_id=self.grant_id,
            selection_json=provider_selection_json((0,)),
            mutation_idempotency_key="d" * 64,
            now=10,
        )
        await provider_repository.record_submission(
            ledger.preparation_id,
            owner_configuration_partition=self.partition,
            remote_id="remote-1",
            remote_hash="hash-1",
            status="fatal_on_new_pipeline",
            ownership="created",
            now=12,
        )
        await provider_repository.record_terminal_status(
            ledger.preparation_id,
            owner_configuration_partition=self.partition,
            status="fatal_on_new_pipeline",
            now=12,
        )
        terminal = await self.database.fetch_one(
            """
            SELECT state, terminal_at, gc_after_at
            FROM provider_preparations
            WHERE preparation_id = :preparation_id
            """,
            {"preparation_id": ledger.preparation_id},
        )
        self.assertEqual(
            (
                terminal["state"],
                terminal["terminal_at"],
                terminal["gc_after_at"],
            ),
            ("terminal", 12, 12 + 6 * 60 * 60),
        )
        playback_repository = PlaybackPreparationRepository(self.database)
        playback = await playback_repository.get_or_create(
            PlaybackIntent(
                self.candidate_id,
                self.provider_id,
                (self.locator_id,),
                (0,),
                "stremio",
            ),
            provider_kind="stremthru_newz",
            owner_configuration_partition=self.partition,
            preparation_intent_key="a" * 64,
            ttl=6 * 60 * 60,
            now=8_400,
        )
        await playback_repository.record_pending_target(
            playback.preparation_id,
            owner_configuration_partition=self.partition,
            provider_account_partition=b"a" * 32,
            target_kind="relay",
            target_ref={
                "provider_preparation_id": ledger.preparation_id,
                "remote_id": "remote-1",
            },
            now=8_401,
        )
        referenced = await self.database.fetch_one(
            """
            SELECT refcount
            FROM provider_preparations
            WHERE preparation_id = :preparation_id
            """,
            {"preparation_id": ledger.preparation_id},
        )

        retained = await provider_repository.garbage_collect(now=22_000)
        expired = await PlaybackPreparationRepository(self.database).garbage_collect(
            now=30_001
        )
        released = await self.database.fetch_one(
            """
            SELECT refcount
            FROM provider_preparations
            WHERE preparation_id = :preparation_id
            """,
            {"preparation_id": ledger.preparation_id},
        )
        removed = await provider_repository.garbage_collect(now=30_001)

        self.assertEqual(referenced["refcount"], 1)
        self.assertEqual(released["refcount"], 0)
        self.assertEqual((retained, expired, removed), (0, 1, 1))
        self.assertIsNone(
            await self.database.fetch_one(
                """
                SELECT preparation_id
                FROM provider_preparations
                WHERE preparation_id = :preparation_id
                """,
                {"preparation_id": ledger.preparation_id},
            )
        )

    async def test_refcounts_are_exact_idempotent_and_account_bound(self):
        provider_repository = ProviderPreparationRepository(self.database)
        ledger, _ = await provider_repository.get_or_create(
            owner_configuration_partition=self.partition,
            provider_configuration_id=self.provider_id,
            credential_fingerprint="b" * 64,
            candidate_id=self.candidate_id,
            locator_id=self.locator_id,
            artifact_grant_id=self.grant_id,
            selection_json=provider_selection_json((0,)),
            mutation_idempotency_key="7" * 64,
            now=10,
        )
        other_provider_id = str(uuid.uuid4())
        other_ledger, _ = await provider_repository.get_or_create(
            owner_configuration_partition=self.partition,
            provider_configuration_id=other_provider_id,
            credential_fingerprint="c" * 64,
            candidate_id=self.candidate_id,
            locator_id=self.locator_id,
            artifact_grant_id=self.grant_id,
            selection_json=provider_selection_json((0,)),
            mutation_idempotency_key="8" * 64,
            now=10,
        )
        playback_repository = PlaybackPreparationRepository(self.database)
        playbacks = []
        for client in ("stremio", "kodi"):
            playback = await playback_repository.get_or_create(
                PlaybackIntent(
                    self.candidate_id,
                    self.provider_id,
                    (self.locator_id,),
                    (0,),
                    client,
                ),
                provider_kind="stremthru_newz",
                owner_configuration_partition=self.partition,
                preparation_intent_key="a" * 64,
                now=20,
            )
            await playback_repository.record_pending_target(
                playback.preparation_id,
                owner_configuration_partition=self.partition,
                provider_account_partition=b"a" * 32,
                target_kind="relay",
                target_ref={
                    "provider_preparation_id": ledger.preparation_id,
                    "remote_id": "remote-1",
                },
                now=21,
            )
            playbacks.append(playback)

        self.assertEqual(
            playbacks[0].preparation_id,
            playbacks[1].preparation_id,
        )
        await playback_repository.record_pending_target(
            playbacks[0].preparation_id,
            owner_configuration_partition=self.partition,
            provider_account_partition=b"a" * 32,
            target_kind="relay",
            target_ref={
                "provider_preparation_id": ledger.preparation_id,
                "remote_id": "remote-1",
            },
            now=22,
        )
        counts = await self.database.fetch_all(
            """
            SELECT preparation_id, refcount
            FROM provider_preparations
            ORDER BY preparation_id
            """
        )
        self.assertEqual(
            {row["preparation_id"]: row["refcount"] for row in counts},
            {
                ledger.preparation_id: 1,
                other_ledger.preparation_id: 0,
            },
        )

        with self.assertRaisesRegex(ValueError, "reference is unavailable"):
            await playback_repository.record_pending_target(
                playbacks[0].preparation_id,
                owner_configuration_partition=self.partition,
                provider_account_partition=b"a" * 32,
                target_kind="relay",
                target_ref={
                    "provider_preparation_id": other_ledger.preparation_id,
                    "remote_id": "remote-2",
                },
                now=23,
            )

        await playback_repository.mark_failed(
            playbacks[0].preparation_id,
            owner_configuration_partition=self.partition,
            provider_account_partition=b"a" * 32,
            code="remote_failed",
            now=24,
        )
        await playback_repository.clear_pending_target(
            playbacks[1].preparation_id,
            owner_configuration_partition=self.partition,
            provider_account_partition=b"a" * 32,
            now=25,
        )
        counts = await self.database.fetch_all(
            """
            SELECT preparation_id, refcount
            FROM provider_preparations
            ORDER BY preparation_id
            """
        )
        self.assertEqual(
            {row["preparation_id"]: row["refcount"] for row in counts},
            {
                ledger.preparation_id: 0,
                other_ledger.preparation_id: 0,
            },
        )

    async def test_unrelated_playback_does_not_retain_an_unreferenced_job(self):
        provider_repository = ProviderPreparationRepository(self.database)
        ledger, _ = await provider_repository.get_or_create(
            owner_configuration_partition=self.partition,
            provider_configuration_id=self.provider_id,
            credential_fingerprint="b" * 64,
            candidate_id=self.candidate_id,
            locator_id=self.locator_id,
            artifact_grant_id=self.grant_id,
            selection_json=provider_selection_json((0,)),
            mutation_idempotency_key="7" * 64,
            now=10,
        )
        await provider_repository.record_submission(
            ledger.preparation_id,
            owner_configuration_partition=self.partition,
            remote_id="remote-1",
            remote_hash="hash-1",
            status="failed",
            ownership="adopted",
            now=12,
        )
        await provider_repository.record_terminal_status(
            ledger.preparation_id,
            owner_configuration_partition=self.partition,
            status="failed",
            now=12,
        )
        await PlaybackPreparationRepository(self.database).get_or_create(
            PlaybackIntent(
                self.candidate_id,
                self.provider_id,
                (self.locator_id,),
                (1, 1, 2),
                "stremio",
            ),
            provider_kind="stremthru_newz",
            owner_configuration_partition=self.partition,
            preparation_intent_key="b" * 64,
            now=10_000,
        )

        removed = await provider_repository.garbage_collect(now=22_000)

        self.assertEqual(removed, 1)

    async def test_expired_unique_playback_is_recycled_and_releases_its_job(self):
        provider_repository = ProviderPreparationRepository(self.database)
        ledger, _ = await provider_repository.get_or_create(
            owner_configuration_partition=self.partition,
            provider_configuration_id=self.provider_id,
            credential_fingerprint="b" * 64,
            candidate_id=self.candidate_id,
            locator_id=self.locator_id,
            artifact_grant_id=self.grant_id,
            selection_json=provider_selection_json((0,)),
            mutation_idempotency_key="7" * 64,
            now=10,
        )
        repository = PlaybackPreparationRepository(self.database)
        intent = PlaybackIntent(
            self.candidate_id,
            self.provider_id,
            (self.locator_id,),
            (0,),
            "stremio",
        )
        expired = await repository.get_or_create(
            intent,
            provider_kind="stremthru_newz",
            owner_configuration_partition=self.partition,
            preparation_intent_key="a" * 64,
            ttl=10,
            now=20,
        )
        await repository.record_pending_target(
            expired.preparation_id,
            owner_configuration_partition=self.partition,
            provider_account_partition=b"a" * 32,
            target_kind="relay",
            target_ref={
                "provider_preparation_id": ledger.preparation_id,
                "remote_id": "remote-1",
            },
            now=21,
        )

        replacement = await repository.get_or_create(
            intent,
            provider_kind="stremthru_newz",
            owner_configuration_partition=self.partition,
            preparation_intent_key="a" * 64,
            ttl=10,
            now=30,
        )
        remote = await self.database.fetch_one(
            """
            SELECT refcount
            FROM provider_preparations
            WHERE preparation_id = :preparation_id
            """,
            {"preparation_id": ledger.preparation_id},
        )

        self.assertNotEqual(replacement.preparation_id, expired.preparation_id)
        self.assertGreater(replacement.expires_at, 30)
        self.assertEqual(remote["refcount"], 0)

    async def test_torbox_cleanup_claims_only_an_expired_created_job(self):
        repository = ProviderPreparationRepository(self.database)
        common = {
            "owner_configuration_partition": self.partition,
            "provider_configuration_id": self.provider_id,
            "credential_fingerprint": "b" * 64,
            "candidate_id": self.candidate_id,
            "locator_id": self.locator_id,
            "artifact_grant_id": None,
            "selection_json": provider_selection_json((0,)),
            "provider_kind": "torbox_usenet",
        }
        created, _ = await repository.get_or_create(
            **common,
            mutation_idempotency_key="1" * 64,
            now=10,
        )
        adopted, _ = await repository.get_or_create(
            **common,
            mutation_idempotency_key="2" * 64,
            now=10,
        )
        await repository.record_submission(
            created.preparation_id,
            owner_configuration_partition=self.partition,
            remote_id="17",
            remote_hash="a" * 32,
            status="failed",
            ownership="created",
            now=12,
        )
        await repository.record_terminal_status(
            created.preparation_id,
            owner_configuration_partition=self.partition,
            status="failed",
            now=12,
        )
        await repository.record_submission(
            adopted.preparation_id,
            owner_configuration_partition=self.partition,
            remote_id="18",
            remote_hash="a" * 32,
            status="failed",
            ownership="adopted",
            now=12,
        )
        await repository.record_terminal_status(
            adopted.preparation_id,
            owner_configuration_partition=self.partition,
            status="failed",
            now=12,
        )
        playback_repository = PlaybackPreparationRepository(self.database)
        playback = await playback_repository.get_or_create(
            PlaybackIntent(
                self.candidate_id,
                self.provider_id,
                (self.locator_id,),
                (0,),
                "stremio",
            ),
            provider_kind="torbox_usenet",
            owner_configuration_partition=self.partition,
            preparation_intent_key="a" * 64,
            ttl=6 * 60 * 60,
            now=500,
        )
        await playback_repository.record_pending_target(
            playback.preparation_id,
            owner_configuration_partition=self.partition,
            provider_account_partition=b"a" * 32,
            target_kind="relay",
            target_ref={
                "provider_preparation_id": created.preparation_id,
                "remote_id": "17",
            },
            now=501,
        )

        removed_before_cleanup = await repository.garbage_collect(now=22_000)
        retained = await repository.claim_torbox_cleanup(
            owner_configuration_partition=self.partition,
            provider_configuration_id=self.provider_id,
            credential_fingerprint="b" * 64,
            now=22_000,
        )
        await playback_repository.clear_pending_target(
            playback.preparation_id,
            owner_configuration_partition=self.partition,
            provider_account_partition=b"a" * 32,
            now=22_000,
        )
        target = await repository.claim_torbox_cleanup(
            owner_configuration_partition=self.partition,
            provider_configuration_id=self.provider_id,
            credential_fingerprint="b" * 64,
            now=22_000,
        )
        assert target is not None
        with self.assertRaisesRegex(ValueError, "reference is unavailable"):
            await playback_repository.record_pending_target(
                playback.preparation_id,
                owner_configuration_partition=self.partition,
                provider_account_partition=b"a" * 32,
                target_kind="relay",
                target_ref={
                    "provider_preparation_id": created.preparation_id,
                    "remote_id": "17",
                },
                now=22_000.5,
            )
        await repository.record_torbox_cleanup_complete(
            target.preparation_id,
            owner_configuration_partition=self.partition,
            usenet_id=target.usenet_id,
            now=22_001,
        )
        removed_after_cleanup = await repository.garbage_collect(now=22_002)
        remaining = await self.database.fetch_all(
            """
            SELECT preparation_id, cleanup_state
            FROM provider_preparations
            ORDER BY preparation_id
            """
        )

        self.assertEqual(removed_before_cleanup, 1)
        self.assertIsNone(retained)
        self.assertEqual(
            (target.preparation_id, target.usenet_id),
            (created.preparation_id, 17),
        )
        self.assertEqual(removed_after_cleanup, 1)
        self.assertEqual(remaining, [])

    async def test_selected_file_seals_a_reusable_terminal_result(self):
        repository = ProviderPreparationRepository(self.database)
        arguments = {
            "owner_configuration_partition": self.partition,
            "provider_configuration_id": self.provider_id,
            "credential_fingerprint": "b" * 64,
            "candidate_id": self.candidate_id,
            "locator_id": self.locator_id,
            "artifact_grant_id": self.grant_id,
            "selection_json": provider_selection_json((0,)),
            "mutation_idempotency_key": "f" * 64,
        }
        ledger, _created = await repository.get_or_create(**arguments, now=10)
        await repository.record_submission(
            ledger.preparation_id,
            owner_configuration_partition=self.partition,
            remote_id="remote-1",
            remote_hash="hash-1",
            status="downloaded",
            ownership="created",
            now=11,
        )

        await repository.record_selected_file(
            ledger.preparation_id,
            owner_configuration_partition=self.partition,
            remote_id="remote-1",
            remote_hash="hash-1",
            file_index=3,
            file_size=42,
            locked_link="locked-ref",
            now=12,
        )
        await repository.record_selected_file(
            ledger.preparation_id,
            owner_configuration_partition=self.partition,
            remote_id="remote-1",
            remote_hash="hash-1",
            file_index=3,
            file_size=42,
            locked_link="locked-ref",
            now=13,
        )
        with self.assertRaisesRegex(ValueError, "provider file is unavailable"):
            await repository.record_selected_file(
                ledger.preparation_id,
                owner_configuration_partition=self.partition,
                remote_id="remote-1",
                remote_hash="hash-1",
                file_index=4,
                file_size=42,
                locked_link="locked-ref",
                now=13,
            )
        selected, created_again = await repository.get_or_create(**arguments, now=14)
        row = await self.database.fetch_one(
            """
            SELECT terminal_at, gc_after_at
            FROM provider_preparations
            WHERE preparation_id = :preparation_id
            """,
            {"preparation_id": ledger.preparation_id},
        )

        self.assertFalse(created_again)
        self.assertEqual(selected.state, "terminal")
        self.assertEqual(
            selected.payload,
            {
                "remote_id": "remote-1",
                "remote_hash": "hash-1",
                "file_index": 3,
                "file_size": 42,
                "locked_link": "locked-ref",
                "status": "selected",
                "ownership": "created",
                "missing_count": 0,
            },
        )
        self.assertEqual(
            (row["terminal_at"], row["gc_after_at"]),
            (12, 12 + 6 * 60 * 60),
        )

    async def test_missing_remote_item_allows_exactly_one_sealed_readd(self):
        repository = ProviderPreparationRepository(self.database)
        arguments = {
            "owner_configuration_partition": self.partition,
            "provider_configuration_id": self.provider_id,
            "credential_fingerprint": "b" * 64,
            "candidate_id": self.candidate_id,
            "locator_id": self.locator_id,
            "artifact_grant_id": self.grant_id,
            "selection_json": provider_selection_json((0,)),
            "mutation_idempotency_key": "1" * 64,
        }
        ledger, _created = await repository.get_or_create(**arguments, now=10)
        await repository.record_submission(
            ledger.preparation_id,
            owner_configuration_partition=self.partition,
            remote_id="remote-1",
            remote_hash="hash-1",
            status="queued",
            ownership="created",
            now=11,
        )

        first_missing = await repository.record_stremthru_missing(
            ledger.preparation_id,
            owner_configuration_partition=self.partition,
            now=12,
        )
        pending_readd, _created = await repository.get_or_create(
            **arguments,
            now=13,
        )
        began = await repository.begin_stremthru_resubmission(
            ledger.preparation_id,
            owner_configuration_partition=self.partition,
            now=14,
        )
        began_twice = await repository.begin_stremthru_resubmission(
            ledger.preparation_id,
            owner_configuration_partition=self.partition,
            now=15,
        )
        await repository.record_stremthru_resubmission(
            ledger.preparation_id,
            owner_configuration_partition=self.partition,
            remote_id="remote-2",
            remote_hash="hash-2",
            status="waiting_for_new_pipeline",
            now=16,
        )
        resubmitted, _created = await repository.get_or_create(
            **arguments,
            now=17,
        )
        second_missing = await repository.record_stremthru_missing(
            ledger.preparation_id,
            owner_configuration_partition=self.partition,
            now=18,
        )
        terminal, _created = await repository.get_or_create(
            **arguments,
            now=19,
        )

        self.assertTrue(first_missing)
        self.assertEqual(
            pending_readd.payload,
            {
                "status": "remote_missing",
                "ownership": "created",
                "missing_count": 1,
            },
        )
        self.assertTrue(began)
        self.assertFalse(began_twice)
        self.assertEqual(
            resubmitted.payload,
            {
                "remote_id": "remote-2",
                "remote_hash": "hash-2",
                "status": "waiting_for_new_pipeline",
                "ownership": "created",
                "missing_count": 1,
            },
        )
        self.assertFalse(second_missing)
        self.assertEqual(terminal.state, "terminal")
        self.assertEqual(
            terminal.payload,
            {
                "status": "remote_item_missing",
                "ownership": "created",
                "missing_count": 1,
            },
        )

    async def test_sab_absence_requires_two_consecutive_observations(self):
        repository = ProviderPreparationRepository(self.database)
        arguments = {
            "owner_configuration_partition": self.partition,
            "provider_configuration_id": self.provider_id,
            "credential_fingerprint": "b" * 64,
            "candidate_id": self.candidate_id,
            "locator_id": self.locator_id,
            "artifact_grant_id": self.grant_id,
            "selection_json": provider_selection_json((0,)),
            "mutation_idempotency_key": "2" * 64,
            "provider_kind": "nzbdav",
        }
        ledger, _created = await repository.get_or_create(**arguments, now=10)
        await repository.record_submission(
            ledger.preparation_id,
            owner_configuration_partition=self.partition,
            remote_id="remote-1",
            remote_hash="hash-1",
            status="downloading_on_new_pipeline",
            ownership="created",
            now=11,
        )

        first = await repository.record_sab_absence(
            ledger.preparation_id,
            owner_configuration_partition=self.partition,
            now=12,
        )
        missing_once, _created = await repository.get_or_create(**arguments, now=13)
        reappeared = await repository.clear_sab_absence(
            ledger.preparation_id,
            owner_configuration_partition=self.partition,
            now=14,
        )
        present, _created = await repository.get_or_create(**arguments, now=15)
        first_again = await repository.record_sab_absence(
            ledger.preparation_id,
            owner_configuration_partition=self.partition,
            now=16,
        )
        second = await repository.record_sab_absence(
            ledger.preparation_id,
            owner_configuration_partition=self.partition,
            now=17,
        )
        confirmed = await repository.record_sab_absence(
            ledger.preparation_id,
            owner_configuration_partition=self.partition,
            now=21,
        )
        terminal, _created = await repository.get_or_create(**arguments, now=22)

        self.assertEqual(first, "pending")
        self.assertEqual(
            missing_once.payload,
            {
                "remote_id": "remote-1",
                "remote_hash": "hash-1",
                "status": "remote_missing",
                "ownership": "created",
                "missing_count": 1,
                "previous_status": "downloading_on_new_pipeline",
                "missing_observed_at": 12,
            },
        )
        self.assertTrue(reappeared)
        self.assertEqual(
            present.payload,
            {
                "remote_id": "remote-1",
                "remote_hash": "hash-1",
                "status": "downloading_on_new_pipeline",
                "ownership": "created",
                "missing_count": 0,
            },
        )
        self.assertEqual(
            (first_again, second, confirmed),
            ("pending", "pending", "terminal"),
        )
        self.assertEqual(terminal.state, "terminal")
        self.assertEqual(
            terminal.payload,
            {
                "remote_id": "remote-1",
                "remote_hash": "hash-1",
                "status": "remote_item_missing",
                "ownership": "created",
                "missing_count": 1,
            },
        )

    async def test_asset_preparation_pins_its_exact_owner_grant_and_manifest(self):
        repository = PlaybackPreparationRepository(self.database)
        preparation = await repository.get_or_create(
            PlaybackIntent(
                self.candidate_id,
                self.provider_id,
                (self.locator_id,),
                (0,),
                "stremio",
            ),
            provider_kind="nzbdav",
            owner_configuration_partition=self.partition,
            preparation_intent_key="a" * 64,
            now=10,
        )

        await repository.bind_artifact(
            preparation.preparation_id,
            owner_configuration_partition=self.partition,
            artifact_grant_id=self.grant_id,
            artifact_sha256="a" * 64,
            manifest_identity="nm1:" + "b" * 64,
            now=11,
        )
        await repository.mark_ready(
            preparation.preparation_id,
            owner_configuration_partition=self.partition,
            provider_account_partition=b"a" * 32,
            target_kind="relay",
            target_ref={
                "byte_size": 42,
                "relative_path": "folder/Movie.2026.mkv",
                "selected_asset_id": "e" * 64,
                "strong_asset_revision": "f" * 64,
            },
            now=11.5,
        )

        row = await self.database.fetch_one(
            """
            SELECT artifact_grant_id, manifest_identity, last_used_at
            FROM asset_preparations
            WHERE preparation_id = :preparation_id
            """,
            {"preparation_id": preparation.preparation_id},
        )
        self.assertEqual(row["artifact_grant_id"], self.grant_id)
        self.assertEqual(row["manifest_identity"], "nm1:" + "b" * 64)
        self.assertEqual(row["last_used_at"], 11.5)
        resolution = await self.database.fetch_one(
            """
            SELECT provider_kind, provider_configuration_id,
                   account_partition, candidate_id,
                   selection_intent_json, selected_asset_id, client,
                   target_kind, resolution_version, exact_logical_length,
                   strong_asset_revision, etag_strength, observed_at_ms,
                   last_used_at_ms
            FROM provider_resolution_cache
            """
        )
        self.assertEqual(resolution["provider_kind"], "nzbdav")
        self.assertEqual(resolution["provider_configuration_id"], self.provider_id)
        self.assertEqual(resolution["account_partition"], (b"a" * 32).hex())
        self.assertEqual(resolution["candidate_id"], self.generic_candidate_id)
        self.assertEqual(resolution["selection_intent_json"], "[0]")
        self.assertEqual(resolution["selected_asset_id"], "e" * 64)
        self.assertEqual(resolution["client"], "stremio")
        self.assertEqual(resolution["target_kind"], "relay")
        self.assertEqual(resolution["resolution_version"], 1)
        self.assertEqual(resolution["exact_logical_length"], 42)
        self.assertEqual(resolution["strong_asset_revision"], "f" * 64)
        self.assertEqual(resolution["etag_strength"], "strong")
        self.assertEqual(resolution["observed_at_ms"], 11_500)
        self.assertEqual(resolution["last_used_at_ms"], 11_500)
        with self.assertRaisesRegex(ValueError, "unavailable"):
            await repository.bind_artifact(
                preparation.preparation_id,
                owner_configuration_partition=self.partition,
                artifact_grant_id=self.grant_id,
                artifact_sha256="a" * 64,
                manifest_identity="nm1:" + "d" * 64,
                now=12,
            )

    async def test_gc_index_is_present(self):
        indexes = await self.database.fetch_all(
            "PRAGMA index_list(provider_preparations)"
        )
        self.assertIn(
            "idx_provider_preparations_gc_v1",
            {row["name"] for row in indexes},
        )
        self.assertIn(
            "idx_provider_preparations_cleanup_v1",
            {row["name"] for row in indexes},
        )
