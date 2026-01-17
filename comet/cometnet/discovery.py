"""
CometNet Discovery Module

Handles peer discovery through multiple methods:
- Manual peer configuration
- Bootstrap nodes
- Peer Exchange (PEX)
"""

import asyncio
import ipaddress
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional
from urllib.parse import urlparse

from comet.cometnet.protocol import PeerInfo, PeerRequest, PeerResponse
from comet.core.logger import logger
from comet.core.models import settings


def is_private_or_internal_ip(host: str) -> bool:
    """
    Check if a host is a private/internal IP address.

    Checks for: private, loopback, link-local, and reserved addresses.
    """
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
    except ValueError:
        # Not an IP address (could be a hostname)
        return False


def is_valid_peer_address(address: str, allow_private: bool = False) -> bool:
    """
    Validate a peer address for security.

    Args:
        address: WebSocket URL to validate
        allow_private: If True, allow private/internal IPs (for manual/bootstrap peers)

    Returns:
        True if the address is valid and safe to connect to
    """
    try:
        parsed = urlparse(address)

        # Must be ws:// or wss:// scheme
        if parsed.scheme not in ("ws", "wss"):
            return False

        # Must have a hostname
        if not parsed.hostname:
            return False

        host = parsed.hostname.lower()

        # Block localhost variants (hostnames that resolve to loopback)
        if host in ("localhost", "localhost.localdomain"):
            if not allow_private:
                return False

        # Check for private/internal IP addresses
        if not allow_private and is_private_or_internal_ip(host):
            return False

        # Port must be valid if specified
        if parsed.port is not None:
            if not (1 <= parsed.port <= 65535):
                return False

        # Block suspicious patterns
        if "@" in address:  # Credential injection
            return False

        return True

    except Exception:
        return False


@dataclass
class KnownPeer:
    """Information about a known peer (connected or not)."""

    node_id: str
    address: str
    last_seen: float = field(default_factory=time.time)
    last_connect_attempt: float = 0.0
    connect_failures: int = 0
    source: str = "unknown"  # "manual", "bootstrap", "pex", "incoming"

    @property
    def is_connectable(self) -> bool:
        """Check if we should attempt to connect to this peer."""
        # Don't retry too quickly after failures
        if self.connect_failures > 0:
            backoff = min(
                settings.COMETNET_PEER_CONNECT_BACKOFF_MAX,
                30 * (2 ** (self.connect_failures - 1)),
            )
            if time.time() - self.last_connect_attempt < backoff:
                return False
        return True

    def record_connect_attempt(self, success: bool) -> None:
        """Record the result of a connection attempt."""
        self.last_connect_attempt = time.time()
        if success:
            self.connect_failures = 0
            self.last_seen = time.time()
        else:
            self.connect_failures += 1


