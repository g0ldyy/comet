import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from comet.cometnet.discovery import DiscoveryService
from comet.cometnet.protocol import PeerInfo, PeerResponse


class CometNetDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def test_constructor_rejects_falsey_or_inconsistent_configuration(self):
        malformed = [
            {"manual_peers": False},
            {"manual_peers": ["peer.example"]},
            {"bootstrap_nodes": ["wss://peer", "wss://peer"]},
            {"min_peers": True},
            {"max_peers": 0},
            {"min_peers": 3, "max_peers": 2},
        ]
        for arguments in malformed:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    DiscoveryService(**arguments)

    async def test_persisted_discovery_rejects_invalid_peer_atomically(self):
        service = DiscoveryService()
        service._add_known_peer("wss://existing.example", "existing", "manual")
        original = service.to_dict()
        candidate = {
            "known_peers": [
                {
                    "address": "wss://valid.example",
                    "node_id": "valid",
                    "source": "pex",
                    "last_seen": 1,
                },
                {
                    "address": "invalid://peer",
                    "node_id": "invalid",
                    "source": "pex",
                    "last_seen": 2,
                },
            ]
        }

        with patch(
            "comet.cometnet.discovery.is_valid_peer_address",
            new=AsyncMock(side_effect=[True, False]),
        ):
            with self.assertRaisesRegex(ValueError, "address is invalid"):
                await service.from_dict(candidate)

        self.assertEqual(service.to_dict(), original)

    async def test_peer_response_counts_only_new_valid_peers(self):
        service = DiscoveryService()
        service._node_id = "self"
        response = PeerResponse(
            peers=[
                PeerInfo(node_id="valid", address="wss://valid.example"),
                PeerInfo(node_id="invalid", address="invalid://peer"),
                PeerInfo(node_id="self", address="wss://self.example"),
            ]
        )

        with patch(
            "comet.cometnet.discovery.is_valid_peer_address",
            new=AsyncMock(side_effect=[True, False]),
        ):
            added = await service.handle_peer_response(response)

        self.assertEqual(added, 1)
        self.assertEqual(set(service._known_peers), {"wss://valid.example"})

    async def test_pex_limit_requires_a_positive_exact_integer(self):
        service = DiscoveryService()
        for max_peers in (True, 0, -1, 1.5):
            with self.subTest(max_peers=max_peers):
                with self.assertRaises(ValueError):
                    await service.get_peers_for_pex(max_peers)

    async def test_stop_clears_cancelled_worker_reference(self):
        service = DiscoveryService()
        service._running = True
        service._discovery_task = asyncio.create_task(asyncio.Event().wait())
        task = service._discovery_task

        await service.stop()

        self.assertTrue(task.cancelled())
        self.assertIsNone(service._discovery_task)
