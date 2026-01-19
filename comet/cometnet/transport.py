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
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional, Set

import websockets
from websockets.client import WebSocketClientProtocol
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Response

from comet.cometnet.crypto import NodeIdentity
from comet.cometnet.protocol import (AnyMessage, HandshakeMessage, MessageType,
                                     PingMessage, PongMessage, parse_message)
from comet.cometnet.utils import (extract_ip_from_address,
                                  format_address_for_log)
from comet.core.logger import logger
from comet.core.models import settings
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


@dataclass
class PeerConnection:
    """Represents an active connection to a peer."""

    node_id: str
    address: str  # WebSocket URL
    websocket: WebSocketClientProtocol
    public_key: str = ""
    connected_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    is_outbound: bool = True  # True if we initiated the connection
    listen_port: int = 0  # Port where this peer accepts connections
    pending_pings: Dict[str, float] = field(default_factory=dict)  # nonce -> sent_time
    latency_ms: float = 0.0
    latency_samples: deque = field(
        default_factory=lambda: deque(maxlen=10)
    )  # Rolling window of latency samples

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

    async def send(self, message: AnyMessage) -> bool:
        """Send a message to this peer. Returns True on success."""
        try:
            await self.websocket.send(message.to_bytes())
            self.update_activity()
            return True
        except ConnectionClosed:
            return False
        except Exception as e:
            logger.warning(f"Error sending message to {self.node_id[:8]}: {e}")
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

    # Security limits
    def __init__(
        self,
        identity: NodeIdentity,
        listen_port: int = 8765,
        max_peers: int = 50,
        advertise_url: Optional[str] = None,
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

        # Rate limits
        self.rate_limit_count = settings.COMETNET_TRANSPORT_RATE_LIMIT_COUNT
        self.rate_limit_window = settings.COMETNET_TRANSPORT_RATE_LIMIT_WINDOW

        # Active connections by node_id
        self._connections: Dict[str, PeerConnection] = {}

        # Track connections per IP to prevent abuse
        self._connections_per_ip: Dict[str, int] = {}

        # Lock for connection operations to prevent race conditions
        self._connection_lock = asyncio.Lock()

        # Addresses we're currently trying to connect to (to prevent duplicates)
        self._connecting: Set[str] = set()

        # Message handlers by message type
        self._handlers: Dict[MessageType, MessageHandler] = {}

        # Server task
        self._server = None
        self._server_task: Optional[asyncio.Task] = None

        # Background tasks
        self._tasks: Set[asyncio.Task] = set()

        # Running flag
        self._running = False

        # Private network settings
        self._private_network = settings.COMETNET_PRIVATE_NETWORK
        self._network_id = settings.COMETNET_NETWORK_ID or ""
        self._network_password = settings.COMETNET_NETWORK_PASSWORD or ""

        # Callback when a peer connects (for Discovery notification)
        self._on_peer_connected: Optional[Callable[[str, str], Awaitable[None]]] = None

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
        connection_header = request.headers.get("Connection", "").lower()
        upgrade_header = request.headers.get("Upgrade", "").lower()

        if "upgrade" not in connection_header or upgrade_header != "websocket":
            path = request.path or "/"
            path_lower = path.lower().rstrip("/")

            # Known health check paths (don't warn for these)
            health_paths = {
                "",
                "/",
                "/health",
                "/healthz",
                "/cometnet",
                "/cometnet/health",
                "/cometnet/healthz",
            }

            is_health_check = (
                path_lower in health_paths
                or path_lower.endswith("/health")
                or path_lower.endswith("/healthz")
            )

            if not is_health_check:
                logger.warning(
                    f"Received HTTP request on WebSocket port (path={path}). "
                    "If using a reverse proxy, ensure it forwards WebSocket headers: "
                    "'Upgrade: websocket' and 'Connection: Upgrade'"
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
                return Response(
                    426,
                    "Upgrade Required",
                    websockets.Headers(
                        [
                            ("Content-Type", "text/plain"),
                            ("Upgrade", "websocket"),
                        ]
                    ),
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

    def get_peer_address(self, node_id: str) -> Optional[str]:
        """Get the address (IP:port) of a connected peer."""
        conn = self._connections.get(node_id)
        if not conn:
            return None

        # If we have a listen port (from handshake), prefer it over the socket port
        # This ensures PEX shares the correct connectable address
        if conn.listen_port > 0:
            try:
                scheme = "wss" if conn.address.startswith("wss://") else "ws"
                clean = conn.address.replace(f"{scheme}://", "")
                host = clean.split(":")[0]
                return f"{scheme}://{host}:{conn.listen_port}"
            except Exception:
                pass

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
                "0.0.0.0",
                self.listen_port,
                ping_interval=None,
                ping_timeout=None,
                max_size=self.max_message_size,
                process_request=self._process_request,
            )
            logger.log(
                "COMETNET",
                f"WebSocket server listening on port {self.listen_port}",
            )
        except Exception as e:
            logger.warning(
                f"Failed to start WebSocket server on port {self.listen_port}: {e}"
            )
            # Continue anyway - we can still make outbound connections

        # Start ping task
        ping_task = asyncio.create_task(self._ping_loop())
        self._tasks.add(ping_task)

        logger.log(
            "COMETNET",
            "Transport layer started",
        )

    async def _handle_ws_connection(self, websocket, path: str = "") -> None:
        """
        Handle incoming WebSocket connection from the native server.
        """
        # Try to get real IP from proxy headers
        real_ip = getattr(websocket, "real_client_ip", None)

        if real_ip:
            # We got a real IP from proxy headers
            address = f"ws://{real_ip}:0"
        else:
            # No proxy headers - use direct connection IP
            # This will be the reverse proxy IP if behind one
            remote = websocket.remote_address
            if remote:
                address = f"ws://{remote[0]}:{remote[1]}"
            else:
                address = "ws://unknown:0"

        node_id = await self.handle_incoming_connection(websocket, address)

        if node_id:
            # Notify Discovery of the new connection
            if self._on_peer_connected:
                corrected_address = self.get_peer_address(node_id) or address
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
        for task in self._tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()

        # Close all connections
        for conn in list(self._connections.values()):
            await conn.close()
        self._connections.clear()

        logger.log("COMETNET", "Transport layer stopped")

    async def connect_to_peer(self, address: str) -> Optional[str]:
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
            if len(self._connections) >= self.max_peers:
                return None

            self._connecting.add(address)

        try:
            # Connect with timeout
            websocket = await asyncio.wait_for(
                websockets.connect(
                    address,
                    ping_interval=None,
                    ping_timeout=None,
                    max_size=self.max_message_size,
                ),
                timeout=5.0,
            )

            # Perform handshake
            node_id = await self._perform_handshake(
                websocket, address, is_outbound=True
            )

            if node_id:
                logger.log("COMETNET", f"Connected to peer {node_id[:8]} at {address}")
                return node_id
            else:
                await websocket.close()
                return None

        except asyncio.TimeoutError:
            return None
        except Exception:
            return None
        finally:
            self._connecting.discard(address)

    async def handle_incoming_connection(
        self, websocket: WebSocketClientProtocol, address: str
    ) -> Optional[str]:
        """
        Handle an incoming WebSocket connection.

        Returns the peer's node_id on success, None on failure.
        """
        if not self._running:
            await websocket.close()
            return None

        # Extract IP from the address (format: ws://IP:port)
        ip = extract_ip_from_address(address)

        # Use lock to prevent race condition on connection limits
        async with self._connection_lock:
            # Check per-IP connection limit (prevent Sybil-like attacks)
            # Relax limit for private IPs (local network, Docker)
            limit = self.max_connections_per_ip
            try:
                if ipaddress.ip_address(ip).is_private:
                    limit = max(
                        limit, 50
                    )  # Allow more connections from local/private IPs
            except ValueError:
                pass  # Not an IP address (hostname)

            current_ip_connections = self._connections_per_ip.get(ip, 0)
            if current_ip_connections >= limit:
                logger.debug(
                    f"Rejecting connection from {ip}: too many connections (limit: {limit})"
                )
                await websocket.close()
                return None

            # Check peer limit
            if len(self._connections) >= self.max_peers:
                await websocket.close()
                return None

            # Pre-increment IP counter to reserve slot (will decrement if handshake fails)
            self._connections_per_ip[ip] = current_ip_connections + 1

        # Perform handshake (we wait for their handshake first)
        node_id = await self._perform_handshake(websocket, address, is_outbound=False)

        if node_id:
            logger.log("COMETNET", f"Accepted connection from peer {node_id[:8]}")
            return node_id
        else:
            # Handshake failed, decrement IP counter
            async with self._connection_lock:
                self._connections_per_ip[ip] = max(
                    0, self._connections_per_ip.get(ip, 1) - 1
                )
                if self._connections_per_ip.get(ip, 0) == 0:
                    self._connections_per_ip.pop(ip, None)
            await websocket.close()
            return None

    async def _perform_handshake(
        self, websocket: WebSocketClientProtocol, address: str, is_outbound: bool
    ) -> Optional[str]:
        """
        Perform the handshake protocol with a peer.

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
                logger.warning(
                    f"Invalid signature in handshake from {format_address_for_log(address)}"
                )
                return None

            # Verify node ID matches public key
            expected_node_id = NodeIdentity.node_id_from_public_key(
                peer_handshake.public_key
            )
            if peer_handshake.sender_id != expected_node_id:
                logger.warning(
                    f"Node ID mismatch in handshake from {format_address_for_log(address)}"
                )
                return None

            # Verify timestamp (anti-replay)
            now = time.time()
            if abs(now - peer_handshake.timestamp) > 300:  # 5 minutes tolerance
                logger.warning(
                    f"Rejecting handshake from {format_address_for_log(address)}: timestamp skew too large "
                    f"(diff: {now - peer_handshake.timestamp:.1f}s)"
                )
                return None

            # Check if already connected to this node
            if peer_handshake.sender_id in self._connections:
                await websocket.close()
                return peer_handshake.sender_id

            # Don't connect to ourselves
            if peer_handshake.sender_id == self.identity.node_id:
                return None

            # Validate private network token
            if self._private_network and self._network_id and self._network_password:
                if not peer_handshake.network_token:
                    logger.warning(
                        f"Rejecting {format_address_for_log(address)}: missing network token (private mode)"
                    )
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
                    logger.warning(
                        f"Rejecting {format_address_for_log(address)}: invalid network token (wrong password or network_id)"
                    )
                    return None

            # Determine effective address
            effective_address = address
            if peer_handshake.public_url:
                effective_address = peer_handshake.public_url

            # Create connection record
            conn = PeerConnection(
                node_id=peer_handshake.sender_id,
                address=effective_address,
                websocket=websocket,
                public_key=peer_handshake.public_key,
                is_outbound=is_outbound,
                listen_port=peer_handshake.listen_port,
            )
            self._connections[peer_handshake.sender_id] = conn

            # Store verified public key in keystore
            if self._keystore:
                self._keystore.store_key(
                    node_id=peer_handshake.sender_id,
                    public_key_hex=peer_handshake.public_key,
                    verified=True,
                )

            # Start message receiver task
            task = asyncio.create_task(self._receive_loop(conn))
            self._tasks.add(task)

            return peer_handshake.sender_id

        except asyncio.TimeoutError:
            logger.debug(f"Handshake timeout with {format_address_for_log(address)}")
            return None
        except ConnectionClosed:
            # Connection closed during handshake - likely incompatible or rejecting us
            return None
        except Exception:
            return None

    async def _receive_loop(self, conn: PeerConnection) -> None:
        """Receive loop for a single connection."""
        try:
            while self._running:
                try:
                    raw_message = await conn.websocket.recv()
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
                        await self._handle_ping(conn, message)
                    elif isinstance(message, PongMessage):
                        self._handle_pong(conn, message)
                    else:
                        # Route to registered handler
                        handler = self._handlers.get(message.type)
                        if handler:
                            try:
                                await handler(conn.node_id, message)
                            except Exception as e:
                                logger.warning(f"Handler error for {message.type}: {e}")

                except ConnectionClosed:
                    break

        except Exception:
            pass
        finally:
            # Clean up connection
            if conn.node_id in self._connections:
                # Decrement IP connection counter
                ip = extract_ip_from_address(conn.address)
                if ip in self._connections_per_ip:
                    self._connections_per_ip[ip] = max(
                        0, self._connections_per_ip[ip] - 1
                    )
                    if self._connections_per_ip[ip] == 0:
                        del self._connections_per_ip[ip]
                del self._connections[conn.node_id]
            logger.log("COMETNET", f"Disconnected from peer {conn.node_id[:8]}")

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
        """Periodically ping all peers to check health (staggered)."""
        while self._running:
            try:
                await asyncio.sleep(settings.COMETNET_TRANSPORT_PING_INTERVAL)

                # Get all connections
                connections = list(self._connections.values())
                if not connections:
                    continue

                # Shuffle to randomize order
                random.shuffle(connections)

                stale_nodes: List[str] = []

                now = time.time()
                high_latency_nodes: List[str] = []
                max_latency = settings.COMETNET_TRANSPORT_MAX_LATENCY_MS or 10000.0

                for conn in connections:
                    if not self._running:
                        break

                    # Check for stale connection
                    if (
                        now - conn.last_activity
                        > settings.COMETNET_TRANSPORT_CONNECTION_TIMEOUT
                    ):
                        stale_nodes.append(conn.node_id)
                        continue

                    # Clean up stale pending pings (older than 60s)
                    # This prevents latency from being calculated on very old pings
                    stale_pings = [
                        nonce
                        for nonce, sent_time in conn.pending_pings.items()
                        if now - sent_time > 60
                    ]
                    for nonce in stale_pings:
                        del conn.pending_pings[nonce]

                    # Check for persistently high latency (only if we have enough samples)
                    if len(conn.latency_samples) >= 5 and conn.latency_ms > max_latency:
                        high_latency_nodes.append(conn.node_id)
                        continue

                    # Prepare ping
                    nonce = secrets.token_hex(8)
                    ping = PingMessage(
                        sender_id=self.identity.node_id,
                        nonce=nonce,
                    )
                    ping.signature = await self.identity.sign_hex_async(
                        ping.to_signable_bytes()
                    )
                    conn.pending_pings[nonce] = now

                    # Send without waiting
                    asyncio.create_task(conn.send(ping))

                    # Sleep briefly to spread load (thundering herd prevention)
                    await asyncio.sleep(0.05)

                # Disconnect stale connections
                if stale_nodes:
                    await asyncio.gather(
                        *(self.disconnect_peer(nid) for nid in stale_nodes),
                        return_exceptions=True,
                    )

                # Disconnect high-latency connections
                if high_latency_nodes:
                    logger.log(
                        "COMETNET",
                        f"Disconnecting {len(high_latency_nodes)} peers with high latency (>{max_latency:.0f}ms)",
                    )
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
        if node_id in self._connections:
            await self._connections[node_id].close()
            del self._connections[node_id]

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
        ip_counts: Dict[str, List[str]] = {}  # ip -> list of node_ids
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
            logger.warning(
                f"Eclipse attack remediation: Disconnecting {len(peers_to_disconnect)} peers "
                f"from overrepresented IPs (diversity was {diversity:.2f})"
            )
            for node_id in peers_to_disconnect:
                await self.disconnect_peer(node_id)

    async def broadcast(
        self, message: AnyMessage, exclude: Optional[Set[str]] = None
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
        self, count: int, exclude: Optional[Set[str]] = None
    ) -> list[str]:
        """Get a random sample of connected peer node IDs."""
        exclude = exclude or set()
        available = [nid for nid in self._connections.keys() if nid not in exclude]
        return random.sample(available, min(count, len(available)))

    def get_peer_addresses(self) -> Dict[str, str]:
        """Get a mapping of node_id to address for all connected peers."""
        return {nid: conn.address for nid, conn in self._connections.items()}

    def get_connection_stats(self) -> Dict:
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

        return {
            "connected_peers": len(self._connections),
            "outbound": sum(1 for c in self._connections.values() if c.is_outbound),
            "inbound": sum(1 for c in self._connections.values() if not c.is_outbound),
            "unique_ips": len(unique_ips),
            "ip_diversity": round(
                ip_diversity, 2
            ),  # 1.0 = all unique, lower = potential eclipse
            "connections_per_ip": dict(self._connections_per_ip),
            "avg_latency_ms": (
                sum(c.latency_ms for c in self._connections.values())
                / len(self._connections)
                if self._connections
                else 0
            ),
        }
