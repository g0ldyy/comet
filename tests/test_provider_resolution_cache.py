import unittest
import uuid
from tempfile import TemporaryDirectory

from databases import Database

from comet.core.db_router import ReplicaAwareDatabase
from comet.core.schema_migrations import (
    MigrationContext,
    _ensure_usenet_schema,
)
from comet.playback.resolution_cache import ProviderResolutionCacheRepository


class ProviderResolutionCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory()
        self.database = ReplicaAwareDatabase(
            Database(f"sqlite+aiosqlite:///{self.temporary.name}/resolution.db")
        )
        await self.database.connect()
        context = MigrationContext(self.database, is_sqlite=True, is_postgres=False)
        await _ensure_usenet_schema(context)
        self.generic_ids = (str(uuid.uuid4()), str(uuid.uuid4()))
        self.rendered_ids = (str(uuid.uuid4()), str(uuid.uuid4()))
        self.owner = b"o" * 32
        for index, (generic_id, rendered_id) in enumerate(
            zip(self.generic_ids, self.rendered_ids)
        ):
            await self.database.execute(
                """
                INSERT INTO release_candidates (
                    candidate_id, visibility_partition, media_id, transport,
                    release_key, scope, season_norm, episode_norm, title,
                    parsed_json, attributes_json, created_at_ms,
                    updated_at_ms, last_seen_at_ms
                ) VALUES (
                    :candidate_id, :partition, 'tt1', 'usenet',
                    :release_key, 'movie', -1, -1, 'Example', '{}', '{}',
                    1, 1, 1
                )
                """,
                {
                    "candidate_id": generic_id,
                    "partition": self.owner.hex(),
                    "release_key": f"release-{index}",
                },
            )
            await self.database.execute(
                """
                INSERT INTO rendered_release_candidates (
                    candidate_id, owner_configuration_partition,
                    external_candidate_id, media_id, transport, title,
                    parsed_json, created_at, updated_at, last_rendered_at
                ) VALUES (
                    :rendered_id, :partition, :generic_id, 'tt1', 'usenet',
                    'Example', '{}', 1, 1, 1
                )
                """,
                {
                    "rendered_id": rendered_id,
                    "partition": self.owner.hex(),
                    "generic_id": generic_id,
                },
            )
        self.repository = ProviderResolutionCacheRepository(self.database)
        self.provider_id = str(uuid.uuid4())
        self.account = b"a" * 32

    async def asyncTearDown(self):
        await self.database.disconnect()
        self.temporary.cleanup()

    def arguments(
        self,
        index: int = 0,
        *,
        account: bytes | None = None,
        client: str = "stremio",
        length: int = 42,
        target_kind: str = "relay",
    ) -> dict[str, object]:
        return {
            "rendered_candidate_id": self.rendered_ids[index],
            "provider_kind": "easynews",
            "provider_configuration_id": self.provider_id,
            "account_partition": account or self.account,
            "selection_intent_json": "[0]",
            "client": client,
            "target_kind": target_kind,
            "representation": {
                "selected_asset_id": "b" * 64,
                "exact_logical_length": length,
                "strong_asset_revision": "c" * 64,
                "etag_strength": "strong",
                "url": "https://must-not-persist.example/signed",
                "headers": {"Authorization": "secret"},
            },
        }

    async def test_exact_scope_round_trips_only_safe_representation_metadata(self):
        await self.repository.record_ready(
            **self.arguments(),
            observed_at=10,
            expires_at=100,
        )

        loaded_scope = await self.repository._scope(
            rendered_candidate_id=self.rendered_ids[0],
            provider_kind="easynews",
            provider_configuration_id=self.provider_id,
            account_partition=self.account,
            selection_intent_json="[0]",
            client="stremio",
        )
        other_account_scope = await self.repository._scope(
            rendered_candidate_id=self.rendered_ids[0],
            provider_kind="easynews",
            provider_configuration_id=self.provider_id,
            account_partition=b"x" * 32,
            selection_intent_json="[0]",
            client="stremio",
        )
        other_client_scope = await self.repository._scope(
            rendered_candidate_id=self.rendered_ids[0],
            provider_kind="easynews",
            provider_configuration_id=self.provider_id,
            account_partition=self.account,
            selection_intent_json="[0]",
            client="kodi",
        )
        loaded = await self.repository._load(loaded_scope, 20)
        other_account = await self.repository._load(other_account_scope, 20)
        other_client = await self.repository._load(other_client_scope, 20)
        row = await self.database.fetch_one("SELECT * FROM provider_resolution_cache")
        indexes = await self.database.fetch_all(
            "PRAGMA index_list(provider_resolution_cache)"
        )

        self.assertEqual(loaded["exact_logical_length"], 42)
        self.assertEqual(loaded["last_used_at_ms"], 20_000)
        self.assertIsNone(other_account)
        self.assertIsNone(other_client)
        self.assertEqual(row["candidate_id"], self.generic_ids[0])
        self.assertEqual(row["account_partition"], self.account.hex())
        self.assertNotIn("url", row)
        self.assertNotIn("headers", row)
        self.assertNotIn("secret", repr(dict(row)))
        self.assertTrue(
            {
                "idx_provider_resolution_lookup_v1",
                "idx_provider_resolution_expiry_v1",
            }.issubset({index["name"] for index in indexes})
        )

    async def test_active_representation_change_fails_but_expired_row_replaces(self):
        await self.repository.record_ready(
            **self.arguments(length=42),
            observed_at=10,
            expires_at=100,
        )
        with self.assertRaisesRegex(ValueError, "representation changed"):
            await self.repository.record_ready(
                **self.arguments(length=43),
                observed_at=20,
                expires_at=120,
            )
        with self.assertRaisesRegex(ValueError, "representation changed"):
            await self.repository.record_ready(
                **self.arguments(target_kind="cloud"),
                observed_at=20,
                expires_at=120,
            )

        await self.repository.record_ready(
            **self.arguments(length=43),
            observed_at=101,
            expires_at=200,
        )
        row = await self.database.fetch_one(
            """
            SELECT exact_logical_length, observed_at_ms, expires_at_ms
            FROM provider_resolution_cache
            """
        )
        self.assertEqual(row["exact_logical_length"], 43)
        self.assertEqual(row["observed_at_ms"], 101_000)
        self.assertEqual(row["expires_at_ms"], 200_000)

    async def test_active_representation_cannot_drop_pinned_identity(self):
        mutations = (
            {"selected_asset_id": None},
            {"exact_logical_length": None},
            {
                "strong_asset_revision": None,
                "etag_strength": "weak",
            },
            {"etag_strength": "weak"},
        )
        for index, mutation in enumerate(mutations):
            with self.subTest(mutation=mutation):
                arguments = self.arguments(
                    account=bytes([index + 1]) * 32,
                )
                await self.repository.record_ready(
                    **arguments,
                    observed_at=10,
                    expires_at=100,
                )
                weakened = {
                    **arguments,
                    "representation": {
                        **arguments["representation"],
                        **mutation,
                    },
                }

                with self.assertRaisesRegex(
                    ValueError,
                    "representation changed",
                ):
                    await self.repository.record_ready(
                        **weakened,
                        observed_at=20,
                        expires_at=120,
                    )
                with self.assertRaisesRegex(
                    ValueError,
                    "representation changed",
                ):
                    await self.repository.validate_ready(
                        **weakened,
                        observed_at=20,
                        expires_at=120,
                    )

    async def test_candidate_reassignment_keeps_the_most_recent_scope(self):
        winner, loser = sorted(self.generic_ids)
        winner_index = self.generic_ids.index(winner)
        loser_index = self.generic_ids.index(loser)
        await self.repository.record_ready(
            **self.arguments(winner_index),
            observed_at=20,
            expires_at=120,
        )
        await self.repository.record_ready(
            **self.arguments(loser_index),
            observed_at=10,
            expires_at=110,
        )
        loser_scope = await self.repository._scope(
            rendered_candidate_id=self.rendered_ids[loser_index],
            provider_kind="easynews",
            provider_configuration_id=self.provider_id,
            account_partition=self.account,
            selection_intent_json="[0]",
            client="stremio",
        )
        await self.repository._load(loser_scope, 30)

        await self.repository.reassign_candidate(loser, winner)

        rows = await self.database.fetch_all(
            "SELECT candidate_id, observed_at_ms FROM provider_resolution_cache"
        )
        self.assertEqual(
            [(row["candidate_id"], row["observed_at_ms"]) for row in rows],
            [(winner, 20_000)],
        )

    async def test_expiry_cleanup_is_bounded(self):
        await self.repository.record_ready(
            **self.arguments(client="stremio"),
            observed_at=1,
            expires_at=5,
        )
        await self.repository.record_ready(
            **self.arguments(client="kodi"),
            observed_at=1,
            expires_at=5,
        )

        self.assertEqual(
            await self.repository.cleanup_expired(now=6, limit=1),
            1,
        )
        self.assertEqual(
            await self.repository.cleanup_expired(now=6, limit=1),
            1,
        )
        self.assertEqual(
            await self.repository.cleanup_expired(now=6, limit=1),
            0,
        )
        for limit in (True, 1.5, 257):
            with (
                self.subTest(limit=limit),
                self.assertRaisesRegex(ValueError, "cleanup limit"),
            ):
                await self.repository.cleanup_expired(now=6, limit=limit)
