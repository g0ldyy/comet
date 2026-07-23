import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from comet.cometnet.pools import (
    MemberRole,
    PoolInvite,
    PoolManifest,
    PoolMember,
    PoolStore,
)
from comet.utils.atomic_file import write_text_atomic


class CometNetPoolStoreTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _manifest(display_name="Original"):
        return PoolManifest(
            pool_id="pool-a",
            creator_key="creator-key",
            display_name=display_name,
            members=[
                PoolMember(
                    public_key="creator-key",
                    role=MemberRole.CREATOR,
                    added_by="creator-key",
                )
            ],
        )

    async def test_load_rejects_partially_invalid_auxiliary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "memberships.json").write_text(
                '["member-b",null,"member-a","member-a",3,""]'
            )
            (root / "subscriptions.json").write_text('"not-a-list"')
            (root / "pool_peers.json").write_text(
                '{"pool-a":["wss://one",null,"wss://one",""],'
                '"pool-b":"not-a-list","pool-c":[]}'
            )

            with patch(
                "comet.cometnet.pools.settings.COMETNET_TRUSTED_POOLS",
                ["configured"],
            ):
                store = PoolStore(directory)
                await store.load()

            self.assertEqual(store._memberships, set())
            self.assertEqual(store._subscriptions, {"configured"})
            self.assertEqual(store._pool_peers, {})

    async def test_load_accepts_only_exact_auxiliary_file_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "memberships.json").write_text('["pool-a","pool-b"]')
            (root / "subscriptions.json").write_text('["pool-a"]')
            (root / "pool_peers.json").write_text(
                '{"pool-a":["wss://one","wss://two"],"pool-b":[]}'
            )

            store = PoolStore(directory)
            await store.load()

            self.assertEqual(store.get_memberships(), {"pool-a", "pool-b"})
            self.assertEqual(store.get_subscriptions(), {"pool-a"})
            self.assertEqual(
                store._pool_peers,
                {"pool-a": {"wss://one", "wss://two"}, "pool-b": set()},
            )

    async def test_invite_load_requires_matching_persisted_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PoolStore(directory)
            await store.store_manifest(self._manifest())
            invite = PoolInvite(
                pool_id="pool-a",
                invite_code="valid-code",
                created_by="creator-key",
                signature="signature",
            )
            invite_dir = Path(directory, "invites", "pool-a")
            invite_dir.mkdir(parents=True, exist_ok=True)
            (invite_dir / "valid-code.json").write_text(invite.model_dump_json())

            wrong_pool = invite.model_copy(
                update={"pool_id": "pool-b", "invite_code": "wrong-pool"}
            )
            (invite_dir / "wrong-pool.json").write_text(wrong_pool.model_dump_json())
            wrong_code = invite.model_copy(update={"invite_code": "inside-code"})
            (invite_dir / "outside-code.json").write_text(wrong_code.model_dump_json())
            unsigned = invite.model_copy(
                update={"invite_code": "unsigned", "signature": ""}
            )
            (invite_dir / "unsigned.json").write_text(unsigned.model_dump_json())

            reloaded = PoolStore(directory)
            await reloaded.load()

            self.assertEqual(
                [loaded.invite_code for loaded in reloaded.get_invites("pool-a")],
                ["valid-code"],
            )

    async def test_manifest_snapshots_require_an_explicit_successful_store(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PoolStore(directory)
            await store.store_manifest(self._manifest())

            detached = store.get_manifest("pool-a")
            detached.display_name = "Mutated outside store"

            self.assertEqual(store.get_manifest("pool-a").display_name, "Original")

    async def test_manifest_persistence_excludes_derived_node_ids_and_reloads(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PoolStore(directory)
            await store.store_manifest(self._manifest())

            manifest_path = Path(directory, "manifests", "pool-a.json")
            persisted = json.loads(manifest_path.read_text())
            self.assertNotIn("node_id", persisted["members"][0])

            reloaded = PoolStore(directory)
            await reloaded.load()
            self.assertEqual(
                reloaded.get_manifest("pool-a").members[0].public_key,
                "creator-key",
            )

    async def test_remote_manifest_requires_existing_pool_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PoolStore(directory)
            await store.store_manifest(self._manifest())

            valid_update = store.get_manifest("pool-a")
            valid_update.members.append(
                PoolMember(
                    public_key="new-member",
                    added_by="creator-key",
                )
            )
            valid_update.version = 2
            valid_update.updated_at += 1
            valid_update.signatures = {"creator-key": "signature"}

            with patch(
                "comet.cometnet.pools.NodeIdentity.verify_hex_async",
                new=AsyncMock(return_value=True),
            ) as verify:
                accepted, previous = await store.accept_remote_manifest(valid_update)

            self.assertTrue(accepted)
            self.assertEqual(previous.version, 1)
            verify.assert_awaited_once()

            takeover = PoolManifest(
                pool_id="pool-a",
                creator_key="attacker-key",
                display_name="Taken over",
                members=[
                    PoolMember(
                        public_key="attacker-key",
                        role=MemberRole.CREATOR,
                        added_by="attacker-key",
                    )
                ],
                version=3,
                signatures={"attacker-key": "signature"},
            )
            with patch(
                "comet.cometnet.pools.NodeIdentity.verify_hex_async",
                new=AsyncMock(return_value=True),
            ) as verify_takeover:
                accepted, previous = await store.accept_remote_manifest(takeover)

            self.assertFalse(accepted)
            self.assertEqual(previous.version, 2)
            verify_takeover.assert_not_awaited()
            self.assertEqual(store.get_manifest("pool-a").creator_key, "creator-key")

    async def test_manifest_model_rejects_non_current_or_inconsistent_data(self):
        valid = self._manifest().to_persisted_dict()
        malformed = []

        root_extra = copy.deepcopy(valid)
        root_extra["legacy"] = True
        malformed.append(root_extra)

        member_extra = copy.deepcopy(valid)
        member_extra["members"][0]["node_id"] = "derived"
        malformed.append(member_extra)

        boolean_version = copy.deepcopy(valid)
        boolean_version["version"] = True
        malformed.append(boolean_version)

        boolean_count = copy.deepcopy(valid)
        boolean_count["members"][0]["contribution_count"] = True
        malformed.append(boolean_count)

        non_finite_timestamp = copy.deepcopy(valid)
        non_finite_timestamp["updated_at"] = float("nan")
        malformed.append(non_finite_timestamp)

        duplicate_member = copy.deepcopy(valid)
        duplicate_member["members"].append(
            copy.deepcopy(duplicate_member["members"][0])
        )
        malformed.append(duplicate_member)

        mismatched_creator = copy.deepcopy(valid)
        mismatched_creator["creator_key"] = "someone-else"
        malformed.append(mismatched_creator)

        non_canonical_id = copy.deepcopy(valid)
        non_canonical_id["pool_id"] = " Pool-A "
        malformed.append(non_canonical_id)

        for data in malformed:
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    PoolManifest.model_validate(data)

    async def test_load_isolates_invalid_and_misnamed_manifests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = PoolStore(directory)
            valid = self._manifest().to_persisted_dict()
            (root / "manifests" / "pool-a.json").write_text(json.dumps(valid))

            invalid = copy.deepcopy(valid)
            invalid["pool_id"] = "pool-b"
            invalid["members"][0]["public_key"] = "other"
            (root / "manifests" / "pool-b.json").write_text(json.dumps(invalid))

            misnamed = copy.deepcopy(valid)
            misnamed["pool_id"] = "pool-c"
            (root / "manifests" / "wrong-name.json").write_text(json.dumps(misnamed))

            await store.load()

            self.assertEqual(set(store.get_all_manifests()), {"pool-a"})

    async def test_failed_manifest_store_preserves_published_state_and_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PoolStore(directory)
            await store.store_manifest(self._manifest())
            manifest_path = Path(directory, "manifests", "pool-a.json")
            original_bytes = manifest_path.read_bytes()
            updated = store.get_manifest("pool-a")
            updated.display_name = "Updated"

            with patch(
                "comet.cometnet.pools.write_text_atomic",
                side_effect=OSError("disk unavailable"),
            ):
                with self.assertRaisesRegex(OSError, "disk unavailable"):
                    await store.store_manifest(updated)

            self.assertEqual(store.get_manifest("pool-a").display_name, "Original")
            self.assertEqual(manifest_path.read_bytes(), original_bytes)

    async def test_failed_member_update_does_not_mutate_trusted_manifest(self):
        class Identity:
            public_key_hex = "creator-key"

            async def sign_hex_async(self, payload):
                del payload
                return "signature"

        with tempfile.TemporaryDirectory() as directory:
            store = PoolStore(directory)
            await store.store_manifest(self._manifest())

            with patch(
                "comet.cometnet.pools.write_text_atomic",
                side_effect=OSError("disk unavailable"),
            ):
                with self.assertRaisesRegex(OSError, "disk unavailable"):
                    await store.add_member("pool-a", "new-key", Identity())

            manifest = store.get_manifest("pool-a")
            self.assertEqual(
                [member.public_key for member in manifest.members], ["creator-key"]
            )
            self.assertEqual(manifest.version, 1)

    async def test_failed_invite_store_is_visible_and_not_published(self):
        class Identity:
            public_key_hex = "creator-key"

            async def sign_hex_async(self, payload):
                del payload
                return "signature"

        with tempfile.TemporaryDirectory() as directory:
            store = PoolStore(directory)
            await store.store_manifest(self._manifest())

            with patch(
                "comet.cometnet.pools.write_text_atomic",
                side_effect=OSError("disk unavailable"),
            ):
                with self.assertRaisesRegex(OSError, "disk unavailable"):
                    await store.create_invite("pool-a", Identity())

            self.assertEqual(store.get_invites("pool-a"), [])

    async def test_invite_snapshots_require_an_explicit_successful_save(self):
        class Identity:
            public_key_hex = "creator-key"

            async def sign_hex_async(self, payload):
                del payload
                return "signature"

        with tempfile.TemporaryDirectory() as directory:
            store = PoolStore(directory)
            await store.store_manifest(self._manifest())
            created = await store.create_invite("pool-a", Identity(), max_uses=2)

            detached = store.get_invite("pool-a", created.invite_code)
            detached.uses = 2

            self.assertEqual(store.get_invite("pool-a", created.invite_code).uses, 0)

    async def test_auxiliary_state_is_published_only_after_successful_save(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PoolStore(directory)

            with patch(
                "comet.cometnet.pools.write_text_atomic",
                side_effect=OSError("disk unavailable"),
            ):
                operations = [
                    store.add_membership("pool-a"),
                    store.subscribe("pool-a"),
                    store.add_pool_peer("pool-a", "wss://peer"),
                ]
                for operation in operations:
                    with self.subTest(operation=operation):
                        with self.assertRaisesRegex(OSError, "disk unavailable"):
                            await operation

            self.assertEqual(store.get_memberships(), set())
            self.assertEqual(store.get_subscriptions(), set())
            self.assertEqual(store.get_all_pool_peers(), {})

    async def test_concurrent_auxiliary_additions_do_not_lose_state(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PoolStore(directory)

            await asyncio.gather(
                store.add_membership("pool-a"),
                store.add_membership("pool-b"),
                store.subscribe("pool-a"),
                store.subscribe("pool-b"),
                store.add_pool_peer("pool-a", "wss://one"),
                store.add_pool_peer("pool-a", "wss://two"),
            )

            self.assertEqual(store.get_memberships(), {"pool-a", "pool-b"})
            self.assertEqual(store.get_subscriptions(), {"pool-a", "pool-b"})
            self.assertEqual(
                store.get_pool_peers("pool-a"),
                {"wss://one", "wss://two"},
            )

    async def test_save_serializes_auxiliary_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PoolStore(directory)
            await store.add_membership("pool-a")
            save_started = asyncio.Event()
            release_save = asyncio.Event()
            first_membership_write = True

            async def yielding_write(path, content):
                nonlocal first_membership_write
                if path.name == "memberships.json" and first_membership_write:
                    first_membership_write = False
                    save_started.set()
                    await release_save.wait()
                await write_text_atomic(path, content)

            with patch("comet.cometnet.pools.write_text_atomic", new=yielding_write):
                save_task = asyncio.create_task(store.save())
                await save_started.wait()
                addition_task = asyncio.create_task(store.add_membership("pool-b"))
                await asyncio.sleep(0)

                self.assertFalse(addition_task.done())
                release_save.set()
                await asyncio.gather(save_task, addition_task)

            persisted = json.loads(Path(directory, "memberships.json").read_text())
            self.assertEqual(persisted, ["pool-a", "pool-b"])

    async def test_delete_pool_cleans_persisted_and_published_state(self):
        class Identity:
            public_key_hex = "creator-key"

            async def sign_hex_async(self, payload):
                del payload
                return "signature"

        with tempfile.TemporaryDirectory() as directory:
            store = PoolStore(directory)
            await store.store_manifest(self._manifest())
            await store.add_membership("pool-a")
            await store.subscribe("pool-a")
            await store.add_pool_peer("pool-a", "wss://peer")
            await store.create_invite("pool-a", Identity())

            self.assertTrue(await store.delete_pool("pool-a"))

            self.assertIsNone(store.get_manifest("pool-a"))
            self.assertEqual(store.get_memberships(), set())
            self.assertEqual(store.get_subscriptions(), set())
            self.assertEqual(store.get_all_pool_peers(), {})
            self.assertEqual(store.get_invites("pool-a"), [])
            self.assertFalse(Path(directory, "manifests", "pool-a.json").exists())
            self.assertFalse(Path(directory, "invites", "pool-a").exists())

    async def test_delete_failure_is_visible_without_hiding_cached_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PoolStore(directory)
            await store.store_manifest(self._manifest())
            await store.add_membership("pool-a")
            await store.subscribe("pool-a")
            await store.add_pool_peer("pool-a", "wss://peer")
            manifest_path = Path(directory, "manifests", "pool-a.json")

            with patch(
                "comet.cometnet.pools.run_in_executor",
                new=AsyncMock(side_effect=OSError("unlink failed")),
            ):
                with self.assertRaisesRegex(OSError, "unlink failed"):
                    await store.delete_pool("pool-a")

            self.assertIsNotNone(store.get_manifest("pool-a"))
            self.assertTrue(manifest_path.exists())
            self.assertEqual(store.get_memberships(), set())
            self.assertEqual(store.get_subscriptions(), set())
            self.assertEqual(store.get_all_pool_peers(), {})

    async def test_concurrent_join_requests_cannot_overuse_invite(self):
        class Identity:
            public_key_hex = "creator-key"

            async def sign_hex_async(self, payload):
                del payload
                return "signature"

        with tempfile.TemporaryDirectory() as directory:
            store = PoolStore(directory)
            await store.store_manifest(self._manifest())
            invite = await store.create_invite("pool-a", Identity(), max_uses=1)

            results = await asyncio.gather(
                store.accept_invite_member(
                    "pool-a",
                    invite.invite_code,
                    "member-a",
                    signing_identity=Identity(),
                ),
                store.accept_invite_member(
                    "pool-a",
                    invite.invite_code,
                    "member-b",
                    signing_identity=Identity(),
                ),
            )

            self.assertEqual(sum(result is not None for result in results), 1)
            self.assertEqual(store.get_invite("pool-a", invite.invite_code).uses, 1)
            member_keys = {
                member.public_key for member in store.get_manifest("pool-a").members
            }
            self.assertEqual(len(member_keys & {"member-a", "member-b"}), 1)

    async def test_concurrent_admin_additions_do_not_lose_members(self):
        class Identity:
            public_key_hex = "creator-key"

            async def sign_hex_async(self, payload):
                del payload
                return "signature"

        with tempfile.TemporaryDirectory() as directory:
            store = PoolStore(directory)
            await store.store_manifest(self._manifest())
            active_writes = 0
            peak_writes = 0

            async def slow_write(path, content):
                nonlocal active_writes, peak_writes
                active_writes += 1
                peak_writes = max(peak_writes, active_writes)
                try:
                    await asyncio.sleep(0.01)
                    await write_text_atomic(path, content)
                finally:
                    active_writes -= 1

            with patch("comet.cometnet.pools.write_text_atomic", new=slow_write):
                results = await asyncio.gather(
                    store.add_member("pool-a", "member-a", Identity()),
                    store.add_member("pool-a", "member-b", Identity()),
                )

            manifest = store.get_manifest("pool-a")
            self.assertEqual(results, [True, True])
            self.assertEqual(peak_writes, 1)
            self.assertEqual(manifest.version, 3)
            self.assertEqual(
                {member.public_key for member in manifest.members},
                {"creator-key", "member-a", "member-b"},
            )

    async def test_concurrent_duplicate_pool_creation_has_one_winner(self):
        class Identity:
            public_key_hex = "creator-key"

            async def sign_hex_async(self, payload):
                del payload
                return "signature"

        with tempfile.TemporaryDirectory() as directory:
            store = PoolStore(directory)
            results = await asyncio.gather(
                store.create_pool("pool-a", "First", Identity()),
                store.create_pool("pool-a", "Second", Identity()),
                return_exceptions=True,
            )

            self.assertEqual(
                sum(isinstance(result, PoolManifest) for result in results), 1
            )
            self.assertEqual(
                sum(isinstance(result, ValueError) for result in results), 1
            )
            self.assertEqual(set(store.get_all_manifests()), {"pool-a"})
            self.assertEqual(store.get_memberships(), {"pool-a"})

    async def test_contribution_waits_for_authoritative_manifest_mutation(self):
        class Identity:
            public_key_hex = "creator-key"

            async def sign_hex_async(self, payload):
                del payload
                return "signature"

        with tempfile.TemporaryDirectory() as directory:
            store = PoolStore(directory)
            await store.store_manifest(self._manifest())
            write_started = asyncio.Event()
            release_write = asyncio.Event()

            async def yielding_write(path, content):
                write_started.set()
                await release_write.wait()
                await write_text_atomic(path, content)

            with patch("comet.cometnet.pools.write_text_atomic", new=yielding_write):
                member_task = asyncio.create_task(
                    store.add_member("pool-a", "member-key", Identity())
                )
                await write_started.wait()
                contribution_task = asyncio.create_task(
                    store.record_contribution("creator-key", "pool-a")
                )
                await asyncio.sleep(0)

                self.assertFalse(contribution_task.done())
                release_write.set()
                self.assertEqual(
                    await asyncio.gather(member_task, contribution_task),
                    [True, True],
                )

            await store.flush_dirty_manifests()
            reloaded = PoolStore(directory)
            await reloaded.load()
            manifest = reloaded.get_manifest("pool-a")
            self.assertEqual(
                {member.public_key for member in manifest.members},
                {"creator-key", "member-key"},
            )
            self.assertEqual(
                manifest.get_member("creator-key").contribution_count,
                1,
            )

    async def test_failed_dirty_flush_is_visible_and_remains_retryable(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PoolStore(directory)
            await store.store_manifest(self._manifest())
            await store.record_contribution("creator-key", "pool-a", count=2)

            with patch(
                "comet.cometnet.pools.write_text_atomic",
                side_effect=OSError("disk unavailable"),
            ):
                with self.assertRaisesRegex(OSError, "disk unavailable"):
                    await store.flush_dirty_manifests()

            self.assertEqual(store._dirty_manifests, {"pool-a"})
            self.assertEqual(
                store.get_manifest("pool-a")
                .get_member("creator-key")
                .contribution_count,
                2,
            )

            await store.flush_dirty_manifests()
            self.assertEqual(store._dirty_manifests, set())
            reloaded = PoolStore(directory)
            await reloaded.load()
            self.assertEqual(
                reloaded.get_manifest("pool-a")
                .get_member("creator-key")
                .contribution_count,
                2,
            )

    async def test_contribution_arguments_require_current_exact_types(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PoolStore(directory)
            for arguments in (
                ("", None, 1),
                ("creator-key", "", 1),
                ("creator-key", None, True),
                ("creator-key", None, 0),
            ):
                with self.subTest(arguments=arguments):
                    with self.assertRaises(ValueError):
                        await store.record_contribution(*arguments)

    async def test_invite_limits_reject_boolean_zero_and_non_finite_values(self):
        class Identity:
            public_key_hex = "creator-key"

            async def sign_hex_async(self, payload):
                del payload
                return "signature"

        with tempfile.TemporaryDirectory() as directory:
            store = PoolStore(directory)
            await store.store_manifest(self._manifest())

            for arguments in [
                {"max_uses": True},
                {"max_uses": 0},
                {"expires_in": True},
                {"expires_in": 0},
            ]:
                with self.subTest(arguments=arguments):
                    with self.assertRaises(ValueError):
                        await store.create_invite("pool-a", Identity(), **arguments)

    async def test_invite_links_accept_only_the_current_exact_shape(self):
        self.assertEqual(
            PoolInvite.parse_link(
                "cometnet://join?pool=pool-a&code=invite-code&node=wss%3A%2F%2Fpeer"
            ),
            {"pool": "pool-a", "code": "invite-code", "node": "wss://peer"},
        )

        invalid_links = [
            "cometnet://pool/pool-a/invite/invite-code",
            "cometnet://join?pool=Pool-A&code=invite-code",
            "cometnet://join?pool=pool-a&code=",
            "cometnet://join?pool=pool-a&code=one&code=two",
            "cometnet://join?pool=pool-a&code=invite-code&legacy=true",
            "cometnet://join/path?pool=pool-a&code=invite-code",
            "cometnet://join?pool=pool-a&code=invite-code#fragment",
        ]
        for link in invalid_links:
            with self.subTest(link=link):
                self.assertIsNone(PoolInvite.parse_link(link))
