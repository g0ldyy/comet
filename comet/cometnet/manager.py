"""
CometNet Service Manager

Main entry point for CometNet functionality.
Orchestrates all components: Identity, Transport, Discovery, Gossip, Reputation, Pools, and Contribution Modes.
"""

import asyncio
import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from comet.cometnet.crypto import NodeIdentity
from comet.cometnet.discovery import DiscoveryService
from comet.cometnet.gossip import GossipEngine
from comet.cometnet.interface import CometNetBackend
from comet.cometnet.keystore import PublicKeyStore
from comet.cometnet.nat import UPnPManager
from comet.cometnet.pools import PoolStore
from comet.cometnet.protocol import (AnyMessage, MessageType, PeerRequest,
                                     PeerResponse, PoolJoinRequest,
                                     PoolManifestMessage, PoolMemberUpdate,
                                     TorrentAnnounce, TorrentMetadata)
from comet.cometnet.reputation import ReputationStore
from comet.cometnet.transport import ConnectionManager
from comet.core.logger import logger
from comet.core.models import settings


class CometNetService(CometNetBackend):
    """
    Main CometNet service that manages the P2P network.

    This is the primary interface for the rest of Comet to interact with
    the CometNet P2P layer.
    """

    STATE_FILE = "cometnet_state.json"

    def __init__(
        self,
        enabled: bool = False,
        listen_port: int = 8765,
        bootstrap_nodes: Optional[List[str]] = None,
        manual_peers: Optional[List[str]] = None,
        max_peers: int = None,
        min_peers: int = None,
        keys_dir: Optional[str] = None,
        advertise_url: Optional[str] = None,
    ):
        self.enabled = enabled
        self.listen_port = listen_port
        self.bootstrap_nodes = bootstrap_nodes or []
        self.manual_peers = manual_peers or []
        self.max_peers = max_peers or settings.COMETNET_MAX_PEERS
        self.min_peers = min_peers or settings.COMETNET_MIN_PEERS
        self.keys_dir = Path(keys_dir) if keys_dir else Path("data/cometnet")
        self.advertise_url = advertise_url

        # Core components (initialized in start())
        self.identity: Optional[NodeIdentity] = None
        self.transport: Optional[ConnectionManager] = None
        self.discovery: Optional[DiscoveryService] = None
        self.gossip: Optional[GossipEngine] = None
        self.reputation: Optional[ReputationStore] = None
        self.keystore: Optional[PublicKeyStore] = None
        self.upnp: Optional[UPnPManager] = None

        # components
        self.pool_store: Optional[PoolStore] = None

        # Callback for saving torrents to database
        self._save_torrent_callback = None

        # Running state
        self._running = False
        self._started_at: Optional[float] = None

    @property
    def running(self) -> bool:
        """Check if the service is running (interface implementation)."""
        return self._running

    def set_save_torrent_callback(self, callback) -> None:
        """
        Set the callback for saving torrents received from the network.

        The callback should be an async function that takes a TorrentMetadata
        and saves it to the database.
        """
        self._save_torrent_callback = callback

    async def start(self) -> None:
        """Start the CometNet service."""
        if not self.enabled:
            logger.log("COMETNET", "CometNet is disabled")
            return

        if self._running:
            return

        logger.log("COMETNET", "Starting CometNet P2P network...")

        # Initialize components
        self._init_components()

        # Load saved state
        await self._load_state()

        # Load pools data
        if self.pool_store:
            await self.pool_store.load()

        # Start transport layer
        await self.transport.start()

        # Handle UPnP if enabled
        if settings.COMETNET_UPNP_ENABLED:
            logger.log("COMETNET", "Initializing UPnP...")
            self.upnp = UPnPManager(
                port=self.listen_port,
                lease_duration=settings.COMETNET_UPNP_LEASE_DURATION or 3600,
            )
            external_ip = await self.upnp.start()
            if external_ip:
                # If we successfully mapped a port and don't have an advertise URL, use the IP
                if not self.advertise_url:
                    self.advertise_url = f"ws://{external_ip}:{self.listen_port}"
                    # Update transport with new URL
                    self.transport.advertise_url = self.advertise_url
                    logger.log(
                        "COMETNET",
                        f"Public Address auto-configured via UPnP: {self.advertise_url}",
                    )
                else:
                    logger.warning(
                        "COMETNET",
                        f"UPnP mapped to {external_ip} but COMETNET_ADVERTISE_URL is already set. Using configured URL.",
                    )

        # Start discovery service
        await self.discovery.start(self.identity.node_id, self.listen_port)

        # Custom check for unencrypted transport
        if self.advertise_url and self.advertise_url.startswith("ws://"):
            allowed_hosts = ["localhost", "127.0.0.1", "0.0.0.0", "::1"]
            is_local = any(host in self.advertise_url for host in allowed_hosts)

            if not is_local:
                logger.warning(
                    "SECURITY WARNING: CometNet is configured with unencrypted 'ws://' URL. "
                    "Your P2P traffic (including metadata) is visible to interceptors. "
                    "It is STRONGLY recommended to use 'wss://' (SSL) for public instances."
                )

        # Start gossip engine
        await self.gossip.start()

        self._running = True
        self._started_at = time.time()

        # Reconnect to known pool peers (from previous sessions)
        await self._reconnect_pool_peers()

        # Log contribution mode and pool info
        mode = settings.COMETNET_CONTRIBUTION_MODE or "full"
        pool_count = len(self.pool_store.get_subscriptions()) if self.pool_store else 0
        pool_info = (
            f", subscribed to {pool_count} pools" if pool_count > 0 else ", open mode"
        )

        logger.log(
            "COMETNET",
            f"CometNet started - Node ID: {self.identity.node_id[:16]}... "
            f"(mode: {mode}{pool_info})",
        )

    async def stop(self) -> None:
        """Stop the CometNet service."""
        if not self._running:
            return

        logger.log("COMETNET", "Stopping CometNet...")

        self._running = False

        # Save state before stopping
        await self._save_state()

        # Save pools data
        if self.pool_store:
            await self.pool_store.save()

        # Stop components in reverse order
        if self.gossip:
            await self.gossip.stop()

        if self.discovery:
            await self.discovery.stop()

        if self.transport:
            await self.transport.stop()

        if self.upnp:
            self.upnp.stop()

        logger.log("COMETNET", "CometNet stopped")

    async def _reconnect_pool_peers(self) -> None:
        """
        Reconnect to known peers for pools we're a member of.

        This runs on startup to re-establish connections to pool members
        from previous sessions.
        """
        if not self.pool_store:
            return

        pool_peers = self.pool_store.get_all_pool_peers()
        if not pool_peers:
            return

        total_peers = sum(len(peers) for peers in pool_peers.values())
        if total_peers == 0:
            return

        logger.debug(f"Reconnecting to {total_peers} known pool peers...")
        asyncio.create_task(self._connect_to_pool_peers(pool_peers))

    async def _connect_to_pool_peers(self, pool_peers: Dict[str, Set[str]]) -> None:
        """Background task to connect to pool peers."""
        connected = 0
        connected_peers: List[str] = []  # Track connected peer IDs for manifest sync

        for pool_id, peers in pool_peers.items():
            for peer_addr in peers:
                try:
                    # Check if already connected to this address
                    already_connected = False
                    for nid, addr in self.transport.get_peer_addresses().items():
                        if addr == peer_addr or addr.rstrip("/") == peer_addr.rstrip(
                            "/"
                        ):
                            already_connected = True
                            break

                    if already_connected:
                        continue

                    # Try to connect
                    peer_id = await self.transport.connect_to_peer(peer_addr)
                    if peer_id:
                        connected += 1
                        connected_peers.append(peer_id)
                        logger.debug(
                            f"Reconnected to pool peer {peer_id[:16]}... for pool {pool_id}"
                        )
                        # Small delay between connections
                        await asyncio.sleep(0.5)
                except Exception as e:
                    logger.debug(f"Failed to reconnect to {peer_addr}: {e}")

        if connected > 0:
            logger.log("COMETNET", f"Reconnected to {connected} pool peers")

            # Send our manifests to newly connected peers to trigger sync
            # This ensures we receive their updated manifests if they have newer versions
            await self._sync_manifests_with_peers(connected_peers)

    def _init_components(self) -> None:
        """Initialize all CometNet components."""
        # Ensure keys directory exists
        self.keys_dir.mkdir(parents=True, exist_ok=True)

        # Initialize identity
        self.identity = NodeIdentity(keys_dir=self.keys_dir)
        self.identity.load_or_generate()

        # Initialize reputation store
        self.reputation = ReputationStore()

        # Initialize public key store
        self.keystore = PublicKeyStore()

        # Initialize pool store
        self.pool_store = PoolStore(pools_dir=settings.COMETNET_POOLS_DIR)

        # Initialize transport
        self.transport = ConnectionManager(
            identity=self.identity,
            listen_port=self.listen_port,
            max_peers=self.max_peers,
            advertise_url=self.advertise_url,
            keystore=self.keystore,
        )

        # Initialize discovery
        self.discovery = DiscoveryService(
            manual_peers=self.manual_peers,
            bootstrap_nodes=self.bootstrap_nodes,
            min_peers=self.min_peers,
            max_peers=self.max_peers,
        )

        # Initialize gossip engine with stores
        self.gossip = GossipEngine(
            identity=self.identity,
            reputation_store=self.reputation,
            keystore=self.keystore,
            pool_store=self.pool_store,
        )

        # Wire up callbacks
        self._setup_callbacks()

    def _setup_callbacks(self) -> None:
        """Set up callbacks between components."""
        # Discovery callbacks
        self.discovery.set_callbacks(
            connect_callback=self.transport.connect_to_peer,
            get_connected_count=lambda: self.transport.connected_peer_count,
            get_connected_ids=lambda: self.transport.connected_node_ids,
            send_message_callback=self.transport.send_to_peer,
            sign_callback=self.identity.sign_hex_async,  # For signing PeerRequest messages
        )

        # Transport callback to notify Discovery when a peer connects
        self.transport.set_on_peer_connected(self._on_peer_connected)

        # Gossip callbacks
        self.gossip.set_callbacks(
            get_random_peers=self.transport.get_random_peers,
            send_message=self._send_gossip_message,
            broadcast=self._broadcast_gossip,
            save_torrent=self._handle_received_torrent,
            disconnect_peer=self.transport.disconnect_peer,
        )

        # Transport message handlers
        self.transport.register_handler(
            MessageType.TORRENT_ANNOUNCE, self._handle_torrent_announce
        )
        self.transport.register_handler(
            MessageType.PEER_REQUEST, self._handle_peer_request
        )
        self.transport.register_handler(
            MessageType.PEER_RESPONSE, self._handle_peer_response
        )
        # Pool message handlers
        self.transport.register_handler(
            MessageType.POOL_MANIFEST, self._handle_pool_manifest
        )
        self.transport.register_handler(
            MessageType.POOL_JOIN_REQUEST, self._handle_pool_join_request
        )
        self.transport.register_handler(
            MessageType.POOL_MEMBER_UPDATE, self._handle_pool_member_update
        )
        self.transport.register_handler(
            MessageType.POOL_DELETE, self._handle_pool_delete
        )

    async def _send_gossip_message(
        self, peer_id: str, message: TorrentAnnounce
    ) -> None:
        """Send a gossip message to a specific peer."""
        await self.transport.send_to_peer(peer_id, message)

    async def _broadcast_gossip(
        self, message: TorrentAnnounce, exclude: Optional[Set[str]] = None
    ) -> None:
        """Broadcast a gossip message to all peers."""
        await self.transport.broadcast(message, exclude)

    async def _handle_received_torrent(self, metadata: TorrentMetadata) -> None:
        """Handle a torrent received from the network."""
        if self._save_torrent_callback:
            try:
                await self._save_torrent_callback(metadata)
            except Exception as e:
                logger.debug(f"Failed to save torrent from network: {e}")

    async def _handle_torrent_announce(
        self, sender_id: str, message: AnyMessage
    ) -> None:
        """Handle incoming torrent announce messages."""
        if isinstance(message, TorrentAnnounce):
            sender_ip = self.transport.get_peer_address(sender_id)
            await self.gossip.handle_announce(sender_id, message, sender_ip)

    async def _handle_peer_request(self, sender_id: str, message: AnyMessage) -> None:
        """Handle incoming peer request messages."""
        if isinstance(message, PeerRequest):
            # Verify sender_id matches (prevent spoofing)
            if message.sender_id and message.sender_id != sender_id:
                logger.debug(f"PeerRequest sender_id mismatch from {sender_id[:16]}")
                return

            # Verify signature if present and we have the key
            if message.signature and self.keystore:
                sender_key = self.keystore.get_key(sender_id)
                if sender_key:
                    if not await NodeIdentity.verify_hex_async(
                        message.to_signable_bytes(),
                        message.signature,
                        sender_key,
                    ):
                        logger.debug(
                            f"Invalid PeerRequest signature from {sender_id[:16]}"
                        )
                        return

            response = await self.discovery.handle_peer_request(sender_id, message)
            response.sender_id = self.identity.node_id
            response.signature = await self.identity.sign_hex_async(
                response.to_signable_bytes()
            )
            await self.transport.send_to_peer(sender_id, response)

    async def _handle_peer_response(self, sender_id: str, message: AnyMessage) -> None:
        """Handle incoming peer response messages."""
        if isinstance(message, PeerResponse):
            # Verify sender_id matches (prevent spoofing)
            if message.sender_id and message.sender_id != sender_id:
                logger.warning(
                    f"PeerResponse sender_id mismatch: expected {sender_id[:16]}, "
                    f"got {message.sender_id[:16]}"
                )
                return

            # Verify signature if present and we have the key
            if message.signature and self.keystore:
                sender_key = self.keystore.get_key(sender_id)
                if sender_key:
                    if not await NodeIdentity.verify_hex_async(
                        message.to_signable_bytes(),
                        message.signature,
                        sender_key,
                    ):
                        logger.warning(
                            f"Invalid PeerResponse signature from {sender_id[:16]}"
                        )
                        # Apply reputation penalty for invalid signature
                        if self.reputation:
                            peer_rep = self.reputation.get_or_create(sender_id)
                            peer_rep.add_signature_failure_penalty()
                        return

            new_peers = self.discovery.handle_peer_response(message)
            if new_peers > 0:
                logger.debug(
                    f"Received {new_peers} new peers via PEX from {sender_id[:16]}"
                )

    async def _handle_pool_manifest(self, sender_id: str, message: AnyMessage) -> None:
        """Handle incoming pool manifest messages."""
        if not isinstance(message, PoolManifestMessage):
            return

        if not self.pool_store:
            return

        from comet.cometnet.pools import (JoinMode, MemberRole, PoolManifest,
                                          PoolMember)

        # Convert message to PoolManifest
        try:
            members = [
                PoolMember(
                    public_key=m.get("public_key", ""),
                    role=MemberRole(m.get("role", "member")),
                    added_at=m.get("added_at", 0),
                    added_by=m.get("added_by", ""),
                )
                for m in message.members
            ]

            manifest = PoolManifest(
                pool_id=message.pool_id,
                display_name=message.display_name,
                description=message.description,
                creator_key=message.creator_key,
                members=members,
                join_mode=JoinMode(message.join_mode),
                version=message.version,
                created_at=message.created_at,
                updated_at=message.updated_at,
                signatures=message.manifest_signatures,
            )

            # Check if we already have this pool with same or newer version
            existing = self.pool_store.get_manifest(message.pool_id)
            if existing and existing.version >= manifest.version:
                # We have the same or newer version - send ours back to help the sender sync
                if existing.version > manifest.version:
                    try:
                        await self._send_pool_manifest(sender_id, existing)
                        logger.debug(
                            f"Sent newer manifest v{existing.version} to {sender_id[:16]} "
                            f"(they had v{manifest.version})"
                        )
                    except Exception as e:
                        logger.debug(f"Failed to send updated manifest: {e}")
                return

            # Store the manifest (validation happens inside)
            if self.pool_store.validate_manifest(manifest, self.keystore):
                await self.pool_store.store_manifest(manifest)

                # Update our membership status based on the new manifest
                my_key = self.identity.public_key_hex if self.identity else None
                if my_key:
                    was_member = self.pool_store.is_member_of(message.pool_id)
                    is_now_member = manifest.is_member(my_key)

                    if was_member and not is_now_member:
                        # We were removed from this pool - clean up completely
                        self.pool_store._memberships.discard(message.pool_id)
                        self.pool_store._subscriptions.discard(message.pool_id)

                        # Remove the manifest since we're no longer a member
                        if message.pool_id in self.pool_store._manifests:
                            del self.pool_store._manifests[message.pool_id]

                        # Remove pool peers for this pool
                        if message.pool_id in self.pool_store._pool_peers:
                            del self.pool_store._pool_peers[message.pool_id]

                        # Remove any invites we had for this pool
                        if message.pool_id in self.pool_store._invites:
                            del self.pool_store._invites[message.pool_id]

                        # Delete manifest file
                        manifest_path = (
                            self.pool_store.manifests_dir / f"{message.pool_id}.json"
                        )
                        try:
                            manifest_path.unlink(missing_ok=True)
                        except Exception:
                            pass

                        # Delete invites directory for this pool
                        pool_inv_dir = self.pool_store.invites_dir / message.pool_id
                        if pool_inv_dir.exists():
                            try:
                                import shutil

                                shutil.rmtree(pool_inv_dir)
                            except Exception:
                                pass

                        await self.pool_store._save_memberships()
                        await self.pool_store._save_subscriptions()
                        await self.pool_store._save_pool_peers()

                        logger.log(
                            "COMETNET",
                            f"Removed from pool {message.pool_id} (kicked by admin) - pool data cleaned up",
                        )
                        return  # Don't store anything else for this pool
                    elif not was_member and is_now_member:
                        # We were added to this pool (e.g., via invite on another node)
                        self.pool_store._memberships.add(message.pool_id)
                        await self.pool_store._save_memberships()
                        logger.log(
                            "COMETNET",
                            f"Added to pool {message.pool_id}",
                        )
                    elif was_member and is_now_member:
                        # Check for role changes
                        old_member = existing.get_member(my_key) if existing else None
                        new_member = manifest.get_member(my_key)
                        if (
                            old_member
                            and new_member
                            and old_member.role != new_member.role
                        ):
                            logger.log(
                                "COMETNET",
                                f"Role updated in pool {message.pool_id}: {old_member.role} -> {new_member.role}",
                            )

                # Store the sender's address so we can reconnect later
                sender_addr = self.transport.get_peer_address(sender_id)
                if sender_addr:
                    self.pool_store.add_pool_peer(message.pool_id, sender_addr)
                    await self.pool_store._save_pool_peers()

                logger.log(
                    "COMETNET",
                    f"Received pool manifest: {message.display_name} ({message.pool_id}) v{message.version}",
                )
            else:
                logger.debug(f"Invalid pool manifest from {sender_id[:16]}")

        except Exception as e:
            logger.debug(f"Failed to process pool manifest: {e}")

    async def _handle_pool_join_request(
        self, sender_id: str, message: AnyMessage
    ) -> None:
        """
        Handle incoming pool join requests.

        When a node wants to join a pool using an invite, they send this request
        to the admin node (specified in the invite link).
        """
        if not isinstance(message, PoolJoinRequest):
            return

        if not self.pool_store:
            return

        pool_id = message.pool_id
        invite_code = message.invite_code
        requester_key = message.requester_key

        # Get the invite
        invite = self.pool_store.get_invite(pool_id, invite_code)
        if not invite or not invite.is_valid():
            logger.debug(f"Invalid or expired invite code from {sender_id[:16]}")
            return

        # Get the manifest
        manifest = self.pool_store.get_manifest(pool_id)
        if not manifest:
            logger.debug(f"Pool {pool_id} not found for join request")
            return

        # Check if already a member
        if manifest.is_member(requester_key):
            # Already a member, just send them the manifest
            pass
        else:
            # Add as member
            from comet.cometnet.pools import MemberRole, PoolMember

            manifest.members.append(
                PoolMember(
                    public_key=requester_key,
                    role=MemberRole.MEMBER,
                    added_by=invite.created_by,
                )
            )
            manifest.version += 1
            manifest.updated_at = time.time()

            # Increment invite usage
            invite.uses += 1
            await self.pool_store._save_invite(invite)

            # Save updated manifest
            await self.pool_store.store_manifest(manifest, self.identity)

            logger.log(
                "COMETNET",
                f"Added {requester_key[:16]}... to pool {pool_id} via join request",
            )

        # Send the manifest back to the requester
        await self._send_pool_manifest(sender_id, manifest)

        # Broadcast update to all pool members (optimized)
        await self._broadcast_pool_member_update(
            pool_id=pool_id,
            action="add",
            member_key=requester_key,
            updated_by=invite.created_by,
            manifest_signatures=manifest.signatures,
            exclude={sender_id},
        )

    async def _handle_pool_member_update(
        self, sender_id: str, message: AnyMessage
    ) -> None:
        """Handle incoming pool member updates (delta updates)."""
        if not isinstance(message, PoolMemberUpdate):
            return

        if not self.pool_store:
            return

        from comet.cometnet.pools import MemberRole, PoolMember

        manifest = self.pool_store.get_manifest(message.pool_id)
        if not manifest:
            return

        # Verify the updater is an admin
        if not manifest.is_admin(message.updated_by):
            logger.debug(
                f"Received pool update from non-admin {message.updated_by[:16]}"
            )
            return

        # Verify signature of the update message
        # (This prevents spoofing the update)
        if self.keystore:
            # Check if we have the admin's key (we should, if they are in the manifest)
            # Or use message.updated_by as key?
            # Creating node identity just to verify
            if not await NodeIdentity.verify_hex_async(
                message.to_signable_bytes(), message.signature, message.updated_by
            ):
                logger.warning(
                    f"Invalid signature on PoolMemberUpdate from {sender_id[:16]}"
                )
                return

        # Apply update tentatively
        target_member = manifest.get_member(message.member_key)

        modified = False
        if message.action == "add":
            if not target_member:
                manifest.members.append(
                    PoolMember(
                        public_key=message.member_key,
                        role=MemberRole(message.new_role)
                        if message.new_role
                        else MemberRole.MEMBER,
                        added_by=message.updated_by,
                        added_at=message.timestamp,
                    )
                )
                modified = True
        elif message.action == "remove":
            if target_member:
                manifest.members = [
                    m for m in manifest.members if m.public_key != message.member_key
                ]
                modified = True
        elif message.action == "promote":
            if target_member:
                target_member.role = MemberRole.ADMIN
                modified = True
        elif message.action == "demote":
            if target_member:
                target_member.role = MemberRole.MEMBER
                modified = True

        if not modified:
            return

        # Update version (we assume it increments by 1)
        manifest.version += 1
        manifest.updated_at = message.timestamp

        # Verify that our new state matches the signatures provided by admin
        # This is the critical step: ensuring our strict determinism matches the admin's
        manifest_valid = False
        manifest.signatures = {}  # modify for to_signable_bytes check? No, to_signable_bytes excludes signatures.

        signable = manifest.to_signable_bytes()

        # Check carefully if any provided signature validates our new state
        for admin_key, sig in message.manifest_signatures.items():
            if NodeIdentity.verify_hex(signable, sig, admin_key):
                manifest_valid = True
                # Adopt the new signatures
                manifest.signatures = message.manifest_signatures
                break

        if manifest_valid:
            await self.pool_store.store_manifest(manifest)
            logger.log(
                "COMETNET",
                f"Applied pool update: {message.action} {message.member_key[:8]}...",
            )

            # Re-broadcast to others who might not have received it
            # (Gossip protocol usually handles this, but here we do direct broadcast)
            # Ideally we should only forward if we just applied it
            await self.transport.broadcast(message, exclude={sender_id})
        else:
            logger.warning(
                f"Computed manifest state for {message.pool_id} does not match admin signature. "
                "Requesting full manifest..."
            )
            # Fallback: Request full manifest
            # (Not implemented here, but we could send a PoolJoinRequest or similar,
            # or just wait for next full sync)

    async def _handle_pool_delete(self, sender_id: str, message: AnyMessage) -> None:
        """Handle incoming pool deletion messages."""
        from comet.cometnet.protocol import PoolDeleteMessage

        if not isinstance(message, PoolDeleteMessage):
            return

        if not self.pool_store:
            return

        # Verify the deletion is from the pool creator
        manifest = self.pool_store.get_manifest(message.pool_id)
        if not manifest:
            return  # We don't have this pool, nothing to delete

        # Only accept deletion from the creator
        if manifest.creator_key != message.deleted_by:
            logger.warning(
                f"Received pool deletion for {message.pool_id} from non-creator {message.deleted_by[:16]}"
            )
            return

        # Verify the signature
        from comet.cometnet.crypto import NodeIdentity

        if not NodeIdentity.verify_hex(
            message.to_signable_bytes(), message.signature, message.deleted_by
        ):
            logger.warning(
                f"Invalid signature on pool deletion message for {message.pool_id}"
            )
            return

        # Delete the pool locally
        await self.pool_store.delete_pool(message.pool_id)
        logger.log(
            "COMETNET",
            f"Pool {message.pool_id} deleted by creator {message.deleted_by[:16]}",
        )

    async def _send_pool_manifest(self, peer_id: str, manifest) -> None:
        """Send a pool manifest to a specific peer."""
        msg = PoolManifestMessage(
            sender_id=self.identity.node_id,
            pool_id=manifest.pool_id,
            display_name=manifest.display_name,
            description=manifest.description,
            creator_key=manifest.creator_key,
            members=[m.model_dump() for m in manifest.members],
            join_mode=manifest.join_mode.value,
            version=manifest.version,
            created_at=manifest.created_at,
            updated_at=manifest.updated_at,
            manifest_signatures=manifest.signatures,
        )
        msg.signature = await self.identity.sign_hex_async(msg.to_signable_bytes())
        await self.transport.send_to_peer(peer_id, msg)

    async def _broadcast_pool_manifest(
        self, manifest, exclude: Optional[Set[str]] = None
    ) -> None:
        """Broadcast a pool manifest to all connected peers."""
        msg = PoolManifestMessage(
            sender_id=self.identity.node_id,
            pool_id=manifest.pool_id,
            display_name=manifest.display_name,
            description=manifest.description,
            creator_key=manifest.creator_key,
            members=[m.model_dump() for m in manifest.members],
            join_mode=manifest.join_mode.value,
            version=manifest.version,
            created_at=manifest.created_at,
            updated_at=manifest.updated_at,
            manifest_signatures=manifest.signatures,
        )
        msg.signature = await self.identity.sign_hex_async(msg.to_signable_bytes())
        await self.transport.broadcast(msg, exclude)

    async def _broadcast_pool_member_update(
        self,
        pool_id: str,
        action: str,
        member_key: str,
        updated_by: str,
        manifest_signatures: Dict[str, str],
        new_role: Optional[str] = None,
        exclude: Optional[Set[str]] = None,
    ) -> None:
        """Broadcast a pool member update (delta)."""
        msg = PoolMemberUpdate(
            sender_id=self.identity.node_id,
            pool_id=pool_id,
            action=action,
            member_key=member_key,
            updated_by=updated_by,
            new_role=new_role,
            manifest_signatures=manifest_signatures,
        )
        msg.signature = await self.identity.sign_hex_async(msg.to_signable_bytes())
        await self.transport.broadcast(msg, exclude)

    async def broadcast_torrent(self, metadata) -> None:
        """
        Broadcast a torrent to the network.

        This is the main method for sharing newly discovered torrents.
        Should be called when a scraper discovers a new torrent.
        Accepts both TorrentMetadata objects and dicts.
        """
        if not self._running or not self.gossip:
            return

        # Convert dict to TorrentMetadata if needed
        if isinstance(metadata, dict):
            metadata = TorrentMetadata(
                info_hash=metadata.get("info_hash", "").lower(),
                title=metadata.get("title", ""),
                size=metadata.get("size", 0),
                tracker=metadata.get("tracker", ""),
                imdb_id=metadata.get("imdb_id"),
                file_index=metadata.get("file_index"),
                seeders=metadata.get("seeders"),
                season=metadata.get("season"),
                episode=metadata.get("episode"),
                sources=metadata.get("sources", []),
                parsed=metadata.get("parsed", {}),
                updated_at=metadata.get("updated_at", time.time()),
            )
        elif not isinstance(metadata, TorrentMetadata):
            logger.warning(
                f"Invalid metadata type passed to broadcast_torrent: {type(metadata)}"
            )
            return

        await self.gossip.queue_torrent(metadata)

    async def handle_websocket_connection(self, websocket, path: str = "") -> None:
        """
        Handle an incoming WebSocket connection from FastAPI.

        This should be called from the FastAPI WebSocket endpoint.
        """
        if not self._running:
            await websocket.close()
            return

        # Get client address
        client_address = getattr(websocket, "client", ("unknown", 0))
        address = (
            f"ws://{client_address[0]}:{client_address[1]}"
            if client_address
            else "unknown"
        )

        node_id = await self.transport.handle_incoming_connection(websocket, address)

        if node_id:
            # Record in discovery for future PEX
            # Use corrected address (with listen port) if available
            real_address = self.transport.get_peer_address(node_id) or address
            self.discovery.record_incoming_connection(node_id, real_address)

            # Sync manifests with the newly connected peer
            asyncio.create_task(self._sync_manifests_with_peers([node_id]))

    async def _on_peer_connected(self, node_id: str, address: str) -> None:
        """Callback when a peer connects via the native WebSocket server."""
        self.discovery.record_incoming_connection(node_id, address)

        # Sync manifests with the newly connected peer
        # This ensures role changes and pool updates are synchronized
        asyncio.create_task(self._sync_manifests_with_peers([node_id]))

    async def _sync_manifests_with_peers(self, peer_ids: List[str]) -> None:
        """
        Send our pool manifests to specified peers.

        This triggers manifest exchange - when peers receive our manifest,
        they will compare versions and send back their manifests if they
        have newer versions. This ensures:
        - Role changes (promotions/demotions) are synchronized
        - Member additions/removals are synchronized
        - Pool metadata updates are synchronized
        """
        if not self.pool_store or not peer_ids:
            return

        # Get all manifests we're a member of
        memberships = self.pool_store.get_memberships()
        if not memberships:
            return

        for pool_id in memberships:
            manifest = self.pool_store.get_manifest(pool_id)
            if not manifest:
                continue

            # Send manifest to each peer
            for peer_id in peer_ids:
                try:
                    await self._send_pool_manifest(peer_id, manifest)
                except Exception as e:
                    logger.debug(
                        f"Failed to send manifest {pool_id} to {peer_id[:16]}: {e}"
                    )

    async def get_stats(self) -> Dict:
        """Get comprehensive CometNet statistics."""
        if not self._running:
            return {"enabled": False}

        uptime = time.time() - self._started_at if self._started_at else 0

        connection_stats = (
            self.transport.get_connection_stats() if self.transport else {}
        )
        gossip_stats = self.gossip.get_stats() if self.gossip else {}
        reputation_summary = (
            self.reputation.get_reputation_summary() if self.reputation else {}
        )

        # Detect security alerts
        security_alerts = []

        # Check IP diversity (low diversity = potential Eclipse attack)
        ip_diversity = connection_stats.get("ip_diversity", 1.0)
        connected_peers = connection_stats.get("connected_peers", 0)
        if connected_peers >= 5 and ip_diversity < 0.5:
            security_alerts.append(
                {
                    "level": "warning",
                    "type": "low_ip_diversity",
                    "message": f"Low IP diversity ({ip_diversity:.2f}) - possible Eclipse attack",
                }
            )

        # Check for high invalid message rate
        invalid_msgs = gossip_stats.get("invalid_messages", 0)
        total_msgs = gossip_stats.get("messages_received", 0)
        if total_msgs > 100 and invalid_msgs / total_msgs > 0.3:
            security_alerts.append(
                {
                    "level": "warning",
                    "type": "high_invalid_rate",
                    "message": f"High invalid message rate ({invalid_msgs}/{total_msgs})",
                }
            )

        # Check for many blacklisted peers
        blacklisted = reputation_summary.get("blacklisted", 0)
        if blacklisted >= 5:
            security_alerts.append(
                {
                    "level": "info",
                    "type": "many_blacklisted",
                    "message": f"{blacklisted} peers have been blacklisted",
                }
            )

        return {
            "enabled": True,
            "node_id": self.identity.node_id if self.identity else None,
            "public_key": self.identity.public_key_hex if self.identity else None,
            "uptime_seconds": uptime,
            "connection_stats": connection_stats,
            "discovery_stats": self.discovery.get_stats() if self.discovery else {},
            "gossip_stats": gossip_stats,
            "reputation_summary": reputation_summary,
            "keystore_stats": self.keystore.get_stats() if self.keystore else {},
            "security_alerts": security_alerts,
            # stats
            "contribution_mode": settings.COMETNET_CONTRIBUTION_MODE or "full",
            "pool_stats": self.pool_store.get_stats() if self.pool_store else {},
        }

    async def get_peers(self) -> Dict[str, Any]:
        """Get connected peers information."""
        if not self._running or not self.transport:
            return {"peers": [], "count": 0}

        peer_info = []
        for node_id, conn in self.transport._connections.items():
            peer_info.append(
                {
                    "node_id": node_id,
                    "address": conn.address,
                    "connected_at": conn.connected_at,
                    "last_activity": conn.last_activity,
                    "is_outbound": conn.is_outbound,
                    "latency_ms": round(conn.latency_ms, 2),
                }
            )

        return {"peers": peer_info, "count": len(peer_info)}

    # ==================== Pool Management API ====================

    async def create_pool(
        self,
        pool_id: str,
        display_name: str,
        description: str = "",
        join_mode: str = "invite",
    ) -> Dict:
        """Create a new pool with this node as admin."""
        if not self._running or not self.pool_store:
            raise RuntimeError("CometNet not running")

        from comet.cometnet.pools import JoinMode

        mode = JoinMode(join_mode)

        manifest = await self.pool_store.create_pool(
            pool_id=pool_id,
            display_name=display_name,
            identity=self.identity,
            description=description,
            join_mode=mode,
        )

        # Auto-subscribe to our own pool
        await self.pool_store.subscribe(pool_id)

        # Broadcast the new pool to peers
        await self._broadcast_pool_manifest(manifest)

        return manifest.model_dump()

    async def delete_pool(self, pool_id: str) -> bool:
        """Delete a pool (creator only) and broadcast to network."""
        if not self._running or not self.pool_store:
            return False

        # Get manifest to check permissions
        manifest = self.pool_store.get_manifest(pool_id)
        if not manifest:
            return False

        # Only the creator can delete the pool
        from comet.cometnet.pools import MemberRole

        my_member = manifest.get_member(self.identity.public_key_hex)
        if not my_member or my_member.role != MemberRole.CREATOR:
            raise PermissionError("Only the pool creator can delete the pool")

        # Delete locally
        result = await self.pool_store.delete_pool(pool_id)

        if result:
            # Broadcast deletion to all peers
            from comet.cometnet.protocol import PoolDeleteMessage

            delete_msg = PoolDeleteMessage(
                sender_id=self.identity.node_id,
                pool_id=pool_id,
                deleted_by=self.identity.public_key_hex,
            )
            delete_msg.signature = await self.identity.sign_hex_async(
                delete_msg.to_signable_bytes()
            )

            await self.transport.broadcast(delete_msg)
            logger.log(
                "COMETNET", f"Deleted and broadcasted deletion of pool {pool_id}"
            )

        return result

    async def get_pools(self) -> Dict:
        """Get all known pools and membership info."""
        if not self.pool_store:
            return {}

        return {
            "pools": {
                pid: m.model_dump()
                for pid, m in self.pool_store.get_all_manifests().items()
            },
            "memberships": list(self.pool_store.get_memberships()),
            "subscriptions": list(self.pool_store.get_subscriptions()),
        }

    async def subscribe_to_pool(self, pool_id: str) -> bool:
        """Subscribe to a pool (trust its members)."""
        if not self.pool_store:
            return False

        await self.pool_store.subscribe(pool_id)
        return True

    async def unsubscribe_from_pool(self, pool_id: str) -> bool:
        """Unsubscribe from a pool."""
        if not self.pool_store:
            return False

        await self.pool_store.unsubscribe(pool_id)
        return True

    async def create_pool_invite(
        self,
        pool_id: str,
        expires_in: Optional[int] = None,
        max_uses: Optional[int] = None,
    ) -> Optional[str]:
        """Create an invitation link for a pool (admin only)."""
        if not self.pool_store:
            return None

        try:
            # Use advertise_url if set, otherwise try to construct from listen_port
            node_url = self.advertise_url
            if not node_url and self.listen_port:
                # Fallback: at least include port info (user will need to know their IP)
                node_url = f"ws://YOUR_IP:{self.listen_port}"

            invite = await self.pool_store.create_invite(
                pool_id=pool_id,
                identity=self.identity,
                expires_in=expires_in,
                max_uses=max_uses,
                node_url=node_url,
            )
            return invite.to_link()
        except (PermissionError, ValueError) as e:
            logger.warning(f"Failed to create invite: {e}")
            return None

    async def get_pool_invites(self, pool_id: str) -> Dict[str, Any]:
        """Get all active invites for a pool."""
        if not self.pool_store:
            return {}
        # Only admins can see invites (implementation detail in store, but we check here too if needed)
        # Assuming store.get_invites returns objects, we need to serialize
        invites = self.pool_store.get_invites(pool_id)
        return {inv.invite_code: inv.model_dump() for inv in invites}

    async def delete_pool_invite(self, pool_id: str, invite_code: str) -> bool:
        """Delete a pool invite."""
        if not self.pool_store:
            return False
        return await self.pool_store.delete_invite(pool_id, invite_code)

    async def join_pool_with_invite(
        self, pool_id: str, invite_code: str, node_url: Optional[str] = None
    ) -> bool:
        """
        Join a pool using an invitation code.

        Args:
            pool_id: ID of the pool to join
            invite_code: The invitation code
            node_url: Optional URL of the node that created the invite.
                      If provided, will connect to that node to request the manifest.
        """
        if not self.pool_store:
            return False

        # First, try local (if we already have the manifest and invite)
        local_success = await self.pool_store.use_invite(
            pool_id, invite_code, self.identity
        )
        if local_success:
            return True

        # If no node_url provided and local failed, we can't proceed
        if not node_url:
            logger.debug(
                f"Cannot join pool {pool_id}: no local invite/manifest and no node_url provided"
            )
            return False

        # Remote join: connect to the node and request to join
        try:
            # Connect to the remote node if not already connected
            peer_id = None

            # Check if we're already connected to this node
            for nid, addr in self.transport.get_peer_addresses().items():
                if addr == node_url or addr.rstrip("/") == node_url.rstrip("/"):
                    peer_id = nid
                    break

            # If not connected, establish a connection
            if not peer_id:
                logger.log(
                    "COMETNET", f"Connecting to {node_url} to join pool {pool_id}..."
                )
                peer_id = await self.transport.connect_to_peer(node_url)
                if not peer_id:
                    logger.warning(f"Failed to connect to {node_url} for pool join")
                    return False

            # Send a join request
            join_request = PoolJoinRequest(
                sender_id=self.identity.node_id,
                pool_id=pool_id,
                invite_code=invite_code,
                requester_key=self.identity.public_key_hex,
            )
            join_request.signature = await self.identity.sign_hex_async(
                join_request.to_signable_bytes()
            )

            success = await self.transport.send_to_peer(peer_id, join_request)
            if not success:
                logger.warning(f"Failed to send join request to {peer_id[:16]}")
                return False

            logger.log(
                "COMETNET", f"Sent join request for pool {pool_id} to {peer_id[:16]}..."
            )

            # The manifest will be received asynchronously via _handle_pool_manifest
            # Wait a bit for the response
            await asyncio.sleep(2.0)

            # Check if we now have the manifest and are a member
            manifest = self.pool_store.get_manifest(pool_id)
            if manifest and manifest.is_member(self.identity.public_key_hex):
                self.pool_store._memberships.add(pool_id)
                await self.pool_store._save_memberships()

                # Store the node_url so we can reconnect later
                self.pool_store.add_pool_peer(pool_id, node_url)
                await self.pool_store._save_pool_peers()

                logger.log("COMETNET", f"Successfully joined pool {pool_id}")
                return True

            logger.debug(
                f"Join request sent but manifest not yet received for {pool_id}"
            )
            return False

        except Exception as e:
            logger.warning(f"Failed to join pool remotely: {e}")
            return False

    async def add_pool_member(
        self,
        pool_id: str,
        member_key: str,
        role: str = "member",
    ) -> bool:
        """Add a member to a pool (admin only)."""
        if not self.pool_store:
            return False

        from comet.cometnet.pools import MemberRole

        member_role = MemberRole(role)

        try:
            result = await self.pool_store.add_member(
                pool_id=pool_id,
                new_member_key=member_key,
                identity=self.identity,
                role=member_role,
            )
            if result:
                # Broadcast updated manifest to all peers
                manifest = self.pool_store.get_manifest(pool_id)
                if manifest:
                    await self._broadcast_pool_manifest(manifest)
            return result
        except (PermissionError, ValueError) as e:
            logger.warning(f"Failed to add member: {e}")
            return False

    async def remove_pool_member(self, pool_id: str, member_key: str) -> bool:
        """Remove a member from a pool (admin only)."""
        if not self.pool_store:
            return False

        try:
            result = await self.pool_store.remove_member(
                pool_id=pool_id,
                member_key=member_key,
                identity=self.identity,
            )
            if result:
                # Broadcast updated manifest to all peers
                manifest = self.pool_store.get_manifest(pool_id)
                if manifest:
                    await self._broadcast_pool_manifest(manifest)
            return result
        except (PermissionError, ValueError) as e:
            logger.warning(f"Failed to remove member: {e}")
            return False

    async def get_pool_details(self, pool_id: str) -> Optional[Dict]:
        """Get detailed information about a pool including all members."""
        if not self.pool_store:
            return None

        manifest = self.pool_store.get_manifest(pool_id)
        if not manifest:
            return None

        # Check if we are admin of this pool
        is_admin = (
            manifest.is_admin(self.identity.public_key_hex) if self.identity else False
        )
        is_member = (
            manifest.is_member(self.identity.public_key_hex) if self.identity else False
        )

        return {
            "pool_id": manifest.pool_id,
            "display_name": manifest.display_name,
            "description": manifest.description,
            "creator_key": manifest.creator_key,
            "join_mode": manifest.join_mode.value,
            "version": manifest.version,
            "created_at": manifest.created_at,
            "updated_at": manifest.updated_at,
            "is_admin": is_admin,
            "is_member": is_member,
            "members": [
                {
                    "public_key": m.public_key,
                    "role": m.role.value,
                    "added_at": m.added_at,
                    "added_by": m.added_by,
                    "contribution_count": m.contribution_count,
                    "last_seen": m.last_seen,
                    "is_self": m.public_key == self.identity.public_key_hex
                    if self.identity
                    else False,
                }
                for m in manifest.members
            ],
        }

    async def update_member_role(
        self, pool_id: str, member_key: str, new_role: str
    ) -> bool:
        """Change a member's role (promote to admin or demote to member)."""
        if not self.pool_store:
            return False

        from comet.cometnet.pools import MemberRole

        manifest = self.pool_store.get_manifest(pool_id)
        if not manifest:
            raise ValueError(f"Pool {pool_id} not found")

        if not manifest.is_admin(self.identity.public_key_hex):
            raise PermissionError("Only admins can change member roles")

        member = manifest.get_member(member_key)
        if not member:
            raise ValueError("Member not found in pool")

        # Validate new role
        try:
            role = MemberRole(new_role)
        except ValueError:
            raise ValueError(f"Invalid role: {new_role}. Must be 'admin' or 'member'")

        # Can't demote or modify the creator
        if member.role == MemberRole.CREATOR:
            raise ValueError("Cannot change the role of the pool creator")

        # Can't demote the last admin
        if member.role == MemberRole.ADMIN and role == MemberRole.MEMBER:
            admin_count = len(manifest.get_admins())
            if admin_count <= 1:
                raise ValueError("Cannot demote the last admin")

        # Update the role
        member.role = role
        manifest.version += 1
        manifest.updated_at = time.time()

        # Re-sign and save
        await self.pool_store.store_manifest(manifest, self.identity)

        # Broadcast updated manifest to all peers
        await self._broadcast_pool_manifest(manifest)

        logger.log(
            "COMETNET",
            f"Changed role of {member_key[:16]}... to {new_role} in pool {pool_id}",
        )
        return True

    async def _load_state(self) -> None:
        """Load saved state from disk."""
        state_path = self.keys_dir / self.STATE_FILE

        if not state_path.exists():
            return

        try:
            with open(state_path, "r") as f:
                state = json.load(f)

            # Verify state file integrity (detect tampering)
            stored_hash = state.pop("integrity_hash", None)
            if stored_hash and self.identity:
                # Compute expected hash
                state_bytes = json.dumps(state, sort_keys=True).encode("utf-8")
                expected_hash = hmac.new(
                    self.identity.public_key_bytes[
                        :32
                    ],  # Use part of public key as HMAC key
                    state_bytes,
                    hashlib.sha256,
                ).hexdigest()

                if not hmac.compare_digest(stored_hash, expected_hash):
                    logger.warning(
                        "State file integrity check failed - possible tampering detected. "
                        "State will not be loaded."
                    )
                    return

            # Load reputation data
            if "reputation" in state and self.reputation:
                self.reputation.from_dict(state["reputation"])
                logger.log(
                    "COMETNET",
                    f"Loaded reputation data for {len(state['reputation'].get('peers', {}))} peers",
                )

            # Load keystore data
            if "keystore" in state and self.keystore:
                self.keystore.from_dict(state["keystore"])

            # Load discovered peers
            if "discovery" in state and self.discovery:
                self.discovery.from_dict(state["discovery"])

        except Exception as e:
            logger.warning(f"Failed to load CometNet state: {e}")

    async def _save_state(self) -> None:
        """Save state to disk."""
        state_path = self.keys_dir / self.STATE_FILE

        try:
            state = {
                "saved_at": time.time(),
                "node_id": self.identity.node_id if self.identity else None,
                "reputation": self.reputation.to_dict() if self.reputation else {},
                "keystore": self.keystore.to_dict() if self.keystore else {},
                "discovery": self.discovery.to_dict() if self.discovery else {},
            }

            # Add integrity hash to detect tampering
            if self.identity:
                state_bytes = json.dumps(state, sort_keys=True).encode("utf-8")
                integrity_hash = hmac.new(
                    self.identity.public_key_bytes[
                        :32
                    ],  # Use part of public key as HMAC key
                    state_bytes,
                    hashlib.sha256,
                ).hexdigest()
                state["integrity_hash"] = integrity_hash

            self.keys_dir.mkdir(parents=True, exist_ok=True)

            with open(state_path, "w") as f:
                json.dump(state, f, indent=2)

            logger.debug("CometNet state saved")

        except Exception as e:
            logger.warning(f"Failed to save CometNet state: {e}")


# Global instance (will be initialized by app.py if enabled)
cometnet_service: Optional[CometNetService] = None


def get_cometnet_service() -> Optional[CometNetService]:
    """Get the global CometNet service instance."""
    return cometnet_service


def init_cometnet_service(
    enabled: bool = False,
    listen_port: int = 8765,
    bootstrap_nodes: Optional[List[str]] = None,
    manual_peers: Optional[List[str]] = None,
    max_peers: int = None,
    min_peers: int = None,
) -> CometNetService:
    """Initialize the global CometNet service."""
    global cometnet_service

    cometnet_service = CometNetService(
        enabled=enabled,
        listen_port=listen_port,
        bootstrap_nodes=bootstrap_nodes,
        manual_peers=manual_peers,
        max_peers=max_peers,
        min_peers=min_peers,
        keys_dir=settings.COMETNET_KEYS_DIR,
        advertise_url=settings.COMETNET_ADVERTISE_URL,
    )

    return cometnet_service
