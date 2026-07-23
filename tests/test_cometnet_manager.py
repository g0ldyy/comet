import asyncio
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from comet.cometnet.manager import CometNetService
from comet.cometnet.pools import MemberRole, PoolManifest, PoolMember, PoolStore
from comet.cometnet.protocol import PoolManifestMessage, PoolMemberUpdate
from comet.utils.atomic_file import write_text_atomic


class CometNetManagerTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _pool_manifest():
        return PoolManifest(
            pool_id="pool-a",
            creator_key="creator-key",
            display_name="Pool A",
            members=[
                PoolMember(
                    public_key="creator-key",
                    role=MemberRole.CREATOR,
                    added_by="creator-key",
                )
            ],
        )

    async def test_member_delta_requires_a_current_admin_manifest_signature(self):
        for signer, accepted in [("rogue-key", False), ("creator-key", True)]:
            with self.subTest(signer=signer):
                service = CometNetService(enabled=True)
                service.pool_store = Mock(
                    get_manifest=Mock(return_value=self._pool_manifest()),
                    store_manifest=AsyncMock(),
                )
                service.transport = Mock(broadcast=AsyncMock())
                message = PoolMemberUpdate(
                    sender_id="relay-node",
                    signature="delta-signature",
                    pool_id="pool-a",
                    action="add",
                    member_key="new-member",
                    updated_by="creator-key",
                    manifest_signatures={signer: "manifest-signature"},
                )

                with (
                    patch(
                        "comet.cometnet.manager.NodeIdentity.verify_hex_async",
                        new=AsyncMock(return_value=True),
                    ) as verify_delta,
                    patch(
                        "comet.cometnet.manager.NodeIdentity.verify_hex",
                        return_value=True,
                    ),
                    patch.object(
                        service, "_send_pool_manifest", new=AsyncMock()
                    ) as send_manifest,
                ):
                    await service._apply_pool_member_update("relay-node", message)

                verify_delta.assert_awaited_once()
                if accepted:
                    service.pool_store.store_manifest.assert_awaited_once()
                    service.transport.broadcast.assert_awaited_once()
                    send_manifest.assert_not_awaited()
                else:
                    service.pool_store.store_manifest.assert_not_awaited()
                    service.transport.broadcast.assert_not_awaited()
                    send_manifest.assert_awaited_once()

    async def test_self_leave_is_persisted_only_as_admin_signed_state(self):
        for local_role, accepted in [
            (MemberRole.CREATOR, True),
            (MemberRole.MEMBER, False),
        ]:
            with self.subTest(local_role=local_role):
                manifest = self._pool_manifest()
                manifest.members.extend(
                    [
                        PoolMember(
                            public_key="leaving-key",
                            role=MemberRole.MEMBER,
                            added_by="creator-key",
                        ),
                        PoolMember(
                            public_key="local-key",
                            role=local_role,
                            added_by="creator-key",
                        ),
                    ]
                )
                if local_role is MemberRole.CREATOR:
                    manifest.members[0].role = MemberRole.ADMIN
                    manifest.creator_key = "local-key"

                async def store_manifest(updated, identity):
                    self.assertEqual(identity.public_key_hex, "local-key")
                    updated.signatures["local-key"] = "new-state-signature"

                service = CometNetService(enabled=True)
                service.identity = Mock(
                    public_key_hex="local-key",
                    node_id="local-node",
                )
                service.pool_store = Mock(
                    get_manifest=Mock(return_value=manifest),
                    store_manifest=AsyncMock(side_effect=store_manifest),
                )
                message = PoolMemberUpdate(
                    sender_id="leaving-node",
                    signature="leave-signature",
                    pool_id="pool-a",
                    action="leave",
                    member_key="leaving-key",
                    updated_by="leaving-key",
                )

                with (
                    patch(
                        "comet.cometnet.manager.NodeIdentity.verify_hex_async",
                        new=AsyncMock(return_value=True),
                    ),
                    patch.object(
                        service,
                        "_broadcast_pool_member_update",
                        new=AsyncMock(),
                    ) as broadcast_update,
                ):
                    await service._apply_pool_member_update("leaving-node", message)

                if accepted:
                    service.pool_store.store_manifest.assert_awaited_once()
                    broadcast_update.assert_awaited_once_with(
                        pool_id="pool-a",
                        action="remove",
                        member_key="leaving-key",
                        updated_by="local-key",
                        manifest_signatures={"local-key": "new-state-signature"},
                        exclude={"leaving-node"},
                    )
                else:
                    service.pool_store.store_manifest.assert_not_awaited()
                    broadcast_update.assert_not_awaited()

    async def test_concurrent_remote_member_deltas_do_not_lose_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PoolStore(directory)
            await store.store_manifest(self._pool_manifest())
            service = CometNetService(enabled=True)
            service.pool_store = store
            service.transport = Mock(broadcast=AsyncMock())
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

            messages = [
                PoolMemberUpdate(
                    sender_id="creator-node",
                    signature="delta-signature",
                    pool_id="pool-a",
                    action="add",
                    member_key=member_key,
                    updated_by="creator-key",
                    manifest_signatures={"creator-key": "manifest-signature"},
                )
                for member_key in ("member-a", "member-b")
            ]
            with (
                patch(
                    "comet.cometnet.manager.validate_message_security",
                    new=AsyncMock(return_value=True),
                ),
                patch(
                    "comet.cometnet.manager.NodeIdentity.verify_hex_async",
                    new=AsyncMock(return_value=True),
                ),
                patch(
                    "comet.cometnet.manager.NodeIdentity.verify_hex",
                    return_value=True,
                ),
                patch("comet.cometnet.pools.write_text_atomic", new=slow_write),
            ):
                await asyncio.gather(
                    *(
                        service._handle_pool_member_update("creator-node", message)
                        for message in messages
                    )
                )

            manifest = store.get_manifest("pool-a")
            self.assertEqual(peak_writes, 1)
            self.assertEqual(manifest.version, 3)
            self.assertEqual(
                {member.public_key for member in manifest.members},
                {"creator-key", "member-a", "member-b"},
            )

    async def test_received_torrent_save_failure_propagates_to_gossip(self):
        service = CometNetService(enabled=True)

        async def fail_save(metadata):
            del metadata
            raise RuntimeError("database unavailable")

        service.set_save_torrent_callback(fail_save)

        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            await service._handle_received_torrent(object())

    async def test_remote_manifest_storage_failure_propagates(self):
        service = CometNetService(enabled=True)
        service.pool_store = Mock(
            accept_remote_manifest=AsyncMock(
                side_effect=OSError("manifest disk unavailable")
            )
        )
        message = PoolManifestMessage(
            sender_id="peer",
            signature="signature",
            pool_id="pool-a",
            display_name="Pool A",
            creator_key="creator-key",
            members=[
                {
                    "public_key": "creator-key",
                    "role": "creator",
                    "added_at": 1,
                    "added_by": "creator-key",
                }
            ],
            created_at=1,
            updated_at=1,
            manifest_signatures={"creator-key": "manifest-signature"},
        )

        with (
            patch(
                "comet.cometnet.manager.validate_message_security",
                new=AsyncMock(return_value=True),
            ),
            self.assertRaisesRegex(OSError, "manifest disk unavailable"),
        ):
            await service._handle_pool_manifest("peer", message)

    async def test_shutdown_continues_after_cleanup_failures(self):
        service = CometNetService(enabled=True)
        service._running = True

        class Component:
            def __init__(self, error=None):
                self.error = error
                self.stopped = 0

            async def stop(self):
                self.stopped += 1
                if self.error is not None:
                    raise self.error

            def to_dict(self):
                return {}

        class PoolStore:
            async def save(self):
                raise RuntimeError("pool save failed")

        class Upnp:
            def __init__(self):
                self.stopped = 0

            def stop(self):
                self.stopped += 1

        gossip = Component(RuntimeError("gossip stop failed"))
        discovery = Component()
        transport = Component()
        upnp = Upnp()
        service.pool_store = PoolStore()
        service.gossip = gossip
        service.discovery = discovery
        service.transport = transport
        service.upnp = upnp

        with (
            patch("comet.cometnet.manager.shutdown_crypto_executor") as shutdown_crypto,
            self.assertRaisesRegex(RuntimeError, "pool save failed"),
        ):
            await service.stop()

        self.assertEqual(gossip.stopped, 1)
        self.assertEqual(discovery.stopped, 1)
        self.assertEqual(transport.stopped, 1)
        self.assertEqual(upnp.stopped, 1)
        shutdown_crypto.assert_called_once_with()

    async def test_failed_start_cleans_every_initialized_component(self):
        service = CometNetService(enabled=True)

        class Component:
            def __init__(self):
                self.stopped = 0

            async def stop(self):
                self.stopped += 1

        class Upnp:
            def __init__(self):
                self.stopped = 0

            def stop(self):
                self.stopped += 1

        transport = Component()
        discovery = Component()
        gossip = Component()
        upnp = Upnp()

        async def fail_start():
            service.transport = transport
            service.discovery = discovery
            service.gossip = gossip
            service.upnp = upnp
            service._state_save_task = asyncio.create_task(asyncio.Event().wait())
            raise RuntimeError("startup failed")

        with (
            patch.object(service, "_start", new=fail_start),
            patch("comet.cometnet.manager.shutdown_crypto_executor") as shutdown_crypto,
            self.assertRaisesRegex(RuntimeError, "startup failed"),
        ):
            await service.start()

        self.assertEqual(transport.stopped, 1)
        self.assertEqual(discovery.stopped, 1)
        self.assertEqual(gossip.stopped, 1)
        self.assertEqual(upnp.stopped, 1)
        self.assertIsNone(service._state_save_task)
        shutdown_crypto.assert_called_once_with()

    async def test_background_tasks_are_cancelled_and_drained(self):
        service = CometNetService()
        service._running = True
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def worker():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        task = service._start_background_task(worker())
        await started.wait()

        service._running = False
        await service._stop_background_tasks()

        self.assertTrue(task.cancelled())
        self.assertTrue(cancelled.is_set())
        self.assertFalse(service._background_tasks)

    async def test_background_task_is_not_started_after_shutdown(self):
        service = CometNetService()
        ran = False

        async def worker():
            nonlocal ran
            ran = True

        task = service._start_background_task(worker())
        await asyncio.sleep(0)

        self.assertIsNone(task)
        self.assertFalse(ran)
