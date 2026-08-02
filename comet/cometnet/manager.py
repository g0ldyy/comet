"""
CometNet Service Manager

Main entry point for CometNet functionality.
Orchestrates all components: Identity, Transport, Discovery, Gossip, Reputation, Pools, and Contribution Modes.
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiofiles

from comet.cometnet.crypto import NodeIdentity
from comet.cometnet.discovery import (
    DiscoveryService,
    canonicalize_persisted_peers,
    is_valid_peer_address,
    validate_discovery_configuration,
)
from comet.cometnet.gossip import GossipEngine
from comet.cometnet.interface import CometNetBackend
from comet.cometnet.keystore import PublicKeyStore
from comet.cometnet.nat import UPnPManager
from comet.cometnet.pools import (
    JoinMode,
    MemberRole,
    PoolManifest,
    PoolMember,
    PoolStore,
)
from comet.cometnet.protocol import (
    AnyMessage,
    MessageType,
    PeerRequest,
    PeerResponse,
    PoolDeleteMessage,
    PoolJoinRequest,
    PoolManifestMessage,
    PoolMemberUpdate,
    TorrentAnnounce,
    TorrentMetadata,
)
from comet.cometnet.reputation import ReputationStore
from comet.cometnet.state import validate_state
from comet.cometnet.transport import ConnectionManager
from comet.cometnet.utils import (
    check_advertise_url_reachability,
    check_system_clock_sync,
    format_websocket_url,
    is_internal_domain,
    is_private_or_internal_ip,
    shutdown_crypto_executor,
)
from comet.cometnet.validation import validate_message_security
from comet.core.models import settings
from comet.observability import create_detached_task, log
from comet.utils.atomic_file import write_text_atomic
from comet.utils.network import get_client_ip_any


class CometNetStartupError(RuntimeError):
    """Category-only startup rejection owned by the service boundary."""


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
        bootstrap_nodes: list[str] | None = None,
        manual_peers: list[str] | None = None,
        max_peers: int | None = None,
        min_peers: int | None = None,
        keys_dir: str | None = None,
        advertise_url: str | None = None,
    ):
        self.enabled = enabled
        self.listen_port = listen_port
        (
            self.manual_peers,
            self.bootstrap_nodes,
            self.min_peers,
            self.max_peers,
        ) = validate_discovery_configuration(
            manual_peers, bootstrap_nodes, min_peers, max_peers
        )
        self.keys_dir = Path(keys_dir) if keys_dir else Path("data/cometnet")
        self.advertise_url = advertise_url

        # Core components (initialized in start())
        self.identity: NodeIdentity | None = None
        self.transport: ConnectionManager | None = None
        self.discovery: DiscoveryService | None = None
        self.gossip: GossipEngine | None = None
        self.reputation: ReputationStore | None = None
        self.keystore: PublicKeyStore | None = None
        self.upnp: UPnPManager | None = None

        # components
        self.pool_store: PoolStore | None = None

        # Callback for saving torrents to database
        self._save_torrent_callback = None
        self._check_torrents_exist_callback = None

        # Running state
        self._running = False
        self._started_at: float | None = None
        self._state_save_task: asyncio.Task | None = None
        self._background_tasks: set[asyncio.Task] = set()

    def _start_background_task(self, coroutine) -> asyncio.Task | None:
        if not self._running:
            coroutine.close()
            return None

        task = create_detached_task(coroutine, name="cometnet.background")
        self._background_tasks.add(task)
        task.add_done_callback(self._on_background_task_done)
        return task

    def _on_background_task_done(self, task: asyncio.Task) -> None:
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        task.exception()

    async def _stop_background_tasks(self) -> None:
        tasks = tuple(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.clear()

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

    def set_check_torrents_exist_callback(self, callback) -> None:
        """
        Set the callback for checking if multiple torrents exist locally.

        The callback should be an async function that takes a list of info_hashes
        and returns a set of existing info_hashes.
        """
        self._check_torrents_exist_callback = callback

    async def start(self) -> None:
        """Start CometNet and clean any partially initialized resources on failure."""
        if not self.enabled or self._running:
            return
        started_at = time.monotonic_ns()
        try:
            await self._start()
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            try:
                await self._shutdown(save_state=False, force=True)
            except BaseException:
                pass
            raise
        except Exception as exc:
            log.error(
                "cometnet.start.failed",
                "CometNet failed to start",
                error_code="startup_failure",
                duration_ms=(time.monotonic_ns() - started_at) / 1_000_000,
                exc=exc,
            )
            try:
                await self._shutdown(save_state=False, force=True)
            except BaseException:
                pass
            raise
        log.info(
            "cometnet.ready",
            "CometNet is ready",
            duration_ms=(time.monotonic_ns() - started_at) / 1_000_000,
        )

    async def _start(self) -> None:
        """Start the CometNet service."""
        if not self.enabled:
            return

        if self._running:
            return

        # Initialize components
        await self._init_components()

        # Validate advertise_url format and security
        if self.advertise_url:
            await self._validate_advertise_url()

        # Load saved state
        await self._load_state()

        # Load pools data
        if self.pool_store:
            await self.pool_store.load()

            # Reconciliation: Ensure I am marked as a member in pools where I appear in the manifest
            # This fixes issues where local membership state gets out of sync with manifest
            if self.identity:
                my_key = self.identity.public_key_hex
                manifests = self.pool_store.get_all_manifests()
                restored_memberships = set()
                for pool_id, manifest in manifests.items():
                    if (
                        manifest.is_member(my_key)
                        and pool_id not in self.pool_store._memberships
                    ):
                        restored_memberships.add(pool_id)

                if restored_memberships:
                    await self.pool_store._replace_memberships(
                        self.pool_store.get_memberships() | restored_memberships
                    )

        # System Clock Sync Check
        if not settings.COMETNET_SKIP_TIME_CHECK:
            is_synced, _message, _offset = await check_system_clock_sync(
                tolerance=settings.COMETNET_TIME_CHECK_TOLERANCE,
                timeout=settings.COMETNET_TIME_CHECK_TIMEOUT,
            )

            if not is_synced:
                raise CometNetStartupError("clock_check_failed")

        # Start transport layer
        await self.transport.start()

        # Handle UPnP if enabled
        if settings.COMETNET_UPNP_ENABLED:
            self.upnp = UPnPManager(
                port=self.listen_port,
                lease_duration=settings.COMETNET_UPNP_LEASE_DURATION,
            )
            external_ip = await self.upnp.start()
            if external_ip and not self.advertise_url:
                # If we successfully mapped a port and don't have an advertise URL, use the IP
                self.advertise_url = format_websocket_url(external_ip, self.listen_port)
                # Update transport with new URL
                self.transport.advertise_url = self.advertise_url

        # validation for private IP in advertise URL
        if (
            self.advertise_url
            and not await is_valid_peer_address(self.advertise_url, allow_private=False)
            and not (
                settings.COMETNET_PRIVATE_NETWORK or settings.COMETNET_ALLOW_PRIVATE_PEX
            )
        ):
            # If we are in a private network or explicitly allow private PEX, we allow it
            raise CometNetStartupError("advertise_address_rejected")

        # Require advertise_url on public networks
        if (
            not self.advertise_url
            and not settings.COMETNET_PRIVATE_NETWORK
            and not settings.COMETNET_ALLOW_PRIVATE_PEX
        ):
            raise CometNetStartupError("advertise_address_missing")

        # WebSocket reachability check
        # Verify we can connect to our own advertise URL (like a peer would)
        if self.advertise_url and not settings.COMETNET_SKIP_REACHABILITY_CHECK:
            max_retries = settings.COMETNET_REACHABILITY_RETRIES
            retry_delay = settings.COMETNET_REACHABILITY_RETRY_DELAY
            timeout = settings.COMETNET_REACHABILITY_TIMEOUT

            is_reachable = False
            for attempt in range(1, max_retries + 1):
                if attempt > 1:
                    await asyncio.sleep(retry_delay)

                is_reachable, _result_message = await check_advertise_url_reachability(
                    self.advertise_url, timeout=timeout
                )

                if is_reachable:
                    break

            if not is_reachable:
                raise CometNetStartupError("reachability_check_failed")

        # Start discovery and gossip services
        await self.discovery.start(self.identity.node_id, self.listen_port)
        await self.gossip.start()

        self._running = True
        self._started_at = time.time()

        # Reconnect to known pool peers (from previous sessions)
        await self._reconnect_pool_peers()

        # Start periodic state save task
        self._state_save_task = create_detached_task(
            self._periodic_state_save(),
            name="cometnet-state-save",
        )

    async def stop(self) -> None:
        """Stop the CometNet service."""
        was_running = self._running
        started_at = time.monotonic_ns()
        await self._shutdown(save_state=True)
        if was_running:
            log.terminal(
                "cometnet.stopped",
                "CometNet stopped",
                outcome="ok",
                duration_ms=(time.monotonic_ns() - started_at) / 1_000_000,
            )

    async def _shutdown(self, *, save_state: bool, force: bool = False) -> None:
        if not self._running and not force:
            return

        self._running = False
        cleanup_errors = []

        async def run_async_cleanup(name: str, awaitable) -> None:
            try:
                await awaitable
            except BaseException as error:
                cleanup_errors.append(error)

        def run_sync_cleanup(name: str, callback) -> None:
            try:
                callback()
            except BaseException as error:
                cleanup_errors.append(error)

        # Stop periodic state save task
        if self._state_save_task:
            self._state_save_task.cancel()
            try:
                result = await asyncio.gather(
                    self._state_save_task, return_exceptions=True
                )
            except BaseException as error:
                cleanup_errors.append(error)
                result = []
            if (
                result
                and isinstance(result[0], BaseException)
                and not isinstance(result[0], asyncio.CancelledError)
            ):
                cleanup_errors.append(result[0])
            self._state_save_task = None

        await run_async_cleanup("background task", self._stop_background_tasks())

        if save_state:
            # Save state before stopping
            await run_async_cleanup("state save", self._save_state())

            # Save pools data
            if self.pool_store:
                await run_async_cleanup("pool save", self.pool_store.save())

        # Stop components in reverse order
        if self.gossip:
            await run_async_cleanup("gossip", self.gossip.stop())

        if self.discovery:
            await run_async_cleanup("discovery", self.discovery.stop())

        if self.transport:
            await run_async_cleanup("transport", self.transport.stop())

        if self.upnp:
            run_sync_cleanup("UPnP", self.upnp.stop)

        # Shutdown the dedicated crypto thread pool
        run_sync_cleanup("crypto executor", shutdown_crypto_executor)
        if cleanup_errors:
            raise cleanup_errors[0]

    async def _periodic_state_save(self) -> None:
        """
        Periodically save CometNet state to disk.
        """
        interval = settings.COMETNET_STATE_SAVE_INTERVAL
        degraded = False
        suppressed_count = 0
        degraded_at = 0

        while self._running:
            try:
                await asyncio.sleep(interval)

                if not self._running:
                    break

                # Save state
                await self._save_state()

                # Save pools data
                if self.pool_store:
                    await self.pool_store.save()
                if degraded:
                    log.info(
                        "cometnet.recovered",
                        "CometNet persistence recovered",
                        duration_ms=(time.monotonic_ns() - degraded_at) / 1_000_000,
                        suppressed_count=suppressed_count,
                    )
                    degraded = False
                    suppressed_count = 0
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if degraded:
                    suppressed_count += 1
                else:
                    degraded = True
                    degraded_at = time.monotonic_ns()
                    log.warning(
                        "cometnet.degraded",
                        "CometNet persistence is degraded",
                        error_code="dependency_failure",
                        suppressed_count=0,
                        exc=exc,
                    )

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

        self._start_background_task(self._connect_to_pool_peers(pool_peers))

    async def _connect_to_pool_peers(self, pool_peers: dict[str, set[str]]) -> None:
        """Background task to connect to pool peers."""
        connected = 0
        connected_peers: list[str] = []  # Track connected peer IDs for manifest sync

        for peers in pool_peers.values():
            for peer_addr in peers:
                try:
                    # Check if already connected to this address
                    already_connected = False
                    for addr in self.transport.get_peer_addresses().values():
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
                        # Small delay between connections
                        await asyncio.sleep(0.5)
                except Exception:
                    pass

        if connected > 0:
            # Send our manifests to newly connected peers to trigger sync
            # This ensures we receive their updated manifests if they have newer versions
            await self._sync_manifests_with_peers(connected_peers)

    async def _validate_advertise_url(self) -> None:
        """
        Validate the advertise URL for security and format issues.

        Checks:
        1. URL format (scheme, hostname, port)
        2. Internal/private domain detection
        3. DNS rebinding protection (domain resolving to private IP)
        4. Port range validation
        """
        url = self.advertise_url
        if not url:
            return

        try:
            parsed = urlparse(url)
        except Exception as exc:
            raise CometNetStartupError("advertise_url_invalid") from exc

        # Validate scheme
        if parsed.scheme not in ("ws", "wss"):
            raise CometNetStartupError("advertise_url_invalid")

        # Validate hostname exists
        hostname = parsed.hostname
        if not hostname:
            raise CometNetStartupError("advertise_url_invalid")

        # Validate port range
        if parsed.port is not None and not (1 <= parsed.port <= 65535):
            raise CometNetStartupError("advertise_url_invalid")

        # Check for internal domains (unless private network is allowed)
        if (
            not settings.COMETNET_PRIVATE_NETWORK
            and not settings.COMETNET_ALLOW_PRIVATE_PEX
        ):
            hostname_lower = hostname.lower()

            # Check for suspicious internal domain patterns
            if is_internal_domain(hostname_lower):
                raise CometNetStartupError("advertise_url_rejected")

            # Check if domain resolves to a private IP (DNS rebinding protection)
            if await is_private_or_internal_ip(hostname_lower):
                raise CometNetStartupError("advertise_url_rejected")

    async def _init_components(self) -> None:
        """Initialize all CometNet components."""
        # Ensure keys directory exists
        self.keys_dir.mkdir(parents=True, exist_ok=True)

        # Initialize identity
        self.identity = NodeIdentity(keys_dir=self.keys_dir)
        await self.identity.load_or_generate()

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
            check_torrents_exist=self._handle_check_torrents_exist,
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
        self, message: TorrentAnnounce, exclude: set[str] | None = None
    ) -> None:
        """Broadcast a gossip message to all peers."""
        await self.transport.broadcast(message, exclude)

    async def _handle_check_torrents_exist(self, info_hashes: list[str]) -> set[str]:
        """Check if torrents exist locally."""
        if self._check_torrents_exist_callback:
            try:
                return await self._check_torrents_exist_callback(info_hashes)
            except Exception:
                return set()

        return set()

    async def _handle_received_torrent(self, metadata: TorrentMetadata) -> None:
        """Handle a torrent received from the network."""
        if self._save_torrent_callback:
            await self._save_torrent_callback(metadata)

    async def _handle_torrent_announce(
        self, sender_id: str, message: AnyMessage
    ) -> None:
        """Handle incoming torrent announce messages."""
        if isinstance(message, TorrentAnnounce):
            sender_ip = self.transport.get_peer_address(sender_id)
            received_before = self.gossip.stats["torrents_received"]
            await self.gossip.handle_announce(sender_id, message, sender_ip)
            received = self.gossip.stats["torrents_received"] - received_before
            log.info(
                "cometnet.contribution.received",
                "CometNet contribution received",
                peer_id=sender_id,
                candidate_count=received,
            )

    async def _handle_peer_request(self, sender_id: str, message: AnyMessage) -> None:
        """Handle incoming peer request messages."""
        if isinstance(message, PeerRequest):
            if not await validate_message_security(
                message, sender_id, self.keystore, self.reputation
            ):
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
            if not await validate_message_security(
                message, sender_id, self.keystore, self.reputation
            ):
                return

            await self.discovery.handle_peer_response(message)

    async def _handle_pool_manifest(self, sender_id: str, message: AnyMessage) -> None:
        """Handle incoming pool manifest messages."""
        if not isinstance(message, PoolManifestMessage):
            return

        if not self.pool_store:
            return

        if not await validate_message_security(
            message, sender_id, self.keystore, self.reputation
        ):
            return

        # Convert message to PoolManifest
        try:
            members = [
                PoolMember(
                    public_key=m.public_key,
                    role=MemberRole(m.role),
                    added_at=m.added_at,
                    added_by=m.added_by,
                    alias=m.alias,
                    contribution_count=m.contribution_count,
                    last_seen=m.last_seen,
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
                version=message.manifest_version,
                created_at=message.created_at,
                updated_at=message.updated_at,
                signatures=message.manifest_signatures,
            )

            accepted, existing = await self.pool_store.accept_remote_manifest(manifest)
            if not accepted:
                if existing and existing.version > manifest.version:
                    await self._send_pool_manifest(sender_id, existing)
                return

            # Update our membership status based on the new manifest
            my_key = self.identity.public_key_hex if self.identity else None
            if my_key:
                was_member = self.pool_store.is_member_of(message.pool_id)
                is_now_member = manifest.is_member(my_key)

                if was_member and not is_now_member:
                    # We were removed from this pool - clean up completely
                    await self.pool_store.delete_pool(message.pool_id)
                    return  # Don't store anything else for this pool
                elif not was_member and is_now_member:
                    # We were added to this pool (e.g., via invite on another node)
                    await self.pool_store.add_membership(message.pool_id)

            # Store the sender's address so we can reconnect later
            sender_addr = self.transport.get_peer_address(sender_id)
            if sender_addr:
                await self.pool_store.add_pool_peer(message.pool_id, sender_addr)
        except ValueError:
            pass

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

        if not await validate_message_security(
            message, sender_id, self.keystore, self.reputation
        ):
            return

        pool_id = message.pool_id
        invite_code = message.invite_code
        requester_key = message.requester_key

        accepted = await self.pool_store.accept_invite_member(
            pool_id,
            invite_code,
            requester_key,
            alias=message.alias,
            signing_identity=self.identity,
        )
        if accepted is None:
            return
        manifest, invite, added = accepted

        # Send the manifest back to the requester
        await self._send_pool_manifest(sender_id, manifest)

        if added:
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

        if not await validate_message_security(
            message, sender_id, self.keystore, self.reputation
        ):
            return

        async with self.pool_store.serialized_manifest_mutation():
            await self._apply_pool_member_update(sender_id, message)

    async def _apply_pool_member_update(
        self, sender_id: str, message: PoolMemberUpdate
    ) -> None:
        """Apply an authenticated pool delta while the store mutation lock is held."""
        current_manifest = self.pool_store.get_manifest(message.pool_id)
        if not current_manifest:
            return

        # Work on a copy to verify before updating
        manifest = current_manifest.model_copy(deep=True)

        # Special case: member leaving (self-removal)
        # For "leave" action, the updated_by should be the member themselves
        is_self_leave = (
            message.action == "leave" and message.updated_by == message.member_key
        )

        if is_self_leave:
            # Verify it's the actual member leaving (signature from the leaving member)
            if not await NodeIdentity.verify_hex_async(
                message.to_signable_bytes(), message.signature, message.updated_by
            ):
                return

            # Verify the person is actually a member
            if not manifest.is_member(message.member_key):
                return

            # A member's leave signature proves intent, but only an administrator
            # can certify and publish the resulting manifest state.
            if not self.identity or not current_manifest.is_admin(
                self.identity.public_key_hex
            ):
                return
        else:
            # Normal case: admin-initiated update
            # Verify the updater is an admin
            if not manifest.is_admin(message.updated_by):
                return

            # The transport sender may be a relay, so bind the delta directly to
            # the administrator identified by the message as well.
            if not await NodeIdentity.verify_hex_async(
                message.to_signable_bytes(), message.signature, message.updated_by
            ):
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
        elif message.action == "remove" or message.action == "leave":
            if target_member:
                manifest.members = [
                    m for m in manifest.members if m.public_key != message.member_key
                ]
                modified = True
        elif message.action == "promote":
            if target_member:
                target_member.role = MemberRole.ADMIN
                modified = True
        elif message.action == "demote" and target_member:
            target_member.role = MemberRole.MEMBER
            modified = True

        if not modified:
            return

        # Update version (we assume it increments by 1)
        manifest.version += 1
        manifest.updated_at = message.timestamp

        # Convert the member's signed intent into a newly signed administrator
        # state update. Persisting the old manifest signatures here would attach
        # invalid signatures to the changed member list.
        if is_self_leave:
            await self.pool_store.store_manifest(manifest, self.identity)
            await self._broadcast_pool_member_update(
                pool_id=message.pool_id,
                action="remove",
                member_key=message.member_key,
                updated_by=self.identity.public_key_hex,
                manifest_signatures=manifest.signatures,
                exclude={sender_id},
            )
            return

        # Verify that our new state matches the signatures provided by admin
        # This is the critical step: ensuring our strict determinism matches the admin's
        manifest_valid = False
        manifest.signatures = {}  # modify for to_signable_bytes check? No, to_signable_bytes excludes signatures.

        signable = manifest.to_signable_bytes()

        # Check carefully if any provided signature validates our new state
        for admin_key, sig in message.manifest_signatures.items():
            if current_manifest.is_admin(admin_key) and NodeIdentity.verify_hex(
                signable, sig, admin_key
            ):
                manifest_valid = True
                # Adopt the new signatures
                manifest.signatures = message.manifest_signatures
                break

        if manifest_valid:
            await self.pool_store.store_manifest(manifest)

            # Re-broadcast to others who might not have received it
            await self.transport.broadcast(message, exclude={sender_id})
        else:
            # Manifest state mismatch - request full manifest
            await self._send_pool_manifest(sender_id, current_manifest)

    async def _handle_pool_delete(self, sender_id: str, message: AnyMessage) -> None:
        """Handle incoming pool deletion messages."""
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
            return

        # Verify the signature
        if not NodeIdentity.verify_hex(
            message.to_signable_bytes(), message.signature, message.deleted_by
        ):
            return

        # Delete the pool locally
        await self.pool_store.delete_pool(message.pool_id)

    async def _send_pool_manifest(self, peer_id: str, manifest) -> None:
        """Send a pool manifest to a specific peer."""
        msg = PoolManifestMessage(
            sender_id=self.identity.node_id,
            pool_id=manifest.pool_id,
            display_name=manifest.display_name,
            description=manifest.description,
            creator_key=manifest.creator_key,
            members=[m.model_dump(exclude={"node_id"}) for m in manifest.members],
            join_mode=manifest.join_mode.value,
            manifest_version=manifest.version,
            created_at=manifest.created_at,
            updated_at=manifest.updated_at,
            manifest_signatures=manifest.signatures,
        )
        msg.signature = await self.identity.sign_hex_async(msg.to_signable_bytes())
        await self.transport.send_to_peer(peer_id, msg)

    async def _broadcast_pool_manifest(
        self, manifest, exclude: set[str] | None = None
    ) -> None:
        """Broadcast a pool manifest to all connected peers."""
        msg = PoolManifestMessage(
            sender_id=self.identity.node_id,
            pool_id=manifest.pool_id,
            display_name=manifest.display_name,
            description=manifest.description,
            creator_key=manifest.creator_key,
            members=[m.model_dump(exclude={"node_id"}) for m in manifest.members],
            join_mode=manifest.join_mode.value,
            manifest_version=manifest.version,
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
        manifest_signatures: dict[str, str],
        new_role: str | None = None,
        exclude: set[str] | None = None,
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

    async def broadcast_torrents(self, metadata_list: list[Any]) -> None:
        """
        Broadcast multiple torrents to the network.
        Accepts both TorrentMetadata objects and dicts.
        """
        if not self._running or not self.gossip:
            return

        valid_torrents = []
        default_updated_at = time.time()
        for metadata in metadata_list:
            # Convert dict to TorrentMetadata if needed
            if isinstance(metadata, dict):
                try:
                    metadata = TorrentMetadata(
                        info_hash=metadata.get("info_hash", "").lower(),
                        title=metadata.get("title", ""),
                        size=int(metadata.get("size") or 0),
                        tracker=metadata.get("tracker", ""),
                        imdb_id=metadata.get("imdb_id"),
                        file_index=metadata.get("file_index"),
                        seeders=metadata.get("seeders"),
                        season=metadata.get("season"),
                        episode=metadata.get("episode"),
                        sources=metadata.get("sources") or [],
                        parsed=metadata.get("parsed"),
                        updated_at=metadata.get("updated_at", default_updated_at),
                    )
                except Exception:
                    continue

            if isinstance(metadata, TorrentMetadata):
                valid_torrents.append(metadata)

        if valid_torrents:
            await self.gossip.queue_torrents(valid_torrents)
            log.info(
                "cometnet.contribution.queued",
                "CometNet contribution queued",
                candidate_count=len(valid_torrents),
            )

    async def broadcast_torrent(self, metadata) -> None:
        """
        Broadcast a torrent to the network.

        This is the main method for sharing newly discovered torrents.
        Should be called when a scraper discovers a new torrent.
        Accepts both TorrentMetadata objects and dicts.
        """
        await self.broadcast_torrents([metadata])

    async def handle_websocket_connection(self, websocket, path: str = "") -> None:
        """
        Handle an incoming WebSocket connection from FastAPI /cometnet/ws endpoint.
        """
        if not self._running:
            await websocket.close()
            return

        client_ip, _from_proxy = get_client_ip_any(websocket)

        node_id = await self.transport.handle_incoming_connection(websocket, client_ip)

        if node_id:
            # Record in discovery for future PEX
            real_address = self.transport.get_peer_address(node_id)
            if real_address:
                self.discovery.record_incoming_connection(node_id, real_address)

            # Sync manifests with the newly connected peer
            self._start_background_task(self._sync_manifests_with_peers([node_id]))

    async def _on_peer_connected(self, node_id: str, address: str | None) -> None:
        """Callback when a peer connects via the native WebSocket server."""
        log.info(
            "cometnet.peer.connected",
            "CometNet peer connected",
            peer_id=node_id,
        )
        if address:
            self.discovery.record_incoming_connection(node_id, address)

        # Sync manifests with the newly connected peer
        # This ensures role changes and pool updates are synchronized
        self._start_background_task(self._sync_manifests_with_peers([node_id]))

    async def _sync_manifests_with_peers(self, peer_ids: list[str]) -> None:
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
                except Exception:
                    pass

    async def get_stats(self) -> dict:
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
            "contribution_mode": settings.COMETNET_CONTRIBUTION_MODE,
            "pool_stats": self.pool_store.get_stats() if self.pool_store else {},
            # Private network info
            "private_network": settings.COMETNET_PRIVATE_NETWORK,
            "network_id": settings.COMETNET_NETWORK_ID
            if settings.COMETNET_PRIVATE_NETWORK
            else None,
        }

    async def get_peers(self) -> dict[str, Any]:
        """Get connected peers information."""
        if not self._running or not self.transport:
            return {"peers": [], "count": 0}

        peer_info = []
        for node_id, conn in self.transport._connections.items():
            # Get reputation data if available
            rep_data = {}
            if self.reputation:
                peer_rep = self.reputation.get(node_id)
                if peer_rep:
                    rep_data = {
                        "torrents_received": peer_rep.valid_contributions,
                        "invalid_contributions": peer_rep.invalid_contributions,
                        "reputation": round(peer_rep.effective_reputation, 2),
                        "trust_level": peer_rep.trust_level,
                    }

            peer_info.append(
                {
                    "node_id": node_id,
                    "address": conn.address,
                    "connected_at": conn.connected_at,
                    "last_activity": conn.last_activity,
                    "is_outbound": conn.is_outbound,
                    "latency_ms": round(conn.latency_ms, 2),
                    "alias": conn.alias,
                    "bytes_sent": conn.bytes_sent,
                    "bytes_received": conn.bytes_received,
                    "messages_sent": conn.messages_sent,
                    "messages_received": conn.messages_received,
                    **rep_data,
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
    ) -> dict:
        """Create a new pool with this node as admin."""
        if not self._running or not self.pool_store:
            raise RuntimeError("CometNet not running")

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

        my_member = manifest.get_member(self.identity.public_key_hex)
        if not my_member or my_member.role != MemberRole.CREATOR:
            raise PermissionError("Only the pool creator can delete the pool")

        # Delete locally
        result = await self.pool_store.delete_pool(pool_id)

        if result:
            # Broadcast deletion to all peers

            delete_msg = PoolDeleteMessage(
                sender_id=self.identity.node_id,
                pool_id=pool_id,
                deleted_by=self.identity.public_key_hex,
            )
            delete_msg.signature = await self.identity.sign_hex_async(
                delete_msg.to_signable_bytes()
            )

            await self.transport.broadcast(delete_msg)

        return result

    async def get_pools(self) -> dict:
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
        expires_in: int | None = None,
        max_uses: int | None = None,
    ) -> str | None:
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
        except (PermissionError, ValueError):
            return None

    async def get_pool_invites(self, pool_id: str) -> dict[str, Any]:
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
        self, pool_id: str, invite_code: str, node_url: str | None = None
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
            pool_id, invite_code, self.identity, alias=settings.COMETNET_NODE_ALIAS
        )
        if local_success:
            return True

        # If no node_url provided and local failed, we can't proceed
        if not node_url:
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
                peer_id = await self.transport.connect_to_peer(node_url)
                if not peer_id:
                    return False

            # Send a join request
            join_request = PoolJoinRequest(
                sender_id=self.identity.node_id,
                pool_id=pool_id,
                invite_code=invite_code,
                requester_key=self.identity.public_key_hex,
                alias=settings.COMETNET_NODE_ALIAS,
            )
            join_request.signature = await self.identity.sign_hex_async(
                join_request.to_signable_bytes()
            )

            success = await self.transport.send_to_peer(peer_id, join_request)
            if not success:
                return False

            # The manifest will be received asynchronously via _handle_pool_manifest
            # Wait a bit for the response
            await asyncio.sleep(2.0)

            # Check if we now have the manifest and are a member
            manifest = self.pool_store.get_manifest(pool_id)
            if manifest and manifest.is_member(self.identity.public_key_hex):
                await self.pool_store.add_membership(pool_id)

                # Store the node_url so we can reconnect later
                await self.pool_store.add_pool_peer(pool_id, node_url)
                return True

            return False
        except Exception:
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
        except (PermissionError, ValueError):
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
        except (PermissionError, ValueError):
            return False

    async def get_pool_details(self, pool_id: str) -> dict | None:
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
                    "node_id": m.node_id,
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

        # Validate new role
        try:
            role = MemberRole(new_role)
        except ValueError:
            raise ValueError(f"Invalid role: {new_role}. Must be 'admin' or 'member'")

        changed = await self.pool_store.set_member_role(
            pool_id,
            member_key,
            role,
            self.identity,
        )
        if not changed:
            return False

        # Broadcast updated manifest to all peers
        manifest = self.pool_store.get_manifest(pool_id)
        await self._broadcast_pool_manifest(manifest)
        return True

    async def leave_pool(self, pool_id: str) -> bool:
        """Leave a pool (self-removal). Any member except creator can leave."""
        if not self._running or not self.pool_store:
            return False

        # Get the manifest before we leave (to verify we're a member)
        manifest = self.pool_store.get_manifest(pool_id)
        if not manifest:
            raise ValueError(f"Pool {pool_id} not found")

        my_key = self.identity.public_key_hex
        member = manifest.get_member(my_key)
        if not member:
            return False  # Not a member

        # Creator cannot leave
        if member.role == MemberRole.CREATOR:
            raise ValueError("Creator cannot leave the pool. Delete the pool instead.")

        # Broadcast our departure to other pool members BEFORE cleaning up locally
        leave_message = PoolMemberUpdate(
            sender_id=self.identity.node_id,
            pool_id=pool_id,
            action="leave",
            member_key=my_key,
            updated_by=my_key,  # We're removing ourselves
            timestamp=time.time(),
        )
        leave_message.signature = await self.identity.sign_hex_async(
            leave_message.to_signable_bytes()
        )

        # Broadcast to all connected peers
        await self.transport.broadcast(leave_message)

        # Now do local cleanup
        return await self.pool_store.leave_pool(
            pool_id=pool_id,
            identity=self.identity,
        )

    async def _load_state(self) -> None:
        """Load saved state from disk."""
        state_path = self.keys_dir / self.STATE_FILE

        if not state_path.exists():
            return

        try:
            async with aiofiles.open(state_path, "r") as f:
                content = await f.read()
                state = validate_state(json.loads(content))

            stored_signature = state.pop("integrity_signature", None)
            if self.identity:
                if not stored_signature:
                    raise ValueError("CometNet state is missing its identity signature")
                state_bytes = json.dumps(state, sort_keys=True).encode("utf-8")
                if not NodeIdentity.verify_hex(
                    state_bytes,
                    stored_signature,
                    self.identity.public_key_hex,
                ):
                    raise ValueError("CometNet state identity signature is invalid")
                if state["node_id"] != self.identity.node_id:
                    raise ValueError(
                        "CometNet state belongs to a different node identity"
                    )

            canonical_peers, state_migrated = canonicalize_persisted_peers(
                state["discovery"]["known_peers"]
            )
            if state_migrated:
                state["discovery"] = {"known_peers": canonical_peers}

            # Prevalidate every synchronous component before the first live
            # component is mutated. Discovery also builds complete candidates
            # before publishing them in its async loader below.
            if self.reputation:
                ReputationStore.validate_persisted(state["reputation"])
            if self.keystore:
                PublicKeyStore.validate_persisted(
                    state["keystore"], max_keys=self.keystore.max_keys
                )

            # Validate addresses asynchronously before mutating any component.
            if self.discovery:
                await self.discovery.from_dict(state["discovery"])

            if self.reputation:
                self.reputation.from_dict(state["reputation"])

            if self.keystore:
                self.keystore.from_dict(state["keystore"])

            if self.gossip:
                self.gossip.from_dict(state["gossip"])

            if state_migrated:
                try:
                    await self._write_state(state)
                except Exception:
                    pass
        except Exception:
            pass

    async def _save_state(self) -> None:
        """Save state to disk."""
        state = {
            "saved_at": time.time(),
            "node_id": self.identity.node_id if self.identity else None,
            "reputation": self.reputation.to_dict() if self.reputation else {},
            "keystore": self.keystore.to_dict() if self.keystore else {},
            "discovery": self.discovery.to_dict() if self.discovery else {},
            "gossip": self.gossip.to_dict() if self.gossip else {},
        }
        await self._write_state(state)

    async def _write_state(self, state: dict) -> None:
        """Sign and atomically persist a validated state snapshot."""
        persisted_state = dict(state)
        if self.identity:
            state_bytes = json.dumps(persisted_state, sort_keys=True).encode("utf-8")
            persisted_state["integrity_signature"] = await self.identity.sign_hex_async(
                state_bytes
            )

        self.keys_dir.mkdir(parents=True, exist_ok=True)
        await write_text_atomic(
            self.keys_dir / self.STATE_FILE,
            json.dumps(persisted_state, indent=2),
        )


# Global instance (will be initialized by app.py if enabled)
cometnet_service: CometNetService | None = None


def get_cometnet_service() -> CometNetService | None:
    """Get the global CometNet service instance."""
    return cometnet_service


async def stop_cometnet_service() -> None:
    global cometnet_service

    if cometnet_service is not None:
        await cometnet_service.stop()
        cometnet_service = None


def init_cometnet_service(
    enabled: bool = False,
    listen_port: int = 8765,
    bootstrap_nodes: list[str] | None = None,
    manual_peers: list[str] | None = None,
    max_peers: int | None = None,
    min_peers: int | None = None,
) -> CometNetService:
    """Initialize the global CometNet service."""
    global cometnet_service

    if settings.FASTAPI_WORKERS > 1:
        raise CometNetStartupError("worker_count_invalid")

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
