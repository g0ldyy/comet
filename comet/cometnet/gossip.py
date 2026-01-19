"""
CometNet Gossip Module

Implements the epidemic gossip protocol for propagating torrent metadata
across the network.
"""

import asyncio
import time
from collections import deque
from typing import Awaitable, Callable, Deque, Dict, List, Optional, Set

from cachetools import TTLCache

from comet.cometnet.crypto import NodeIdentity
from comet.cometnet.protocol import TorrentAnnounce, TorrentMetadata
from comet.cometnet.reputation import ReputationStore
from comet.cometnet.validation import validate_message_security
from comet.core.logger import logger
from comet.core.models import settings


class MessageCache:
    """
    Cache of recently seen messages for deduplication.

    Each entry is keyed by (info_hash, updated_at) to prevent
    processing the same torrent announcement multiple times.
    """

    def __init__(
        self,
        ttl_seconds: float = None,
        max_size: int = None,
    ):
        self.ttl_seconds = ttl_seconds or settings.COMETNET_GOSSIP_CACHE_TTL
        self.max_size = max_size or settings.COMETNET_GOSSIP_CACHE_SIZE
        self._cache: TTLCache = TTLCache(maxsize=self.max_size, ttl=self.ttl_seconds)

    def is_seen(self, info_hash: str, updated_at: float) -> bool:
        """Check if we've seen this message recently."""
        return (info_hash, updated_at) in self._cache

    def mark_seen(self, info_hash: str, updated_at: float) -> None:
        """Mark a message as seen."""
        self._cache[(info_hash, updated_at)] = True

    def cleanup(self) -> int:
        """TTLCache auto-expires, but we can force expiration check."""
        self._cache.expire()
        return 0

    def __len__(self) -> int:
        return len(self._cache)


# Type for the callback to save a torrent to the database
SaveTorrentCallback = Callable[[TorrentMetadata], Awaitable[None]]

# Type for getting random peers
GetRandomPeersCallback = Callable[[int, Optional[Set[str]]], List[str]]

# Type for sending a message to peers
SendMessageCallback = Callable[[str, TorrentAnnounce], Awaitable[None]]

# Type for broadcasting a message
BroadcastCallback = Callable[[TorrentAnnounce, Optional[Set[str]]], Awaitable[None]]

# Type for disconnecting a peer
DisconnectPeerCallback = Callable[[str], Awaitable[None]]


