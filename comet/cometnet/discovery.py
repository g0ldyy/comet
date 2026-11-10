"""
CometNet Discovery Module

Handles peer discovery through multiple methods:
- Manual peer configuration
- Bootstrap nodes
- Peer Exchange (PEX)
"""

import asyncio
import math
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from itertools import compress
from typing import Any

from comet.cometnet.protocol import PeerInfo, PeerRequest, PeerResponse
from comet.cometnet.utils import is_valid_peer_address
from comet.core.models import settings
from comet.observability import create_detached_task, log


def canonicalize_persisted_peers(peers: list[dict]) -> tuple[list[dict], bool]:
    """Keep the most recently seen address for each identified node."""
    address_indices: dict[str, int] = {}
    canonical_indices: dict[str, int] = {}
    for index, peer in enumerate(peers):
        address = peer["address"]
        previous_address_index = address_indices.get(address)
        if previous_address_index is None or (
            peer["last_seen"],
            peer["node_id"],
            peer["source"],
        ) > (
            peers[previous_address_index]["last_seen"],
            peers[previous_address_index]["node_id"],
            peers[previous_address_index]["source"],
        ):
            address_indices[address] = index

        node_id = peer["node_id"]
        if not node_id:
            continue

        previous_index = canonical_indices.get(node_id)
        if previous_index is None or (
            peer["last_seen"],
            peer["address"],
        ) > (
            peers[previous_index]["last_seen"],
            peers[previous_index]["address"],
        ):
            canonical_indices[node_id] = index

    canonical = [
        peer
        for index, peer in enumerate(peers)
        if address_indices[peer["address"]] == index
        and (not peer["node_id"] or canonical_indices[peer["node_id"]] == index)
    ]
    return canonical, len(canonical) != len(peers)


async def is_allowed_peer_address(address: str, source: str) -> bool:
    """Apply the SSRF policy matching where a peer address came from."""
    return await is_valid_peer_address(
        address,
        allow_private=source != "pex" or settings.COMETNET_ALLOW_PRIVATE_PEX,
    )


