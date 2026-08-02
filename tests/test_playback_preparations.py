import unittest
import uuid
from unittest.mock import AsyncMock

from comet.playback.preparations import PlaybackPreparationRepository
from comet.playback.tokens import PlaybackIntent


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


def target_database(preparation_id: str):
    database = type("Database", (), {})()
    database.transaction = lambda: _Transaction()
    database.fetch_one = AsyncMock(
        side_effect=[
            {
                "candidate_id": str(uuid.uuid4()),
                "provider_configuration_id": str(uuid.uuid4()),
                "provider_kind": "easynews",
                "locator_ids_json": f'["{uuid.uuid4()}"]',
                "selection_intent_json": "[0]",
                "provider_preparation_id": None,
                "manifest_identity": None,
                "parser_version": 2,
                "client": "stremio",
                "state": "pending",
                "absolute_expires_at": 3601,
            },
            {"preparation_id": preparation_id},
        ]
    )
    database.execute = AsyncMock()
    return database


class PlaybackPreparationRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_artifact_binding_uses_postgres_safe_alias(self):
        database = type("Database", (), {})()
        database.transaction = lambda: _Transaction()
        database.fetch_one = AsyncMock(
            side_effect=[{"grant_id": "grant-id"}, {"preparation_id": "prep-id"}]
        )

        await PlaybackPreparationRepository(database).bind_artifact(
            "prep-id",
            owner_configuration_partition=b"a" * 32,
            artifact_grant_id="grant-id",
            artifact_sha256="b" * 64,
            manifest_identity="nm1:" + "c" * 64,
            now=1,
        )

        query = database.fetch_one.await_args_list[0].args[0]
        self.assertIn("nzb_artifact_grants AS grants", query)

    def test_representation_metadata_uses_the_ready_target_schema(self):
        self.assertEqual(
            PlaybackPreparationRepository.representation_metadata(
                {
                    "byte_size": 42,
                    "relative_path": "folder/Movie.mkv",
                    "file_size": 84,
                    "filename": "legacy.mp4",
                },
                state="ready",
            ),
            {
                "exact_logical_length": 42,
                "strong_asset_revision": None,
                "etag_strength": "weak",
                "selected_asset_id": None,
            },
        )

    def test_signed_locator_ids_must_be_canonical_and_unique(self):
        locator_id = str(uuid.uuid4())
        for locator_ids in (
            ("AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",),
            (locator_id, locator_id),
        ):
            with self.subTest(locator_ids=locator_ids), self.assertRaises(ValueError):
                PlaybackPreparationRepository._canonical_ids(locator_ids)

    async def test_creation_persists_only_signed_identifiers_and_selection(self):
        candidate_id, provider_id, locator_id = (str(uuid.uuid4()) for _ in range(3))
        row = {
            "preparation_id": str(uuid.uuid4()),
            "candidate_id": candidate_id,
            "provider_configuration_id": provider_id,
            "provider_kind": "torbox_usenet",
            "locator_ids_json": f'["{locator_id}"]',
            "selection_json": "[0]",
            "client": "stremio",
            "state": "pending",
            "target_kind": None,
            "target_ref_json": None,
            "provider_preparation_id": None,
            "expires_at": 3601,
        }
        database = type("Database", (), {})()
        database.fetch_one = AsyncMock(side_effect=[None, None, row])
        database.execute = AsyncMock()
        intent = PlaybackIntent(
            candidate_id, provider_id, (locator_id,), (0,), "stremio"
        )

        preparation = await PlaybackPreparationRepository(database).get_or_create(
            intent,
            provider_kind="torbox_usenet",
            owner_configuration_partition=b"a" * 32,
            preparation_intent_key="a" * 64,
            now=1,
        )

        self.assertEqual(preparation.state, "pending")
        self.assertEqual(preparation.locator_ids, (locator_id,))
        params = database.execute.await_args.args[1]
        self.assertNotIn("url", params["locator_ids_json"])
        self.assertEqual(params["selection_intent_json"], "[0]")

    async def test_resolution_is_partition_and_expiry_bound(self):
        preparation_id, locator_id = str(uuid.uuid4()), str(uuid.uuid4())
        database = type("Database", (), {})()
        database.fetch_one = AsyncMock(
            return_value={
                "preparation_id": preparation_id,
                "candidate_id": str(uuid.uuid4()),
                "provider_configuration_id": str(uuid.uuid4()),
                "provider_kind": "easynews",
                "locator_ids_json": f'["{locator_id}"]',
                "selection_json": "[1,2,3]",
                "client": "stremio",
                "state": "ready",
                "target_kind": "relay",
                "target_ref_json": '{"locator":"sealed"}',
                "provider_preparation_id": None,
                "expires_at": 101,
            }
        )

        preparation = await PlaybackPreparationRepository(database).resolve(
            preparation_id, owner_configuration_partition=b"a" * 32, now=100
        )

        self.assertEqual(preparation.target_ref, {"locator": "sealed"})
        self.assertEqual(
            database.fetch_one.await_args.args[1]["partition"], (b"a" * 32).hex()
        )

    async def test_ready_targets_preserve_opaque_provider_values(self):
        preparation_id = str(uuid.uuid4())
        database = target_database(preparation_id)
        repository = PlaybackPreparationRepository(database)
        await repository.mark_ready(
            preparation_id,
            owner_configuration_partition=b"a" * 32,
            provider_account_partition=b"b" * 32,
            target_kind="relay",
            target_ref={"locked_link": "future:https://opaque-provider-value"},
            now=1,
        )
        self.assertEqual(
            database.fetch_one.await_args.args[1]["reconstruction_blueprint_json"],
            '{"locked_link":"future:https://opaque-provider-value"}',
        )

    async def test_pending_targets_retain_state_until_the_provider_finishes(self):
        preparation_id = str(uuid.uuid4())
        database = target_database(preparation_id)

        await PlaybackPreparationRepository(database).record_pending_target(
            preparation_id,
            owner_configuration_partition=b"a" * 32,
            provider_account_partition=b"b" * 32,
            target_kind="relay",
            target_ref={"remote_job_id": "safe-id"},
            now=1,
        )

        query = database.fetch_one.await_args.args[0]
        self.assertIn("state = 'pending'", query)