class DiscoveryService:
    """
    Manages peer discovery for CometNet.

    Uses multiple methods to find peers:
    1. Manual peers (from configuration)
    2. Bootstrap nodes (public entry points)
    3. PEX (Peer Exchange - ask connected peers for more peers)
    """

    def __init__(
        self,
        manual_peers: Optional[List[str]] = None,
        bootstrap_nodes: Optional[List[str]] = None,
        min_peers: int = None,
        max_peers: int = None,
    ):
        self.manual_peers = manual_peers or []
        self.bootstrap_nodes = bootstrap_nodes or []
        self.min_peers = min_peers or settings.COMETNET_MIN_PEERS
        self.max_peers = max_peers or settings.COMETNET_MAX_PEERS

        # Known peers by address
        self._known_peers: Dict[str, KnownPeer] = {}

        # Node ID to address mapping (for deduplication)
        self._node_id_to_address: Dict[str, str] = {}

        # Callbacks
        self._connect_callback: Optional[Callable[[str], Awaitable[Optional[str]]]] = (
            None
        )
        self._get_connected_count: Optional[Callable[[], int]] = None
        self._get_connected_ids: Optional[Callable[[], List[str]]] = None
        self._send_message_callback: Optional[Callable[[str, Any], Awaitable[None]]] = (
            None
        )
        self._sign_callback: Optional[Callable[[bytes], Awaitable[str]]] = (
            None  # Returns hex signature
        )

        # Running state
        self._running = False
        self._discovery_task: Optional[asyncio.Task] = None

    def set_callbacks(
        self,
        connect_callback: Callable[[str], Awaitable[Optional[str]]],
        get_connected_count: Callable[[], int],
        get_connected_ids: Callable[[], List[str]],
        send_message_callback: Callable[[str, Any], Awaitable[None]],
        sign_callback: Optional[Callable[[bytes], Awaitable[str]]] = None,
    ) -> None:
        """Set callbacks for connection management."""
        self._connect_callback = connect_callback
        self._get_connected_count = get_connected_count
        self._get_connected_ids = get_connected_ids
        self._send_message_callback = send_message_callback
        self._sign_callback = sign_callback

    async def start(self, node_id: str, listen_port: int) -> None:
        """Start the discovery service."""
        if self._running:
            return

        self._running = True
        self._node_id = node_id
        self._listen_port = listen_port

        # Add manual peers
        for address in self.manual_peers:
            self._add_known_peer(address, source="manual")

        # Add bootstrap nodes
        for address in self.bootstrap_nodes:
            self._add_known_peer(address, source="bootstrap")

        # Start discovery loop
        self._discovery_task = asyncio.create_task(self._discovery_loop())

        logger.log(
            "COMETNET",
            f"Discovery service started with {len(self._known_peers)} known peers",
        )

    async def stop(self) -> None:
        """Stop the discovery service."""
        self._running = False

        if self._discovery_task:
            self._discovery_task.cancel()
            try:
                await self._discovery_task
            except asyncio.CancelledError:
                pass

        logger.log("COMETNET", "Discovery service stopped")

    def _add_known_peer(
        self, address: str, node_id: Optional[str] = None, source: str = "unknown"
    ) -> None:
        """Add a peer to the known peers list."""
        # Normalize address
        address = address.strip()
        if not address.startswith("ws://") and not address.startswith("wss://"):
            address = f"ws://{address}"

        if address not in self._known_peers:
            self._known_peers[address] = KnownPeer(
                node_id=node_id or "",
                address=address,
                source=source,
            )
        elif node_id and not self._known_peers[address].node_id:
            self._known_peers[address].node_id = node_id

        if node_id:
            self._node_id_to_address[node_id] = address

    def add_peer_from_pex(self, peer_info: PeerInfo) -> None:
        """
        Add a peer discovered through Peer Exchange.

        Validates the address to prevent SSRF attacks (unless COMETNET_ALLOW_PRIVATE_PEX is True).
        """
        if peer_info.node_id == self._node_id:
            return  # Don't add ourselves

        # Validate address before adding
        # Allow private IPs only if explicitly configured
        allow_private = settings.COMETNET_ALLOW_PRIVATE_PEX
        if not is_valid_peer_address(peer_info.address, allow_private=allow_private):
            logger.debug(f"Rejecting invalid PEX address: {peer_info.address}")
            return

        self._add_known_peer(
            address=peer_info.address, node_id=peer_info.node_id, source="pex"
        )

    def record_incoming_connection(self, node_id: str, address: str) -> None:
        """Record a peer that connected to us."""
        self._add_known_peer(address=address, node_id=node_id, source="incoming")

    def get_peers_for_pex(self, max_peers: int = None) -> List[PeerInfo]:
        """Get a list of peers to share via PEX."""
        if max_peers is None:
            max_peers = settings.COMETNET_PEX_BATCH_SIZE
        connected_ids = set(
            self._get_connected_ids() if self._get_connected_ids else []
        )

        peers = []
        for address, known_peer in self._known_peers.items():
            if known_peer.node_id and known_peer.node_id in connected_ids:
                peers.append(
                    PeerInfo(
                        node_id=known_peer.node_id,
                        address=known_peer.address,
                        last_seen=known_peer.last_seen,
                    )
                )
                if len(peers) >= max_peers:
                    break

        return peers

    async def handle_peer_request(
        self, sender_id: str, request: PeerRequest
    ) -> PeerResponse:
        """Handle a peer request and return a response."""
        peers = self.get_peers_for_pex(request.max_peers)

        # Don't include the requester in the response
        peers = [p for p in peers if p.node_id != sender_id]

        return PeerResponse(peers=peers)

    async def request_peers_from(
        self, node_id: str, send_callback: Callable[[str, PeerRequest], Awaitable[Any]]
    ) -> None:
        """Request peers from a connected peer."""
        request = PeerRequest(max_peers=settings.COMETNET_PEX_BATCH_SIZE)
        await send_callback(node_id, request)

    def handle_peer_response(self, response: PeerResponse) -> int:
        """
        Handle a peer response from PEX.
        Returns the number of new peers added.
        """
        new_count = 0
        for peer_info in response.peers:
            if peer_info.address not in self._known_peers:
                self.add_peer_from_pex(peer_info)
                new_count += 1
        return new_count

    async def _discovery_loop(self) -> None:
        """Main discovery loop."""
        # Initial delay to allow server to start
        await asyncio.sleep(2.0)

        while self._running:
            try:
                connected_count = (
                    self._get_connected_count() if self._get_connected_count else 0
                )

                # If we need more peers, try to connect
                if connected_count < self.min_peers:
                    await self._try_connect_to_peers()

                # Periodic maintenance
                self._cleanup_old_peers()

                # PEX: Ask connected peers for more peers
                if connected_count > 0:
                    await self._perform_pex()

                # Adaptive interval: faster when disconnected, slower when healthy
                if connected_count == 0:
                    # Aggressive reconnection when we have no peers
                    await asyncio.sleep(5.0)
                elif connected_count < self.min_peers:
                    # Medium pace when below minimum
                    await asyncio.sleep(15.0)
                else:
                    # Normal pace when healthy
                    await asyncio.sleep(30.0)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Discovery loop error: {e}")
                await asyncio.sleep(10.0)

    async def _perform_pex(self) -> None:
        """Periodically ask connected peers for their peer lists."""
        if not self._get_connected_ids or not self._send_message_callback:
            return

        connected_ids = self._get_connected_ids()
        if not connected_ids:
            return

        peers_to_query = connected_ids
        if len(connected_ids) > 3:
            peers_to_query = random.sample(connected_ids, 3)

        for node_id in peers_to_query:
            try:
                request = PeerRequest(
                    sender_id=self._node_id,
                    max_peers=self.max_peers,
                )
                # Sign the request if we have a signing callback
                if self._sign_callback:
                    request.signature = await self._sign_callback(
                        request.to_signable_bytes()
                    )
                await self._send_message_callback(node_id, request)
            except Exception as e:
                logger.debug(f"PEX query failed for {node_id[:8]}: {e}")

    async def _try_connect_to_peers(self) -> None:
        """Attempt to connect to known peers."""
        if not self._connect_callback:
            return

        connected_ids = set(
            self._get_connected_ids() if self._get_connected_ids else []
        )
        connected_count = len(connected_ids)

        # Sort peers by priority (manual > bootstrap > pex)
        priority = {
            "manual": 0,
            "bootstrap": 1,
            "incoming": 2,
            "pex": 3,
            "unknown": 4,
        }
        sorted_peers = sorted(
            self._known_peers.values(),
            key=lambda p: (priority.get(p.source, 5), p.connect_failures),
        )

        for known_peer in sorted_peers:
            if connected_count >= self.max_peers:
                break

            # Skip if already connected
            if known_peer.node_id and known_peer.node_id in connected_ids:
                continue

            # Skip if not connectable (backoff)
            if not known_peer.is_connectable:
                continue

            # Try to connect
            try:
                result = await self._connect_callback(known_peer.address)
                success = result is not None

                if success and result:
                    known_peer.node_id = result
                    self._node_id_to_address[result] = known_peer.address
                    connected_ids.add(result)
                    connected_count += 1

                known_peer.record_connect_attempt(success)

            except Exception as e:
                logger.debug(f"Connection attempt failed: {e}")
                known_peer.record_connect_attempt(False)

    def _cleanup_old_peers(self) -> None:
        """Remove very old peers with many failures."""
        cutoff = time.time() - settings.COMETNET_PEER_CLEANUP_AGE
        to_remove = [
            addr
            for addr, peer in self._known_peers.items()
            if peer.last_seen < cutoff
            and peer.connect_failures > settings.COMETNET_PEER_MAX_FAILURES
            and peer.source not in ("manual", "bootstrap")
        ]
        for addr in to_remove:
            if self._known_peers[addr].node_id:
                self._node_id_to_address.pop(self._known_peers[addr].node_id, None)
            del self._known_peers[addr]

    def get_stats(self) -> Dict:
        """Get discovery statistics."""
        sources = {}
        for peer in self._known_peers.values():
            sources[peer.source] = sources.get(peer.source, 0) + 1

        return {
            "known_peers": len(self._known_peers),
            "peers_by_source": sources,
        }

    def to_dict(self) -> Dict:
        """Serialize known peers for persistence."""
        peers_data = []
        for addr, peer in self._known_peers.items():
            # Only persist peers that are worth reconnecting to
            # Skip incoming (ephemeral) and failed peers
            if peer.source in ("incoming",) or peer.connect_failures > 3:
                continue
            peers_data.append(
                {
                    "address": peer.address,
                    "node_id": peer.node_id,
                    "source": peer.source,
                    "last_seen": peer.last_seen,
                }
            )
        return {"known_peers": peers_data}

    def from_dict(self, data: Dict) -> None:
        """Load known peers from persisted data."""
        peers_data = data.get("known_peers", [])
        loaded_count = 0
        for peer_info in peers_data:
            address = peer_info.get("address", "")
            if not address:
                continue
            # Validate before loading to prevent loading invalid data
            allow_private = peer_info.get("source") in ("manual", "bootstrap")
            if not is_valid_peer_address(address, allow_private=allow_private):
                continue

            self._known_peers[address] = KnownPeer(
                node_id=peer_info.get("node_id", ""),
                address=address,
                source=peer_info.get("source", "pex"),
                last_seen=peer_info.get("last_seen", time.time()),
            )
            if peer_info.get("node_id"):
                self._node_id_to_address[peer_info["node_id"]] = address
            loaded_count += 1

        if loaded_count > 0:
            logger.log("COMETNET", f"Loaded {loaded_count} persisted peers")