class GossipEngine:
    """
    Implements the gossip protocol for propagating torrent metadata.

    Key features:
    - Fanout gossip (each node sends to a random subset of peers)
    - Message deduplication
    - Signature verification
    - Reputation-based filtering
    - Contribution modes (full/consumer/source/leech)
    - Pool-based trust filtering
    """

    # Valid contribution modes
    CONTRIBUTION_MODES = {"full", "consumer", "source", "leech"}

    def __init__(
        self,
        identity: NodeIdentity,
        reputation_store: ReputationStore,
        keystore=None,  # Optional PublicKeyStore for signature verification
        pool_store=None,  # PoolStore for trust filtering
    ):
        self.identity = identity
        self.reputation = reputation_store
        self._keystore = keystore
        self._pool_store = pool_store

        # Gossip parameters from settings
        self.fanout = settings.COMETNET_GOSSIP_FANOUT
        self.gossip_interval = settings.COMETNET_GOSSIP_INTERVAL
        self.message_ttl = settings.COMETNET_GOSSIP_MESSAGE_TTL
        self.max_torrents_per_message = (
            settings.COMETNET_GOSSIP_MAX_TORRENTS_PER_MESSAGE
        )

        # Contribution mode determines what we share/receive
        self.contribution_mode = settings.COMETNET_CONTRIBUTION_MODE or "full"
        if self.contribution_mode not in self.CONTRIBUTION_MODES:
            logger.warning(
                f"Invalid contribution mode '{self.contribution_mode}', defaulting to 'full'"
            )
            self.contribution_mode = "full"

        # Message deduplication cache
        self.seen_cache = MessageCache()

        # Queue of torrents waiting to be gossiped
        self._outgoing_queue: Deque[TorrentMetadata] = deque(maxlen=10000)

        # Callbacks (set by manager)
        self._get_random_peers: Optional[GetRandomPeersCallback] = None
        self._send_message: Optional[SendMessageCallback] = None
        self._broadcast: Optional[BroadcastCallback] = None
        self._save_torrent: Optional[SaveTorrentCallback] = None
        self._disconnect_peer: Optional[DisconnectPeerCallback] = None

        # Running state
        self._running = False
        self._gossip_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None

        # Statistics
        self.stats = {
            "torrents_received": 0,
            "torrents_propagated": 0,  # Original torrents from this node
            "torrents_repropagated": 0,  # Torrents received and forwarded to others
            "messages_sent": 0,
            "messages_received": 0,
            "invalid_messages": 0,
            "duplicates_ignored": 0,
            # stats
            "torrents_filtered_untrusted": 0,
            "torrents_filtered_blacklisted": 0,
            "torrents_skipped_mode": 0,
        }

    def set_callbacks(
        self,
        get_random_peers: GetRandomPeersCallback,
        send_message: SendMessageCallback,
        broadcast: BroadcastCallback,
        save_torrent: SaveTorrentCallback,
        disconnect_peer: Optional[DisconnectPeerCallback] = None,
    ) -> None:
        """Set the callbacks for network operations."""
        self._get_random_peers = get_random_peers
        self._send_message = send_message
        self._broadcast = broadcast
        self._save_torrent = save_torrent
        self._disconnect_peer = disconnect_peer

    async def start(self) -> None:
        """Start the gossip engine."""
        if self._running:
            return

        self._running = True

        # Start background tasks
        self._gossip_task = asyncio.create_task(self._gossip_loop())
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

        logger.log("COMETNET", "Gossip engine started")

    async def stop(self) -> None:
        """Stop the gossip engine."""
        self._running = False

        for task in [self._gossip_task, self._cleanup_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        logger.log("COMETNET", "Gossip engine stopped")

    async def queue_torrent(
        self, metadata: TorrentMetadata, pool_id: Optional[str] = None
    ) -> None:
        """
        Queue a torrent for gossiping.

        This is called when a new torrent is discovered locally
        (e.g., from a scraper).

        Args:
            metadata: The torrent metadata to broadcast
            pool_id: Optional pool to associate with this torrent
        """
        # Check contribution mode - only 'full' and 'source' can share
        if self.contribution_mode not in ("full", "source"):
            self.stats["torrents_skipped_mode"] += 1
            return

        # Sign the torrent with our identity
        metadata.contributor_id = self.identity.node_id
        metadata.contributor_public_key = self.identity.public_key_hex

        # Set pool_id if provided and we're a member
        if pool_id and self._pool_store:
            if self._pool_store.is_member_of(pool_id):
                metadata.pool_id = pool_id

        metadata.contributor_signature = await self.identity.sign_hex_async(
            metadata.to_signable_bytes()
        )

        # Record our own contribution
        if self._pool_store:
            await self._pool_store.record_contribution(
                contributor_public_key=self.identity.public_key_hex,
                pool_id=metadata.pool_id,
                count=1,
            )

        # Mark as seen to prevent re-gossiping our own announce
        self.seen_cache.mark_seen(metadata.info_hash, metadata.updated_at)

        # Add to outgoing queue
        self._outgoing_queue.append(metadata)

    async def handle_announce(
        self, sender_id: str, announce: TorrentAnnounce, sender_ip: str = None
    ) -> None:
        """
        Handle an incoming torrent announce message.

        This validates, processes, and optionally re-propagates the torrents.
        """
        self.stats["messages_received"] += 1

        # Check sender reputation
        if not self.reputation.is_peer_acceptable(sender_id):
            logger.debug(f"Ignoring announce from untrusted peer {sender_id[:8]}")
            self.stats["invalid_messages"] += 1
            if self._disconnect_peer:
                await self._disconnect_peer(sender_id)
            return

        # Validate message security (timestamp, sender_id, signature)
        if not await validate_message_security(
            announce, sender_id, self._keystore, self.reputation
        ):
            self.stats["invalid_messages"] += 1
            return

        peer_rep = self.reputation.get_or_create(sender_id)
        peer_rep.messages_received += 1
        peer_rep.update_seen()

        valid_torrents = []
        torrents_to_repropagate = []

        for torrent in announce.torrents:
            # Check deduplication
            if self.seen_cache.is_seen(torrent.info_hash, torrent.updated_at):
                self.stats["duplicates_ignored"] += 1
                continue

            # Skip torrents with invalid size (0) without penalizing the peer
            # This handles cases where scrapers might send incomplete metadata
            if torrent.size == 0:
                self.stats["invalid_messages"] += 1
                continue

            # Validate torrent structure
            if not self._validate_torrent(torrent):
                peer_rep.add_invalid_contribution()
                self.stats["invalid_messages"] += 1
                continue

            # Mark as seen
            self.seen_cache.mark_seen(torrent.info_hash, torrent.updated_at)

            # Require valid signature and public key on all torrents
            if (
                not torrent.contributor_id
                or not torrent.contributor_signature
                or not torrent.contributor_public_key
            ):
                logger.debug(f"Rejecting incomplete torrent {torrent.info_hash}")
                peer_rep.add_invalid_contribution()
                self.stats["invalid_messages"] += 1
                continue

            # Verify that public key matches contributor_id
            derived_id = NodeIdentity.node_id_from_public_key(
                torrent.contributor_public_key
            )
            if derived_id != torrent.contributor_id:
                logger.debug(
                    f"Contributor ID mismatch for {torrent.info_hash}: "
                    f"claimed {torrent.contributor_id[:8]}, derived {derived_id[:8]}"
                )
                peer_rep.add_invalid_contribution()
                self.stats["invalid_messages"] += 1
                continue

            # Verify contributor signature using the provided public key
            if not await NodeIdentity.verify_hex_async(
                torrent.to_signable_bytes(),
                torrent.contributor_signature,
                torrent.contributor_public_key,
            ):
                logger.debug(
                    f"Invalid contributor signature on torrent {torrent.info_hash}"
                )
                peer_rep.add_invalid_contribution()
                self.stats["invalid_messages"] += 1
                continue

            # Check if contributor is trusted (pool-based filtering)
            if self._pool_store:
                if not self._pool_store.is_contributor_trusted(
                    torrent.contributor_public_key, torrent.pool_id
                ):
                    self.stats["torrents_filtered_untrusted"] += 1
                    continue

            # Store the contributor key in our keystore for future reference (optional)
            if self._keystore:
                self._keystore.store_key(
                    node_id=torrent.contributor_id,
                    public_key_hex=torrent.contributor_public_key,
                    verified=True,
                )

            valid_torrents.append(torrent)

            # Add to re-propagation list if TTL allows
            # Only repropagate if contribution mode allows (full or consumer)
            if announce.ttl > 1 and self.contribution_mode in ("full", "consumer"):
                torrents_to_repropagate.append(torrent)

        # Update reputation for valid contributions
        if valid_torrents:
            peer_rep.add_valid_contribution(len(valid_torrents))

        # Record contributions in pool store (track member stats)
        if valid_torrents and self._pool_store:
            # Group by contributor to batch the updates
            contributions: Dict[tuple, int] = {}
            for torrent in valid_torrents:
                key = (torrent.contributor_public_key, torrent.pool_id)
                contributions[key] = contributions.get(key, 0) + 1

            # Record each contributor's contributions
            for (contributor_key, pool_id), count in contributions.items():
                await self._pool_store.record_contribution(
                    contributor_public_key=contributor_key,
                    pool_id=pool_id,
                    count=count,
                )

        # Save valid torrents to database
        # Only save if contribution mode allows receiving (not 'source')
        if self._save_torrent and valid_torrents and self.contribution_mode != "source":
            saved_count = 0
            for torrent in valid_torrents:
                try:
                    await self._save_torrent(torrent)
                    self.stats["torrents_received"] += 1
                    saved_count += 1
                except Exception as e:
                    logger.debug(f"Failed to save torrent {torrent.info_hash}: {e}")

            if saved_count > 0:
                logger.log(
                    "COMETNET",
                    f"Received {saved_count} torrents from peer {sender_id[:12]}...",
                )

        # Re-propagate to other peers (with reduced TTL)
        if torrents_to_repropagate and self._get_random_peers and self._send_message:
            peers_reached = await self._repropagate(
                torrents_to_repropagate, announce.ttl - 1, exclude={sender_id}
            )
            # Track re-propagations separately from original contributions
            if peers_reached > 0:
                self.stats["torrents_repropagated"] += len(torrents_to_repropagate)

        # Check if peer is still acceptable after processing
        if not self.reputation.is_peer_acceptable(sender_id):
            logger.debug(f"Peer {sender_id[:8]} became unacceptable, disconnecting")
            if self._disconnect_peer:
                await self._disconnect_peer(sender_id)

    def _validate_torrent(self, torrent: TorrentMetadata) -> bool:
        """
        Validate a torrent's metadata.

        Returns True if the torrent is valid.
        """
        # Basic validation (Pydantic already does field-level validation)
        try:
            # Verify info_hash format
            if len(torrent.info_hash) != 40:
                logger.debug(
                    f"Torrent validation failed: info_hash length {len(torrent.info_hash)} != 40"
                )
                return False
            int(torrent.info_hash, 16)

            # Title should be non-empty
            if not torrent.title or len(torrent.title) < 1:
                logger.debug("Torrent validation failed: empty title")
                return False

            # Size should be positive
            if torrent.size <= 0:
                logger.debug(f"Torrent validation failed: invalid size {torrent.size}")
                return False

            # Tracker should be non-empty
            if not torrent.tracker:
                logger.debug("Torrent validation failed: empty tracker")
                return False

            # Timestamp should be reasonable
            now = time.time()
            if (
                torrent.updated_at
                > now + settings.COMETNET_GOSSIP_VALIDATION_FUTURE_TOLERANCE
            ):  # Future tolerance
                logger.debug(
                    f"Torrent validation failed: timestamp in future ({torrent.updated_at} > {now})"
                )
                return False
            if (
                torrent.updated_at < now - settings.COMETNET_GOSSIP_TORRENT_MAX_AGE
            ):  # Max age
                logger.debug(
                    f"Torrent validation failed: timestamp too old ({torrent.updated_at} < {now - settings.COMETNET_GOSSIP_TORRENT_MAX_AGE})"
                )
                return False

            return True

        except (ValueError, TypeError) as e:
            logger.debug(f"Torrent validation failed: {e}")
            return False

    async def _repropagate(
        self,
        torrents: List[TorrentMetadata],
        ttl: int,
        exclude: Optional[Set[str]] = None,
    ) -> int:
        """Re-propagate torrents to random peers. Returns the number of peers reached."""
        if not self._get_random_peers or not self._send_message:
            return 0

        # Select random peers
        peers = self._get_random_peers(self.fanout, exclude)

        if not peers:
            return 0

        # Create announce message
        announce = TorrentAnnounce(
            sender_id=self.identity.node_id,
            torrents=torrents,
            ttl=ttl,
        )
        announce.signature = await self.identity.sign_hex_async(
            announce.to_signable_bytes()
        )

        # Send to all selected peers
        async def send_to_peer(peer_id: str) -> bool:
            try:
                await self._send_message(peer_id, announce)
                return True
            except Exception as e:
                logger.debug(f"Failed to send to peer {peer_id[:8]}: {e}")
                return False

        results = await asyncio.gather(
            *(send_to_peer(peer_id) for peer_id in peers),
            return_exceptions=True,
        )
        successful_sends = sum(1 for r in results if r is True)
        self.stats["messages_sent"] += successful_sends
        return successful_sends

    async def _gossip_loop(self) -> None:
        """Main gossip loop - periodically sends queued torrents."""
        while self._running:
            try:
                await asyncio.sleep(self.gossip_interval)

                # Check if we have torrents to gossip
                if not self._outgoing_queue:
                    continue

                total_sent = 0
                while self._outgoing_queue:
                    # Take up to MAX_TORRENTS_PER_MESSAGE from queue
                    to_send: List[TorrentMetadata] = []
                    for _ in range(
                        min(self.max_torrents_per_message, len(self._outgoing_queue))
                    ):
                        to_send.append(self._outgoing_queue.popleft())

                    # Create and send announce
                    if self._get_random_peers and self._send_message and to_send:
                        peers_reached = await self._repropagate(
                            to_send, self.message_ttl
                        )
                        # Only count torrents as propagated if at least one peer received them
                        if peers_reached > 0:
                            self.stats["torrents_propagated"] += len(to_send)
                            total_sent += len(to_send)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Gossip loop error: {e}")

    async def _cleanup_loop(self) -> None:
        """Periodically clean up the message cache and keystore."""
        while self._running:
            try:
                await asyncio.sleep(60.0)

                # Cleanup message cache
                removed = self.seen_cache.cleanup()
                if removed > 0:
                    logger.debug(f"Cleaned up {removed} expired cache entries")

                # Cleanup old keys from keystore
                if self._keystore:
                    keys_removed = self._keystore.cleanup_old_keys(max_age_days=30.0)
                    if keys_removed > 0:
                        logger.debug(f"Cleaned up {keys_removed} old public keys")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Cleanup loop error: {e}")

    def get_stats(self) -> Dict:
        """Get gossip statistics."""
        return {
            **self.stats,
            "queue_size": len(self._outgoing_queue),
            "cache_size": len(self.seen_cache),
        }

    def to_dict(self) -> Dict:
        """Serialize the gossip engine state for persistence."""
        return {
            "stats": self.stats,
        }

    def from_dict(self, data: Dict) -> None:
        """Load the gossip engine state from a dictionary."""
        if "stats" in data:
            # Update stats but preserve keys that might be missing in older state files
            # or add new keys that are present in the current code
            loaded_stats = data["stats"]
            for key, value in loaded_stats.items():
                if key in self.stats:
                    self.stats[key] = value
