import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from pydantic import ValidationError

from comet.cometnet.gossip import GossipEngine
from comet.cometnet.protocol import TorrentAnnounce, TorrentMetadata
from comet.cometnet.reputation import ReputationStore


def _engine_with_queue(*items):
    engine = GossipEngine(object(), None)
    engine._outgoing_queue.extend(items)
    engine._get_random_peers = lambda *args: []
    engine._send_message = object()
    engine._running = True
    engine.gossip_interval = 0
    return engine


class CometNetGossipTests(unittest.IsolatedAsyncioTestCase):
    def test_signed_protocol_rejects_coerced_and_non_finite_numbers(self):
        base = {
            "info_hash": "a" * 40,
            "title": "Title",
            "size": 1,
            "tracker": "peer",
            "imdb_id": "tt123",
        }

        for override in (
            {"size": True},
            {"size": 0},
            {"seeders": False},
            {"file_index": -1},
            {"updated_at": float("nan")},
            {"updated_at": float("inf")},
        ):
            with self.subTest(override=override):
                with self.assertRaises(ValidationError):
                    TorrentMetadata(**(base | override))

        with self.assertRaisesRegex(ValidationError, "finite number"):
            TorrentAnnounce(timestamp=float("nan"))

    async def test_existing_hash_is_not_repropagated_without_verification(self):
        info_hash = "a" * 40
        engine = GossipEngine(object(), ReputationStore())

        async def existing(hashes):
            self.assertEqual(hashes, [info_hash])
            return {info_hash}

        engine._check_torrents_exist = existing
        announce = TorrentAnnounce(
            sender_id="sender",
            torrents=[
                TorrentMetadata(
                    info_hash=info_hash,
                    title="Unverified metadata",
                    size=1,
                    tracker="peer",
                    imdb_id="tt123",
                )
            ],
        )

        with (
            patch(
                "comet.cometnet.gossip.validate_message_security",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                engine, "_repropagate", new=AsyncMock(return_value=1)
            ) as repropagate,
        ):
            await engine.handle_announce("sender", announce)

        repropagate.assert_not_awaited()
        self.assertEqual(engine.stats["validation_skipped_exists"], 1)
        self.assertEqual(engine.stats["duplicates_ignored"], 1)

    async def test_valid_torrent_key_does_not_gain_handshake_authority(self):
        keystore = Mock()
        engine = GossipEngine(object(), ReputationStore(), keystore=keystore)
        torrent = TorrentMetadata(
            info_hash="a" * 40,
            title="Title",
            size=1,
            tracker="peer",
            imdb_id="tt123",
            contributor_id="contributor-id",
            contributor_public_key="public-key",
            contributor_signature="signature",
        )
        announce = TorrentAnnounce(sender_id="sender", torrents=[torrent])

        with (
            patch(
                "comet.cometnet.gossip.validate_message_security",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "comet.cometnet.gossip.verify_torrent_signature_sync",
                return_value=True,
            ),
        ):
            await engine.handle_announce("sender", announce)

        keystore.store_key.assert_called_once_with(
            node_id="contributor-id",
            public_key_hex="public-key",
        )
        keystore.store_verified_key.assert_not_called()

    async def test_stop_cancels_both_workers_and_clears_references(self):
        engine = GossipEngine(object(), None)
        engine._running = True
        engine._gossip_task = asyncio.create_task(asyncio.Event().wait())
        engine._cleanup_task = asyncio.create_task(asyncio.Event().wait())
        gossip_task = engine._gossip_task
        cleanup_task = engine._cleanup_task

        await engine.stop()

        self.assertTrue(gossip_task.cancelled())
        self.assertTrue(cleanup_task.cancelled())
        self.assertIsNone(engine._gossip_task)
        self.assertIsNone(engine._cleanup_task)

    async def test_batch_is_requeued_when_no_peer_is_reached(self):
        engine = _engine_with_queue("first", "second")

        async def no_peer(batch, ttl):
            del batch, ttl
            engine._running = False
            return 0

        with patch.object(engine, "_repropagate", new=no_peer):
            await engine._gossip_loop()

        self.assertEqual(list(engine._outgoing_queue), ["first", "second"])

    async def test_batch_is_requeued_when_propagation_fails(self):
        engine = _engine_with_queue("first", "second")

        async def fail(batch, ttl):
            del batch, ttl
            engine._running = False
            raise RuntimeError("signing failed")

        with patch.object(engine, "_repropagate", new=fail):
            await engine._gossip_loop()

        self.assertEqual(list(engine._outgoing_queue), ["first", "second"])

    async def test_batch_is_requeued_when_shutdown_cancels_propagation(self):
        engine = _engine_with_queue("first", "second")
        started = asyncio.Event()

        async def block(batch, ttl):
            del batch, ttl
            started.set()
            await asyncio.Event().wait()

        with patch.object(engine, "_repropagate", new=block):
            gossip_loop = asyncio.create_task(engine._gossip_loop())
            await started.wait()
            gossip_loop.cancel()
            await gossip_loop

        self.assertEqual(list(engine._outgoing_queue), ["first", "second"])