def resolve_discovery_configuration(
    manual_peers: list[str] | None,
    bootstrap_nodes: list[str] | None,
    min_peers: int | None,
    max_peers: int | None,
) -> tuple[list[str], list[str], int, int]:
    return (
        list(dict.fromkeys(manual_peers or ())),
        list(dict.fromkeys(bootstrap_nodes or ())),
        settings.COMETNET_MIN_PEERS if min_peers is None else min_peers,
        settings.COMETNET_MAX_PEERS if max_peers is None else max_peers,
    )


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
            max_backoff = settings.COMETNET_PEER_CONNECT_BACKOFF_MAX
            backoff = min(
                max_backoff,
                30 * (2 ** min(self.connect_failures - 1, max_backoff.bit_length())),
            )
            if time.time() - self.last_connect_attempt < backoff:
                return False
        return True

    def record_connect_attempt(self, success: bool) -> None:
        """Record the result of a connection attempt."""
        now = time.time()
        self.last_connect_attempt = now
        if success:
            self.connect_failures = 0
            self.last_seen = now
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
        manual_peers: list[str] | None = None,
        bootstrap_nodes: list[str] | None = None,
        min_peers: int | None = None,
        max_peers: int | None = None,
    ):
        (
            self.manual_peers,
            self.bootstrap_nodes,
            self.min_peers,
            self.max_peers,
        ) = resolve_discovery_configuration(
            manual_peers, bootstrap_nodes, min_peers, max_peers
        )

        # Known peers by address
        self._known_peers: dict[str, KnownPeer] = {}

        # Callbacks
        self._connect_callback: Callable[[str], Awaitable[str | None]] | None = None
        self._get_connected_count: Callable[[], int] | None = None
        self._get_connected_ids: Callable[[], list[str]] | None = None
        self._send_message_callback: Callable[[str, Any], Awaitable[None]] | None = None
        self._sign_callback: Callable[[bytes], Awaitable[str]] | None = (
            None  # Returns hex signature
        )

        # Running state
        self._running = False
        self._discovery_task: asyncio.Task | None = None

    def set_callbacks(
        self,
        connect_callback: Callable[[str], Awaitable[str | None]],
        get_connected_count: Callable[[], int],
        get_connected_ids: Callable[[], list[str]],
        send_message_callback: Callable[[str, Any], Awaitable[None]],
        sign_callback: Callable[[bytes], Awaitable[str]] | None = None,
    ) -> None:
        """Set callbacks for connection management."""
        self._connect_callback = connect_callback
        self._get_connected_count = get_connected_count
        self._get_connected_ids = get_connected_ids
        self._send_message_callback = send_message_callback
        self._sign_callback = sign_callback

    async def start(self, node_id: str) -> None:
        """Start the discovery service."""
        if self._running:
            return

        self._running = True
        self._node_id = node_id

        # Add manual peers
        for address in self.manual_peers:
            self._add_known_peer(address, source="manual")

        # Add bootstrap nodes
        for address in self.bootstrap_nodes:
            self._add_known_peer(address, source="bootstrap")

        # Start discovery loop
        self._discovery_task = create_detached_task(
            self._discovery_loop(),
            name="cometnet-discovery",
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
            self._discovery_task = None

    def _add_known_peer(
        self, address: str, node_id: str | None = None, source: str = "unknown"
    ) -> None:
        """Add a peer to the known peers list."""
        address = address.strip()

        if address not in self._known_peers:
            self._known_peers[address] = KnownPeer(
                node_id=node_id or "",
                address=address,
                source=source,
            )
        elif node_id and not self._known_peers[address].node_id:
            self._known_peers[address].node_id = node_id

    async def add_peer_from_pex(self, peer_info: PeerInfo) -> bool:
        """
        Add a peer discovered through Peer Exchange.

        Validates the address to prevent SSRF attacks (unless COMETNET_ALLOW_PRIVATE_PEX is True).
        """
        if peer_info.node_id == self._node_id:
            return False  # Don't add ourselves

        if not await is_allowed_peer_address(peer_info.address, "pex"):
            return False

        is_new = peer_info.address not in self._known_peers
        if is_new:
            pex_peers = [
                peer for peer in self._known_peers.values() if peer.source == "pex"
            ]
            if self.max_peers <= 0:
                return False
            if len(pex_peers) >= self.max_peers:
                oldest = min(pex_peers, key=lambda peer: peer.last_seen)
                del self._known_peers[oldest.address]
        self._add_known_peer(
            address=peer_info.address, node_id=peer_info.node_id, source="pex"
        )
        return is_new

    def record_incoming_connection(self, node_id: str, address: str) -> None:
        """Record a peer that connected to us."""
        self._add_known_peer(address=address, node_id=node_id, source="incoming")

    async def get_peers_for_pex(self, max_peers: int | None = None) -> list[PeerInfo]:
        """Get a list of peers to share via PEX."""
        if max_peers is None:
            max_peers = settings.COMETNET_PEX_BATCH_SIZE
        connected_ids = set(
            self._get_connected_ids() if self._get_connected_ids else []
        )

        # Only share private IPs if explicitly allowed
        allow_private = settings.COMETNET_ALLOW_PRIVATE_PEX

        # Snapshot eligible peer data before address validation yields control.
        # Discovery updates can otherwise resize _known_peers while its dynamic
        # view is being iterated.
        candidates = [
            (peer.node_id, peer.address, peer.last_seen)
            for peer in self._known_peers.values()
            if peer.node_id and peer.node_id in connected_ids
        ]

        peers = []
        for node_id, address, last_seen in candidates:
            if not allow_private and not await is_valid_peer_address(
                address, allow_private=False
            ):
                continue
            peers.append(
                PeerInfo(
                    node_id=node_id,
                    address=address,
                    last_seen=last_seen,
                )
            )
            if len(peers) >= max_peers:
                break

        return peers

    async def handle_peer_request(
        self, sender_id: str, request: PeerRequest
    ) -> PeerResponse:
        """Handle a peer request and return a response."""
        peers = await self.get_peers_for_pex(request.max_peers)

        # Don't include the requester in the response
        peers = [p for p in peers if p.node_id != sender_id]

        return PeerResponse(peers=peers)

    async def handle_peer_response(self, response: PeerResponse) -> int:
        """
        Handle a peer response from PEX.
        Returns the number of new peers added.
        """
        new_count = 0
        for peer_info in response.peers:
            if await self.add_peer_from_pex(peer_info):
                new_count += 1
        return new_count

    async def _discovery_loop(self) -> None:
        """Main discovery loop."""
        # Initial delay to allow server to start
        await asyncio.sleep(2.0)

        while self._running:
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
                await asyncio.sleep(5.0)
            elif connected_count < self.min_peers:
                await asyncio.sleep(15.0)
            else:
                await asyncio.sleep(30.0)

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
            except Exception:
                pass

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
                    connected_ids.add(result)
                    connected_count += 1

                known_peer.record_connect_attempt(success)
            except Exception:
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
            del self._known_peers[addr]

    def get_stats(self) -> dict:
        """Get discovery statistics."""
        sources = {}
        for peer in self._known_peers.values():
            sources[peer.source] = sources.get(peer.source, 0) + 1

        return {
            "known_peers": len(self._known_peers),
            "peers_by_source": sources,
        }

    def to_dict(self) -> dict:
        """Serialize known peers for persistence."""
        peers_data = []
        for peer in sorted(self._known_peers.values(), key=lambda peer: peer.address):
            # Configured peers are restored from settings; incoming and failed
            # peers aren't useful reconnect candidates.
            if peer.source != "pex" or peer.connect_failures:
                continue
            peers_data.append(
                {
                    "address": peer.address,
                    "node_id": peer.node_id,
                    "source": peer.source,
                    "last_seen": peer.last_seen,
                }
            )
        canonical_peers, _ = canonicalize_persisted_peers(peers_data)
        return {"known_peers": canonical_peers}

    async def from_dict(self, data: dict) -> None:
        """Load known peers from persisted data."""
        if type(data) is not dict or "known_peers" not in data:
            raise ValueError("discovery state does not match the current schema")
        if type(data["known_peers"]) is not list:
            raise ValueError("known_peers must be a list")

        validated_peers = []
        for index, peer_info in enumerate(data["known_peers"]):
            required_fields = {
                "address",
                "node_id",
                "source",
                "last_seen",
            }
            if type(peer_info) is not dict or not required_fields <= peer_info.keys():
                raise ValueError(f"known_peers[{index}] has an invalid schema")
            address = peer_info["address"]
            node_id = peer_info["node_id"]
            source = peer_info["source"]
            last_seen = peer_info["last_seen"]
            if type(address) is not str or not address:
                raise ValueError("persisted peer address must be non-empty")
            if type(node_id) is not str:
                raise ValueError("persisted peer node_id must be a string")
            if source not in {"manual", "bootstrap", "pex"}:
                raise ValueError("persisted peer source is invalid")
            if (
                type(last_seen) not in (int, float)
                or not math.isfinite(last_seen)
                or last_seen < 0
            ):
                raise ValueError(
                    "persisted peer last_seen must be finite and non-negative"
                )

            validated_peers.append(
                {
                    "address": address,
                    "node_id": node_id,
                    "source": source,
                    "last_seen": last_seen,
                }
            )

        # Address policy depends on DNS and settings that can change between
        # restarts, so a peer failing it is dropped instead of voiding the state.
        # Filtering before canonicalization keeps a node's freshest allowed address.
        allowed = await asyncio.gather(
            *(
                is_allowed_peer_address(peer["address"], peer["source"])
                for peer in validated_peers
            )
        )
        allowed_peers = list(compress(validated_peers, allowed))
        if dropped_count := len(validated_peers) - len(allowed_peers):
            log.warning(
                "cometnet.state.peers_dropped",
                "Dropped persisted CometNet peers with disallowed addresses",
                failure_count=dropped_count,
            )

        canonical_peers, _ = canonicalize_persisted_peers(allowed_peers)
        known_peers = {peer["address"]: KnownPeer(**peer) for peer in canonical_peers}

        self._known_peers = known_peers
