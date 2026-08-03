"""
CometNet Transport Module

Manages WebSocket connections for peer-to-peer communication.
Handles both server-side (incoming) and client-side (outgoing) connections.
"""

import asyncio
import hashlib
import hmac
import ipaddress
import logging
import random
import secrets
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import websockets
from websockets.client import WebSocketClientProtocol
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException
from websockets.http11 import Response

from comet.cometnet.crypto import NodeIdentity
from comet.cometnet.protocol import (
    AnyMessage,
    HandshakeMessage,
    MessageType,
    PingMessage,
    PongMessage,
    parse_message,
)
from comet.cometnet.utils import (
    extract_ip_from_address,
    format_websocket_url,
    get_websocket_compression,
    is_valid_peer_address,
    replace_websocket_url_port,
)
from comet.cometnet.validation import validate_message_security
from comet.core.models import settings
from comet.observability.context import create_detached_task
from comet.utils.network import extract_ip_from_headers


class WebSocketHeadFilter(logging.Filter):
    """Filter out noise errors from websockets (health checks, port scanners)."""

    def filter(self, record):
        if record.exc_info:
            _, exc_value, _ = record.exc_info
            current = exc_value
            while current:
                msg = str(current)
                if (
                    ("unsupported HTTP method" in msg and "HEAD" in msg)
                    or "did not receive a valid HTTP request" in msg
                    or "connection closed while reading HTTP request line" in msg
                    or "line without CRLF" in msg
                ):
                    return False
                current = getattr(current, "__cause__", None)
        return True


# Apply filter to websockets logger
logging.getLogger("websockets.server").addFilter(WebSocketHeadFilter())


async def resolve_effective_peer_address(
    public_url: str | None,
    connectable_address: str | None,
    allow_private: bool,
) -> str:
    """
    Pick the best address to associate with a peer connection.

    A peer-supplied public URL is only trusted if it is a syntactically valid
    WebSocket address and matches the current private-network policy.
    """
    if public_url:
        normalized_public_url = public_url.strip()
        if await is_valid_peer_address(
            normalized_public_url, allow_private=allow_private
        ):
            return normalized_public_url

    return connectable_address or ""


@dataclass
class PeerConnection:
    """Represents an active connection to a peer."""

    node_id: str
    address: str  # WebSocket URL
    websocket: WebSocketClientProtocol
    client_ip: str | None = None  # The actual IP address (for rate limiting)
    alias: str | None = None  # Friendly name
    public_key: str = ""
    connected_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    is_outbound: bool = True  # True if we initiated the connection
    pending_pings: dict[str, float] = field(default_factory=dict)  # nonce -> sent_time
    latency_ms: float = 0.0
    latency_samples: deque = field(
        default_factory=lambda: deque(maxlen=10)
    )  # Rolling window of latency samples
    bytes_sent: int = 0
    bytes_received: int = 0
    messages_sent: int = 0
    messages_received: int = 0

    # Rate limiting
    rate_limit_history: deque = field(default_factory=deque)

    def check_rate_limit(self, max_count: int, window: float) -> bool:
        """
        Check if the peer has exceeded the rate limit.
        Returns True if allowed, False if limited.
        """
        if not settings.COMETNET_TRANSPORT_RATE_LIMIT_ENABLED:
            return True

        now = time.time()

        # Remove old entries
        while self.rate_limit_history and now - self.rate_limit_history[0] > window:
            self.rate_limit_history.popleft()

        if len(self.rate_limit_history) >= max_count:
            return False

        self.rate_limit_history.append(now)
        return True

    def update_activity(self) -> None:
        """Update the last activity timestamp."""
        self.last_activity = time.time()

    async def send(self, message: AnyMessage | bytes) -> bool:
        """Send a message to this peer. Returns True on success."""
        try:
            if isinstance(message, bytes):
                data = message
            else:
                data = message.to_bytes()

            await self.websocket.send(data)
            self.bytes_sent += len(data)
            self.messages_sent += 1
            self.update_activity()
            return True
        except ConnectionClosed:
            return False
        except Exception:
            return False

    async def close(self) -> None:
        """Close the connection."""
        try:
            await self.websocket.close()
        except Exception:
            pass


