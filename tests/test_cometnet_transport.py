import asyncio
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from comet.cometnet.protocol import HandshakeMessage, PingMessage
from comet.cometnet.transport import ConnectionManager, NodeIdentity, PeerConnection


class _Identity:
    node_id = "local"
    public_key_hex = "local-key"

    async def sign_hex_async(self, payload):
        del payload
        return "signature"


class _Peer:
    def __init__(self):
        self.node_id = "peer"
        self.last_activity = time.time()
        self.pending_pings = {}
        self.latency_samples = []
        self.latency_ms = 0
        self.send_started = asyncio.Event()
        self.send_cancelled = asyncio.Event()

    async def send(self, message):
        del message
        self.send_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.send_cancelled.set()


class CometNetTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_unexpected_server_start_failure_is_visible(self):
        manager = ConnectionManager(_Identity())

        with (
            patch(
                "comet.cometnet.transport.websockets.serve",
                new=AsyncMock(side_effect=RuntimeError("server bug")),
            ),
            self.assertRaisesRegex(RuntimeError, "server bug"),
        ):
            await manager.start()

        self.assertFalse(manager._running)
        self.assertEqual(manager._tasks, set())

    async def test_server_listens_on_all_available_address_families(self):
        manager = ConnectionManager(_Identity())
        server = Mock(wait_closed=AsyncMock())

        with patch(
            "comet.cometnet.transport.websockets.serve",
            new=AsyncMock(return_value=server),
        ) as serve:
            await manager.start()
            await manager.stop()

        self.assertIsNone(serve.await_args.kwargs["host"])
        self.assertEqual(serve.await_args.kwargs["port"], manager.listen_port)
        server.close.assert_called_once_with()
        server.wait_closed.assert_awaited_once_with()

    async def test_native_ipv6_peer_address_is_a_valid_websocket_url(self):
        manager = ConnectionManager(_Identity())
        websocket = AsyncMock()
        websocket.real_client_ip = None
        websocket.remote_address = ("2001:db8::1", 49152, 0, 0)

        with patch.object(
            manager,
            "handle_incoming_connection",
            new=AsyncMock(return_value=None),
        ) as handle:
            await manager._handle_ws_connection(websocket)

        handle.assert_awaited_once_with(
            websocket,
            "2001:db8::1",
            "ws://[2001:db8::1]:49152",
        )

    async def test_inbound_ipv6_fallback_uses_advertised_listen_port(self):
        manager = ConnectionManager(_Identity())
        manager._running = True
        handshake = HandshakeMessage(
            sender_id="peer",
            public_key="peer-key",
            signature="signature",
            listen_port=8765,
        )
        websocket = AsyncMock()
        websocket.recv.return_value = handshake.to_bytes()

        with (
            patch.object(
                NodeIdentity,
                "verify_hex_async",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                NodeIdentity,
                "node_id_from_public_key",
                return_value="peer",
            ),
        ):
            node_id = await manager.handle_incoming_connection(
                websocket,
                "2001:db8::1",
                "ws://[2001:db8::1]:49152",
            )

        self.assertEqual(node_id, "peer")
        self.assertEqual(
            manager.get_peer_address("peer"),
            "ws://[2001:db8::1]:8765",
        )
        await manager.stop()

    async def test_equivalent_ipv6_spellings_share_connection_limit(self):
        manager = ConnectionManager(_Identity())
        manager._running = True
        manager.max_connections_per_ip = 1
        manager._connections_per_ip["2606:4700:4700::1111"] = 1
        websocket = AsyncMock()

        node_id = await manager.handle_incoming_connection(
            websocket,
            "2606:4700:4700:0:0:0:0:1111",
        )

        self.assertIsNone(node_id)
        websocket.close.assert_awaited_once_with()

    def test_get_peer_address_preserves_authoritative_public_url(self):
        manager = ConnectionManager(_Identity())
        manager._connections["peer"] = PeerConnection(
            node_id="peer",
            address="wss://[2001:db8::1]/cometnet/ws",
            websocket=object(),
        )

        self.assertEqual(
            manager.get_peer_address("peer"),
            "wss://[2001:db8::1]/cometnet/ws",
        )

    async def test_connection_traffic_totals_survive_peer_disconnect(self):
        manager = ConnectionManager(_Identity())
        websocket = AsyncMock()
        connection = PeerConnection(
            node_id="peer",
            address="wss://peer",
            websocket=websocket,
        )
        manager._connections["peer"] = connection

        self.assertTrue(await connection.send(b"four"))
        connection.bytes_received = 6
        connection.messages_received = 2
        self.assertTrue(manager._release_connection("peer", connection))

        stats = manager.get_connection_stats()
        self.assertEqual(stats["bytes_sent"], 4)
        self.assertEqual(stats["bytes_received"], 6)
        self.assertEqual(stats["messages_sent"], 1)
        self.assertEqual(stats["messages_received"], 2)

    async def test_outbound_handshake_reserves_global_capacity(self):
        manager = ConnectionManager(_Identity(), max_peers=1)
        manager._running = True
        connect_started = asyncio.Event()
        release_connect = asyncio.Event()
        websocket = AsyncMock()

        async def connect(*args, **kwargs):
            del args, kwargs
            connect_started.set()
            await release_connect.wait()
            return websocket

        with (
            patch("comet.cometnet.transport.websockets.connect", new=connect),
            patch.object(
                manager, "_perform_handshake", new=AsyncMock(return_value="peer")
            ) as handshake,
        ):
            first = asyncio.create_task(manager.connect_to_peer("wss://one"))
            await connect_started.wait()
            second = await manager.connect_to_peer("wss://two")
            release_connect.set()
            self.assertEqual(await first, "peer")

        self.assertIsNone(second)
        handshake.assert_awaited_once()
        self.assertEqual(manager._pending_connections, 0)

    async def test_inbound_handshake_reserves_global_capacity(self):
        manager = ConnectionManager(_Identity(), max_peers=1)
        manager._running = True
        handshake_started = asyncio.Event()
        release_handshake = asyncio.Event()
        first_socket = AsyncMock()
        second_socket = AsyncMock()

        async def handshake(*args, **kwargs):
            del args, kwargs
            handshake_started.set()
            await release_handshake.wait()
            return "peer"

        with patch.object(manager, "_perform_handshake", new=handshake):
            first = asyncio.create_task(
                manager.handle_incoming_connection(first_socket, "203.0.113.1")
            )
            await handshake_started.wait()
            second = await manager.handle_incoming_connection(
                second_socket, "203.0.113.2"
            )
            release_handshake.set()
            self.assertEqual(await first, "peer")

        self.assertIsNone(second)
        second_socket.close.assert_awaited_once()
        self.assertEqual(manager._pending_connections, 0)

    async def test_cancelled_inbound_handshake_releases_every_reservation(self):
        manager = ConnectionManager(_Identity(), max_peers=1)
        manager._running = True
        websocket = AsyncMock()
        started = asyncio.Event()

        async def handshake(*args, **kwargs):
            del args, kwargs
            started.set()
            await asyncio.Event().wait()

        with patch.object(manager, "_perform_handshake", new=handshake):
            task = asyncio.create_task(
                manager.handle_incoming_connection(websocket, "203.0.113.1")
            )
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(manager._pending_connections, 0)
        self.assertEqual(manager._connections_per_ip, {})
        websocket.close.assert_awaited_once()

    async def test_cancelled_outbound_handshake_closes_and_releases(self):
        manager = ConnectionManager(_Identity(), max_peers=1)
        manager._running = True
        websocket = AsyncMock()
        started = asyncio.Event()

        async def connect(*args, **kwargs):
            del args, kwargs
            return websocket

        async def handshake(*args, **kwargs):
            del args, kwargs
            started.set()
            await asyncio.Event().wait()

        with (
            patch("comet.cometnet.transport.websockets.connect", new=connect),
            patch.object(manager, "_perform_handshake", new=handshake),
        ):
            task = asyncio.create_task(manager.connect_to_peer("wss://peer"))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(manager._pending_connections, 0)
        self.assertEqual(manager._connecting, set())
        websocket.close.assert_awaited_once()

    async def test_unexpected_connect_failure_propagates_after_rollback(self):
        manager = ConnectionManager(_Identity(), max_peers=1)
        manager._running = True

        with (
            patch(
                "comet.cometnet.transport.websockets.connect",
                side_effect=RuntimeError("connector bug"),
            ),
            self.assertRaisesRegex(RuntimeError, "connector bug"),
        ):
            await manager.connect_to_peer("wss://peer")

        self.assertEqual(manager._pending_connections, 0)
        self.assertEqual(manager._connecting, set())

    async def test_unexpected_handshake_failure_propagates_and_closes(self):
        manager = ConnectionManager(_Identity(), max_peers=1)
        manager._running = True
        websocket = AsyncMock()

        async def connect(*args, **kwargs):
            del args, kwargs
            return websocket

        with (
            patch("comet.cometnet.transport.websockets.connect", new=connect),
            patch.object(
                manager.identity,
                "sign_hex_async",
                new=AsyncMock(side_effect=RuntimeError("signing failed")),
            ),
            self.assertRaisesRegex(RuntimeError, "signing failed"),
        ):
            await manager.connect_to_peer("wss://peer")

        self.assertEqual(manager._pending_connections, 0)
        self.assertEqual(manager._connecting, set())
        websocket.close.assert_awaited_once()

    async def test_receive_loop_rejects_unauthenticated_control_message(self):
        manager = ConnectionManager(_Identity())
        manager._running = True
        ping = PingMessage(sender_id="peer", nonce="nonce", signature="00")

        class WebSocket:
            calls = 0

            async def recv(self):
                self.calls += 1
                if self.calls == 1:
                    return ping.to_bytes()
                raise RuntimeError("stop receive loop")

        connection = PeerConnection(
            node_id="peer",
            address="ws://peer",
            websocket=WebSocket(),
        )
        manager._connections["peer"] = connection

        with (
            patch(
                "comet.cometnet.transport.validate_message_security",
                new=AsyncMock(return_value=False),
            ) as validate,
            patch.object(manager, "_handle_ping", new=AsyncMock()) as handle_ping,
        ):
            await manager._receive_loop(connection)

        validate.assert_awaited_once_with(ping, "peer", None, None)
        handle_ping.assert_not_awaited()

    async def test_duplicate_inbound_handshake_releases_reserved_ip_slot(self):
        manager = ConnectionManager(_Identity())
        manager._connections["peer"] = object()
        manager._connections_per_ip["203.0.113.1"] = 1
        handshake = HandshakeMessage(
            sender_id="peer",
            public_key="peer-key",
            signature="signature",
        )

        class WebSocket:
            closed = False

            async def recv(self):
                return handshake.to_bytes()

            async def send(self, payload):
                del payload

            async def close(self):
                self.closed = True

        websocket = WebSocket()
        with (
            patch.object(
                NodeIdentity,
                "verify_hex_async",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                NodeIdentity,
                "node_id_from_public_key",
                return_value="peer",
            ),
        ):
            node_id = await manager.handle_incoming_connection(
                websocket,
                "203.0.113.1",
            )

        self.assertIsNone(node_id)
        self.assertTrue(websocket.closed)
        self.assertEqual(manager._connections_per_ip["203.0.113.1"], 1)

    async def test_disconnect_tolerates_receive_loop_removing_connection(self):
        manager = ConnectionManager(_Identity())

        class Connection:
            async def close(inner_self):
                del inner_self
                manager._connections.pop("peer", None)

        manager._connections["peer"] = Connection()

        await manager.disconnect_peer("peer")

        self.assertNotIn("peer", manager._connections)

    async def test_disconnect_releases_inbound_ip_slot(self):
        manager = ConnectionManager(_Identity())

        class WebSocket:
            async def close(inner_self):
                del inner_self

        connection = PeerConnection(
            node_id="peer",
            address="ws://peer",
            websocket=WebSocket(),
            client_ip="203.0.113.5",
            is_outbound=False,
        )
        manager._connections["peer"] = connection
        manager._connections_per_ip["203.0.113.5"] = 1

        await manager.disconnect_peer("peer")

        self.assertNotIn("peer", manager._connections)
        self.assertNotIn("203.0.113.5", manager._connections_per_ip)

    async def test_old_receive_loop_does_not_remove_replacement_connection(self):
        manager = ConnectionManager(_Identity())
        old_connection = PeerConnection(
            node_id="peer",
            address="ws://old",
            websocket=object(),
        )
        replacement = object()
        manager._connections["peer"] = replacement

        await manager._receive_loop(old_connection)

        self.assertIs(manager._connections["peer"], replacement)

    async def test_ping_loop_owns_and_cancels_send_operations(self):
        manager = ConnectionManager(_Identity())
        peer = _Peer()
        manager._connections[peer.node_id] = peer
        manager._running = True

        with patch(
            "comet.cometnet.transport.settings.COMETNET_TRANSPORT_PING_INTERVAL",
            0,
        ):
            ping_loop = asyncio.create_task(manager._ping_loop())
            await peer.send_started.wait()
            ping_loop.cancel()
            await ping_loop

        self.assertTrue(peer.send_cancelled.is_set())