MessageHandler = Callable[[str, AnyMessage], Awaitable[None]]


def compute_network_token(
    network_id: str, network_password: str, sender_id: str, timestamp: float
) -> str:
    """
    Compute HMAC token for private network authentication.

    Token = HMAC-SHA256(password, network_id:sender_id:window)
    Timestamp is rounded to 5-minute windows to allow clock drift.
    """
    window = int(timestamp // 300) * 300
    message = f"{network_id}:{sender_id}:{window}".encode()
    return hmac.new(network_password.encode(), message, hashlib.sha256).hexdigest()


class ConnectionManager:
    """
    Manages all WebSocket connections to peers.

    Responsibilities:
    - Track active connections
    - Handle connection lifecycle (connect, disconnect)
    - Route incoming messages to handlers
    - Periodic ping/pong for health checks
    """

    _HEALTH_PATHS = frozenset(
        {
            "",
            "/cometnet",
        }
    )
    _UPGRADE_REQUIRED_HEADERS = (
        ("Content-Type", "text/plain"),
        ("Upgrade", "websocket"),
    )

    # Security limits
    def __init__(
        self,
        identity: NodeIdentity,
        listen_port: int = 8765,
        max_peers: int = 50,
        advertise_url: str | None = None,
        keystore=None,  # Optional PublicKeyStore for storing peer keys
    ):
        self.identity = identity
        self.listen_port = listen_port
        self.max_peers = max_peers
        self.advertise_url = advertise_url
        self._keystore = keystore

        # Security limits from settings
        self.max_message_size = settings.COMETNET_TRANSPORT_MAX_MESSAGE_SIZE
        self.max_connections_per_ip = settings.COMETNET_TRANSPORT_MAX_CONNECTIONS_PER_IP
        self.websocket_compression = get_websocket_compression()

        # Rate limits
        self.rate_limit_count = settings.COMETNET_TRANSPORT_RATE_LIMIT_COUNT
        self.rate_limit_window = settings.COMETNET_TRANSPORT_RATE_LIMIT_WINDOW

        # Active connections by node_id
        self._connections: dict[str, PeerConnection] = {}
        self._closed_bytes_sent = 0
        self._closed_bytes_received = 0
        self._closed_messages_sent = 0
        self._closed_messages_received = 0

        # Track connections per IP to prevent abuse
        self._connections_per_ip: dict[str, int] = {}
        self._pending_connections = 0

        # Lock for connection operations to prevent race conditions
        self._connection_lock = asyncio.Lock()

        # Addresses we're currently trying to connect to (to prevent duplicates)
        self._connecting: set[str] = set()

        # Message handlers by message type
        self._handlers: dict[MessageType, MessageHandler] = {}

        # Server task
        self._server = None
        self._server_task: asyncio.Task | None = None

        # Background tasks
        self._tasks: set[asyncio.Task] = set()

        # Running flag
        self._running = False

        # Private network settings
        self._private_network = settings.COMETNET_PRIVATE_NETWORK
        self._network_id = settings.COMETNET_NETWORK_ID or ""
        self._network_password = settings.COMETNET_NETWORK_PASSWORD or ""

        # Callback when a peer connects (for Discovery notification)
        self._on_peer_connected: Callable[[str, str], Awaitable[None]] | None = None

    @staticmethod
    def _header_tokens(headers: websockets.Headers, name: str) -> set[str]:
        return {
            token.strip().lower()
            for value in headers.get_all(name)
            for token in value.split(",")
            if token.strip()
        }

    @classmethod
    def _upgrade_required_response(cls, body: bytes) -> Response:
        return Response(
            426,
            "Upgrade Required",
            websockets.Headers(cls._UPGRADE_REQUIRED_HEADERS),
            body,
        )

    def _process_request(self, connection, request):
        """
        Handle non-WebSocket HTTP requests gracefully.

        This is called for every incoming connection. If the request is not a valid
        WebSocket upgrade request (e.g., health checks, load balancer probes),
        we return an appropriate HTTP response instead of raising an error.
        """
        real_ip = extract_ip_from_headers(dict(request.headers), require_public=False)
        if real_ip:
            connection.real_client_ip = real_ip

        # Check if this is a WebSocket upgrade request
        connection_tokens = self._header_tokens(request.headers, "Connection")
        upgrade_tokens = self._header_tokens(request.headers, "Upgrade")
        ws_key = request.headers.get("Sec-WebSocket-Key", "")
        is_ws_upgrade = "upgrade" in connection_tokens and "websocket" in upgrade_tokens

        if not is_ws_upgrade:
            path = request.path or "/"
            path_lower = path.lower().rstrip("/")

            if ws_key:
                return self._upgrade_required_response(
                    (
                        b"WebSocket handshake reached CometNet without required "
                        b"upgrade headers. Ensure your reverse proxy forwards or "
                        b"recreates 'Upgrade: websocket' and 'Connection: Upgrade' "
                        b"to the origin.\n"
                    ),
                )

            is_health_check = path_lower in self._HEALTH_PATHS or path_lower.endswith(
                ("/health", "/healthz")
            )

            if is_health_check:
                return Response(
                    200,
                    "OK",
                    websockets.Headers([("Content-Type", "text/plain")]),
                    b"CometNet WebSocket Server\n",
                )
            else:
                # Other requests - return 426 Upgrade Required
                return self._upgrade_required_response(
                    b"This is a WebSocket endpoint. Use a WebSocket client.\n",
                )

        # Valid WebSocket request - continue with normal handshake
        return None

    def set_on_peer_connected(
        self, callback: Callable[[str, str], Awaitable[None]]
    ) -> None:
        """Set callback to be called when a peer connects. Args: (node_id, address)"""
        self._on_peer_connected = callback

    @property
    def connected_peer_count(self) -> int:
        """Return the number of connected peers."""
        return len(self._connections)

    @property
    def connected_node_ids(self) -> list[str]:
        """Return list of connected node IDs."""
        return list(self._connections.keys())

    def get_peer_address(self, node_id: str) -> str | None:
        """Get the address (IP:port) of a connected peer."""
        conn = self._connections.get(node_id)
        if not conn:
            return None
        return conn.address

    def register_handler(self, msg_type: MessageType, handler: MessageHandler) -> None:
        """Register a handler for a specific message type."""
        self._handlers[msg_type] = handler

    async def start(self) -> None:
        """Start the connection manager and WebSocket server."""
        if self._running:
            return

        self._running = True

        # Start WebSocket server
        try:
            self._server = await websockets.serve(
                self._handle_ws_connection,
                host=None,
                port=self.listen_port,
                ping_interval=None,
                ping_timeout=None,
                max_size=self.max_message_size,
                process_request=self._process_request,
                compression=self.websocket_compression,
            )
        except OSError:
            pass
            # Continue anyway - we can still make outbound connections
        except BaseException:
            self._running = False
            raise

        # Start ping task
        ping_task = create_detached_task(
            self._ping_loop(),
            name="cometnet-transport-ping",
        )
        self._tasks.add(ping_task)

    async def _handle_ws_connection(self, websocket, path: str = "") -> None:
        """
        Handle incoming WebSocket connection from the native server.
        """
        real_ip = getattr(websocket, "real_client_ip", None)
        connectable_address: str | None = None

        if real_ip:
            client_ip = real_ip
        else:
            remote = websocket.remote_address
            if remote:
                client_ip = remote[0]
                connectable_address = format_websocket_url(remote[0], remote[1])
            else:
                client_ip = "unknown"

        node_id = await self.handle_incoming_connection(
            websocket, client_ip, connectable_address
        )

        if node_id:
            # Notify Discovery of the new connection (only if we have a valid address)
            if self._on_peer_connected:
                corrected_address = (
                    self.get_peer_address(node_id) or connectable_address
                )
                if corrected_address:
                    await self._on_peer_connected(node_id, corrected_address)

            # Keep connection alive until it's closed
            try:
                await websocket.wait_closed()
            except Exception:
                pass

    async def stop(self) -> None:
        """Stop the connection manager and close all connections."""
        self._running = False

        # Stop WebSocket server
        if self._server:
            self._server.close()
            await self._server.wait_closed()

        # Cancel background tasks
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

        # Close all connections
        for conn in list(self._connections.values()):
            await conn.close()
        self._connections.clear()
        self._connections_per_ip.clear()

    async def connect_to_peer(self, address: str) -> str | None:
        """
        Connect to a peer at the given address.

        Returns the peer's node_id on success, None on failure.
        """
        if not self._running:
            return None

        # Check if we're already connected or connecting
        if address in self._connecting:
            return None

        # Use lock to prevent race condition on peer limit check
        async with self._connection_lock:
            # Check peer limit
            if len(self._connections) + self._pending_connections >= self.max_peers:
                return None

            self._connecting.add(address)
            self._pending_connections += 1

        websocket = None
        node_id = None
        try:
            websocket = await asyncio.wait_for(
                websockets.connect(
                    address,
                    ping_interval=None,
                    ping_timeout=None,
                    max_size=self.max_message_size,
                    compression=self.websocket_compression,
                ),
                timeout=5.0,
            )

            # Perform handshake
            # For outbound connections, address is both the client_ip (for logging) and connectable_address
            node_id = await self._perform_handshake(
                websocket, address, address, is_outbound=True
            )

            if node_id:
                return node_id
            else:
                return None
        except TimeoutError:
            return None
        except InvalidStatus:
            return None
        except (WebSocketException, ConnectionClosed):
            return None
        except asyncio.CancelledError:
            raise
        except OSError:
            return None
        finally:
            async with self._connection_lock:
                self._connecting.discard(address)
                self._pending_connections -= 1
            if node_id is None and websocket is not None:
                await websocket.close()

    async def handle_incoming_connection(
        self,
        websocket: WebSocketClientProtocol,
        client_ip: str,
        connectable_address: str | None = None,
    ) -> str | None:
        """
        Handle an incoming WebSocket connection.

        Args:
            websocket: The WebSocket connection
            client_ip: The client's IP address (for rate limiting and logging)
            connectable_address: Optional real address we can reconnect to (e.g. ws://ip:port).
                                 If not provided, we rely on the peer's public_url from handshake.

        Returns the peer's node_id on success, None on failure.
        """
        if not self._running:
            await websocket.close()
            return None

        ip = client_ip
        parsed_ip = None
        try:
            parsed_ip = ipaddress.ip_address(ip)
            if isinstance(parsed_ip, ipaddress.IPv6Address) and parsed_ip.ipv4_mapped:
                parsed_ip = parsed_ip.ipv4_mapped
            ip = str(parsed_ip)
        except ValueError:
            pass

        # Use lock to prevent race condition on connection limits
        async with self._connection_lock:
            # Check per-IP connection limit (prevent Sybil-like attacks)
            # Relax limit for private IPs (local network, Docker)
            limit = self.max_connections_per_ip
            if parsed_ip is not None and parsed_ip.is_private:
                limit = max(limit, 50)  # Allow more connections from private IPs

            current_ip_connections = self._connections_per_ip.get(ip, 0)
            if current_ip_connections >= limit:
                await websocket.close()
                return None

            # Check peer limit
            if len(self._connections) + self._pending_connections >= self.max_peers:
                await websocket.close()
                return None

            # Pre-increment IP counter to reserve slot (will decrement if handshake fails)
            self._connections_per_ip[ip] = current_ip_connections + 1
            self._pending_connections += 1

        node_id = None
        try:
            # Perform handshake (we wait for their handshake first)
            node_id = await self._perform_handshake(
                websocket, ip, connectable_address, is_outbound=False
            )
        finally:
            async with self._connection_lock:
                self._pending_connections -= 1
                if node_id is None:
                    self._release_ip_slot(ip)
            if node_id is None:
                await websocket.close()

        if node_id:
            return node_id
        else:
            return None

    async def _perform_handshake(
        self,
        websocket: WebSocketClientProtocol,
        client_ip: str,
        connectable_address: str | None,
        is_outbound: bool,
    ) -> str | None:
        """
        Perform the handshake protocol with a peer.

        Args:
            websocket: The WebSocket connection
            client_ip: The client's IP address (for logging)
            connectable_address: Optional real address we can reconnect to
            is_outbound: True if we initiated the connection

        Returns the peer's node_id on success, None on failure.
        """
        try:
            if is_outbound:
                # We initiated, so we send our handshake first
                handshake = HandshakeMessage(
                    sender_id=self.identity.node_id,
                    public_key=self.identity.public_key_hex,
                    listen_port=self.listen_port,
                    public_url=self.advertise_url,
                    alias=settings.COMETNET_NODE_ALIAS,
                )
                # Add network token for private mode
                if (
                    self._private_network
                    and self._network_id
                    and self._network_password
                ):
                    handshake.network_token = compute_network_token(
                        self._network_id,
                        self._network_password,
                        self.identity.node_id,
                        handshake.timestamp,
                    )
                handshake.signature = await self.identity.sign_hex_async(
                    handshake.to_signable_bytes()
                )
                await websocket.send(handshake.to_bytes())

                # Wait for their handshake
                response = await asyncio.wait_for(websocket.recv(), timeout=10.0)
                peer_handshake = parse_message(response)
            else:
                # They initiated, so we wait for their handshake first
                response = await asyncio.wait_for(websocket.recv(), timeout=10.0)
                peer_handshake = parse_message(response)

                if not isinstance(peer_handshake, HandshakeMessage):
                    return None

                # Send our handshake
                handshake = HandshakeMessage(
                    sender_id=self.identity.node_id,
                    public_key=self.identity.public_key_hex,
                    listen_port=self.listen_port,
                    public_url=self.advertise_url,
                    alias=settings.COMETNET_NODE_ALIAS,
                )
                # Add network token for private mode
                if (
                    self._private_network
                    and self._network_id
                    and self._network_password
                ):
                    handshake.network_token = compute_network_token(
                        self._network_id,
                        self._network_password,
                        self.identity.node_id,
                        handshake.timestamp,
                    )
                handshake.signature = await self.identity.sign_hex_async(
                    handshake.to_signable_bytes()
                )

                await websocket.send(handshake.to_bytes())

            # Validate peer handshake
            if not isinstance(peer_handshake, HandshakeMessage):
                return None

            # Verify signature
            if not await NodeIdentity.verify_hex_async(
                peer_handshake.to_signable_bytes(),
                peer_handshake.signature,
                peer_handshake.public_key,
            ):
                return None

            # Verify node ID matches public key
            expected_node_id = NodeIdentity.node_id_from_public_key(
                peer_handshake.public_key
            )
            if peer_handshake.sender_id != expected_node_id:
                return None

            # Verify timestamp (anti-replay)
            now = time.time()
            if abs(now - peer_handshake.timestamp) > 300:  # 5 minutes tolerance
                return None

            # Don't connect to ourselves
            if peer_handshake.sender_id == self.identity.node_id:
                return None

            # Validate private network token
            if self._private_network and self._network_id and self._network_password:
                if not peer_handshake.network_token:
                    return None

                # Validate token for current AND previous window (clock tolerance)
                token_current = compute_network_token(
                    self._network_id,
                    self._network_password,
                    peer_handshake.sender_id,
                    peer_handshake.timestamp,
                )
                token_prev = compute_network_token(
                    self._network_id,
                    self._network_password,
                    peer_handshake.sender_id,
                    peer_handshake.timestamp - 300,
                )
                if not (
                    hmac.compare_digest(peer_handshake.network_token, token_current)
                    or hmac.compare_digest(peer_handshake.network_token, token_prev)
                ):
                    return None

            fallback_address = connectable_address
            if not is_outbound and fallback_address and peer_handshake.listen_port > 0:
                fallback_address = replace_websocket_url_port(
                    fallback_address, peer_handshake.listen_port
                )

            effective_address = await resolve_effective_peer_address(
                peer_handshake.public_url,
                fallback_address,
                allow_private=(
                    self._private_network or settings.COMETNET_ALLOW_PRIVATE_PEX
                ),
            )

            # Create connection record
            conn = PeerConnection(
                node_id=peer_handshake.sender_id,
                address=effective_address,
                websocket=websocket,
                client_ip=client_ip,
                public_key=peer_handshake.public_key,
                is_outbound=is_outbound,
                alias=peer_handshake.alias,
            )
            # Atomically bind the verified identity to one live connection. The
            # caller's pending reservation already owns the capacity slot.
            async with self._connection_lock:
                if peer_handshake.sender_id in self._connections:
                    await websocket.close()
                    return None
                if self._keystore:
                    self._keystore.store_verified_key(
                        node_id=peer_handshake.sender_id,
                        public_key_hex=peer_handshake.public_key,
                    )
                self._connections[peer_handshake.sender_id] = conn

            # Start message receiver task
            task = create_detached_task(
                self._receive_loop(conn),
                name="cometnet-peer-receive",
                keep_connection=True,
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

            return peer_handshake.sender_id
        except TimeoutError:
            return None
        except (ConnectionClosed, WebSocketException, OSError):
            return None

    async def _receive_loop(self, conn: PeerConnection) -> None:
        """Receive loop for a single connection."""
        try:
            while self._running:
                try:
                    raw_message = await conn.websocket.recv()
                    conn.bytes_received += len(raw_message)
                    conn.messages_received += 1
                    conn.update_activity()

                    # Rate limiting check
                    if not conn.check_rate_limit(
                        self.rate_limit_count, self.rate_limit_window
                    ):
                        continue

                    message = parse_message(raw_message)
                    if message is None:
                        continue

                    # Handle ping/pong internally
                    if isinstance(message, PingMessage):
                        if not await validate_message_security(
                            message, conn.node_id, self._keystore, None
                        ):
                            continue
                        await self._handle_ping(conn, message)
                    elif isinstance(message, PongMessage):
                        if not await validate_message_security(
                            message, conn.node_id, self._keystore, None
                        ):
                            continue
                        self._handle_pong(conn, message)
                    else:
                        # Route to registered handler
                        handler = self._handlers.get(message.type)
                        if handler:
                            try:
                                await handler(conn.node_id, message)
                            except Exception:
                                pass
                except ConnectionClosed:
                    break
        except Exception:
            pass
        finally:
            self._release_connection(conn.node_id, conn)

    def _release_ip_slot(self, ip: str) -> None:
        """Release one inbound per-IP reservation, dropping the key at zero."""
        remaining = self._connections_per_ip.get(ip, 0) - 1
        if remaining > 0:
            self._connections_per_ip[ip] = remaining
        else:
            self._connections_per_ip.pop(ip, None)

    def _release_connection(self, node_id: str, conn: PeerConnection) -> bool:
        """Drop ``conn`` iff it is still the live connection for ``node_id`` and
        release any inbound per-IP reservation it holds.

        Fully synchronous: it runs to completion without awaiting, so the two
        teardown paths (receive loop and disconnect_peer) can never release the
        same slot twice. Whichever path wins the identity check does the work;
        the other becomes a no-op.
        """
        if self._connections.get(node_id) is not conn:
            return False
        del self._connections[node_id]
        self._closed_bytes_sent += conn.bytes_sent
        self._closed_bytes_received += conn.bytes_received
        self._closed_messages_sent += conn.messages_sent
        self._closed_messages_received += conn.messages_received
        if not conn.is_outbound and conn.client_ip:
            self._release_ip_slot(conn.client_ip)
        from comet.observability import log

        log.info(
            "cometnet.peer.disconnected",
            "CometNet peer disconnected",
            peer_id=node_id,
            transferred_bytes=conn.bytes_sent + conn.bytes_received,
        )
        return True

    async def _handle_ping(self, conn: PeerConnection, ping: PingMessage) -> None:
        """Respond to a ping with a pong."""
        pong = PongMessage(
            sender_id=self.identity.node_id,
            nonce=ping.nonce,
        )
        pong.signature = await self.identity.sign_hex_async(pong.to_signable_bytes())
        await conn.send(pong)

    def _handle_pong(self, conn: PeerConnection, pong: PongMessage) -> None:
        """Handle a pong response."""
        if pong.nonce in conn.pending_pings:
            sent_time = conn.pending_pings.pop(pong.nonce)
            rtt = (time.time() - sent_time) * 1000

            # Ignore extremely old pongs (> 60s) - they're stale
            if rtt > 60000:
                return

            # Add to rolling window and compute average
            conn.latency_samples.append(rtt)
            conn.latency_ms = sum(conn.latency_samples) / len(conn.latency_samples)

    async def _ping_loop(self) -> None:
        """Periodically ping all peers to check health."""
        while self._running:
            try:
                await asyncio.sleep(settings.COMETNET_TRANSPORT_PING_INTERVAL)

                # Get all connections
                connections = list(self._connections.values())
                if not connections:
                    continue

                now = time.time()
                stale_nodes: list[str] = []
                high_latency_nodes: list[str] = []
                max_latency = settings.COMETNET_TRANSPORT_MAX_LATENCY_MS

                peers_to_ping: list[PeerConnection] = []
                for conn in connections:
                    if (
                        now - conn.last_activity
                        > settings.COMETNET_TRANSPORT_CONNECTION_TIMEOUT
                    ):
                        stale_nodes.append(conn.node_id)
                        continue

                    stale_pings = [
                        nonce
                        for nonce, sent_time in conn.pending_pings.items()
                        if now - sent_time > 60
                    ]
                    for nonce in stale_pings:
                        del conn.pending_pings[nonce]

                    if len(conn.latency_samples) >= 5 and conn.latency_ms > max_latency:
                        high_latency_nodes.append(conn.node_id)
                        continue

                    peers_to_ping.append(conn)

                ping_data: list[tuple] = []
                for conn in peers_to_ping:
                    nonce = secrets.token_hex(8)
                    ping = PingMessage(
                        sender_id=self.identity.node_id,
                        nonce=nonce,
                    )
                    ping_data.append((conn, nonce, ping))

                if ping_data:

                    async def sign_ping(ping: PingMessage) -> str:
                        return await self.identity.sign_hex_async(
                            ping.to_signable_bytes()
                        )

                    signatures = await asyncio.gather(
                        *(sign_ping(ping) for _, _, ping in ping_data),
                        return_exceptions=True,
                    )

                    signed_pings = []
                    for i, (conn, nonce, ping) in enumerate(ping_data):
                        if not self._running:
                            break

                        sig = signatures[i]
                        if isinstance(sig, Exception):
                            continue

                        ping.signature = sig
                        send_time = time.time()
                        conn.pending_pings[nonce] = send_time
                        signed_pings.append((conn, nonce, ping))

                    send_results = await asyncio.gather(
                        *(conn.send(ping) for conn, _, ping in signed_pings),
                        return_exceptions=True,
                    )
                    for (conn, nonce, _), result in zip(signed_pings, send_results):
                        if result is not True:
                            conn.pending_pings.pop(nonce, None)

                # Disconnect stale connections
                if stale_nodes:
                    await asyncio.gather(
                        *(self.disconnect_peer(nid) for nid in stale_nodes),
                        return_exceptions=True,
                    )

                # Disconnect high-latency connections
                if high_latency_nodes:
                    await asyncio.gather(
                        *(self.disconnect_peer(nid) for nid in high_latency_nodes),
                        return_exceptions=True,
                    )

                # Eclipse Attack auto-remediation
                await self._remediate_eclipse_attack()
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    async def disconnect_peer(self, node_id: str) -> None:
        """Disconnect from a specific peer."""
        connection = self._connections.get(node_id)
        if connection is not None:
            await connection.close()
            self._release_connection(node_id, connection)

    async def _remediate_eclipse_attack(self) -> None:
        """
        Detect and remediate potential Eclipse attacks.

        If IP diversity is too low (many peers from same IPs), disconnect
        some peers from overrepresented IPs to make room for diverse connections.
        """
        # Only check if we have enough peers to evaluate
        if len(self._connections) < 5:
            return

        # Calculate IP distribution
        ip_counts: dict[str, list[str]] = {}  # ip -> list of node_ids
        for node_id, conn in self._connections.items():
            ip = extract_ip_from_address(conn.address)
            if ip != "unknown":
                if ip not in ip_counts:
                    ip_counts[ip] = []
                ip_counts[ip].append(node_id)

        if not ip_counts:
            return

        # Calculate diversity (unique IPs / total connections)
        unique_ips = len(ip_counts)
        total_peers = len(self._connections)
        diversity = unique_ips / total_peers

        # Threshold for action: if diversity < 0.4 (e.g., 5 connections from 2 IPs)
        if diversity >= 0.4:
            return

        # Find overrepresented IPs (more than 2 connections from same IP)
        peers_to_disconnect = []
        for ip, node_ids in ip_counts.items():
            # Determine max connections allowed for this IP
            max_allowed = 2
            try:
                if ipaddress.ip_address(ip).is_private:
                    max_allowed = 50
            except ValueError:
                pass

            if len(node_ids) > max_allowed:
                # Keep only max_allowed connections per IP, disconnect the rest (prefer newer ones)
                # Sort by connected_at and keep oldest ones (most stable)
                sorted_peers = sorted(
                    node_ids, key=lambda nid: self._connections[nid].connected_at
                )
                peers_to_disconnect.extend(sorted_peers[max_allowed:])

        if peers_to_disconnect:
            for node_id in peers_to_disconnect:
                await self.disconnect_peer(node_id)

    async def broadcast(
        self, message: AnyMessage, exclude: set[str] | None = None
    ) -> int:
        """
        Broadcast a message to all connected peers.

        Returns the number of peers the message was sent to.
        """
        exclude = exclude or set()

        # Filter targets first
        targets = [
            conn
            for node_id, conn in self._connections.items()
            if node_id not in exclude
        ]

        if not targets:
            return 0

        # Send to targets in batches
        batch_size = 50
        sent_count = 0

        for i in range(0, len(targets), batch_size):
            batch = targets[i : i + batch_size]
            results = await asyncio.gather(
                *(conn.send(message) for conn in batch), return_exceptions=True
            )
            sent_count += sum(1 for r in results if r is True)

        return sent_count

    async def send_to_peer(self, node_id: str, message: AnyMessage) -> bool:
        """Send a message to a specific peer."""
        if node_id in self._connections:
            return await self._connections[node_id].send(message)
        return False

    def get_random_peers(
        self, count: int, exclude: set[str] | None = None
    ) -> list[str]:
        """Get a random sample of connected peer node IDs."""
        exclude = exclude or set()
        available = [nid for nid in self._connections if nid not in exclude]
        return random.sample(available, min(count, len(available)))

    def get_peer_addresses(self) -> dict[str, str]:
        """Get a mapping of node_id to address for all connected peers."""
        return {nid: conn.address for nid, conn in self._connections.items()}

    def get_connection_stats(self) -> dict:
        """Get statistics about connections including security metrics."""
        # Calculate IP diversity for Eclipse attack detection
        unique_ips = set()
        for conn in self._connections.values():
            ip = extract_ip_from_address(conn.address)
            if ip != "unknown":
                unique_ips.add(ip)

        ip_diversity = (
            len(unique_ips) / len(self._connections) if self._connections else 1.0
        )
        active = tuple(self._connections.values())

        return {
            "connected_peers": len(self._connections),
            "outbound": sum(1 for c in self._connections.values() if c.is_outbound),
            "inbound": sum(1 for c in self._connections.values() if not c.is_outbound),
            "unique_ips": len(unique_ips),
            "ip_diversity": round(
                ip_diversity, 2
            ),  # 1.0 = all unique, lower = potential eclipse
            "avg_latency_ms": (
                sum(c.latency_ms for c in active) / len(self._connections)
                if self._connections
                else 0
            ),
            "bytes_sent": self._closed_bytes_sent
            + sum(connection.bytes_sent for connection in active),
            "bytes_received": self._closed_bytes_received
            + sum(connection.bytes_received for connection in active),
            "messages_sent": self._closed_messages_sent
            + sum(connection.messages_sent for connection in active),
            "messages_received": self._closed_messages_received
            + sum(connection.messages_received for connection in active),
        }
