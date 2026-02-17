"""
CometNet Service Manager

Main entry point for CometNet functionality.
Orchestrates all components: Identity, Transport, Discovery, Gossip, Reputation, Pools, and Contribution Modes.
"""

import asyncio
import hashlib
import hmac
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlparse

import aiofiles

from comet.cometnet.crypto import NodeIdentity
from comet.cometnet.discovery import DiscoveryService, is_valid_peer_address
from comet.cometnet.gossip import GossipEngine
from comet.cometnet.interface import CometNetBackend
from comet.cometnet.keystore import PublicKeyStore
from comet.cometnet.nat import UPnPManager
from comet.cometnet.pools import (JoinMode, MemberRole, PoolManifest,
                                  PoolMember, PoolStore)
from comet.cometnet.protocol import (AnyMessage, MessageType, PeerRequest,
                                     PeerResponse, PoolDeleteMessage,
                                     PoolJoinRequest, PoolManifestMessage,
                                     PoolMemberUpdate, TorrentAnnounce,
                                     TorrentMetadata)
from comet.cometnet.reputation import ReputationStore
from comet.cometnet.transport import ConnectionManager
from comet.cometnet.utils import (check_advertise_url_reachability,
                                  check_system_clock_sync, is_internal_domain,
                                  is_private_or_internal_ip, run_in_executor,
                                  shutdown_crypto_executor)
from comet.cometnet.validation import validate_message_security
from comet.core.logger import logger
from comet.core.models import settings
from comet.utils.network import get_client_ip_any


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
        # Callback for checking if torrent exists
        self._check_torrent_exists_callback = None
        self._check_torrents_exist_callback = None

        # Running state
        self._running = False
        self._started_at: Optional[float] = None
        self._state_save_task: Optional[asyncio.Task] = None

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

    def set_check_torrent_exists_callback(self, callback) -> None:
        """
        Set the callback for checking if a torrent exists locally.

        The callback should be an async function that takes an info_hash (str)
        and returns a boolean.
        """
        self._check_torrent_exists_callback = callback

    def set_check_torrents_exist_callback(self, callback) -> None:
        """
        Set the callback for checking if multiple torrents exist locally.

        The callback should be an async function that takes a list of info_hashes
        and returns a set of existing info_hashes.
        """
        self._check_torrents_exist_callback = callback

    async def start(self) -> None:
        """Start the CometNet service."""
        if not self.enabled:
            logger.log("COMETNET", "CometNet is disabled")
            return

        if self._running:
            return

        # Initialize components
        await self._init_components()

        logger.log("COMETNET", "=" * 60)
        logger.log(
            "COMETNET", f"Starting CometNet P2P Node - {self.identity.node_id[:8]}"
        )
        logger.log("COMETNET", "=" * 60)

        key_encrypted = "Yes" if settings.COMETNET_KEY_PASSWORD else "No"
        private_mode = (
            f" - Private Network: {settings.COMETNET_NETWORK_ID}"
            if settings.COMETNET_PRIVATE_NETWORK
            else " - Private Network: False"
        )

        if settings.COMETNET_TRUSTED_POOLS:
            trusted_pools = f" - Trusted Pools={len(settings.COMETNET_TRUSTED_POOLS)}"
        else:
            trusted_pools = " - Trusted Pools=All (Open Mode)"

        ingest_pools = (
            f" - Ingest Pools={len(settings.COMETNET_INGEST_POOLS)}"
            if settings.COMETNET_INGEST_POOLS
            else ""
        )

        logger.log(
            "COMETNET",
            f"Configuration: Port={self.listen_port}"
            f" - Max Peers={self.max_peers}"
            f" - Min Peers={self.min_peers}"
            f" - Keys: {self.keys_dir}"
            f" - Key Encrypted: {key_encrypted}"
            f" - Allow Private PEX: {settings.COMETNET_ALLOW_PRIVATE_PEX}"
            f" - Skip Reachability Check: {settings.COMETNET_SKIP_REACHABILITY_CHECK}"
            f" - Skip Time Check: {settings.COMETNET_SKIP_TIME_CHECK} (Tolerance: {settings.COMETNET_TIME_CHECK_TOLERANCE}s)"
            f" - State Save Interval: {settings.COMETNET_STATE_SAVE_INTERVAL}s"
            f"{private_mode}",
        )
        logger.log(
            "COMETNET",
            f"Reachability Check Config: Retries={settings.COMETNET_REACHABILITY_RETRIES}"
            f" - Retry Delay={settings.COMETNET_REACHABILITY_RETRY_DELAY}s"
            f" - Timeout={settings.COMETNET_REACHABILITY_TIMEOUT}s",
        )
        logger.log(
            "COMETNET",
            f"Pools Config: Dir={settings.COMETNET_POOLS_DIR}"
            f"{trusted_pools}{ingest_pools}",
        )

        if self.advertise_url:
            logger.log("COMETNET", f"Advertise URL: {self.advertise_url}")

        logger.log(
            "COMETNET",
            f"Peers: Bootstrap={len(self.bootstrap_nodes)}"
            f" - Manual={len(self.manual_peers)}"
            f" - UPnP: {settings.COMETNET_UPNP_ENABLED} (Lease: {settings.COMETNET_UPNP_LEASE_DURATION}s)",
        )

        logger.log(
            "COMETNET",
            f"Gossip: Fanout={settings.COMETNET_GOSSIP_FANOUT}"
            f" - Interval={settings.COMETNET_GOSSIP_INTERVAL}s"
            f" - TTL={settings.COMETNET_GOSSIP_MESSAGE_TTL}"
            f" - Max Torrents/Msg={settings.COMETNET_GOSSIP_MAX_TORRENTS_PER_MESSAGE}"
            f" - Clock Drift={settings.COMETNET_GOSSIP_VALIDATION_FUTURE_TOLERANCE}s/{settings.COMETNET_GOSSIP_VALIDATION_PAST_TOLERANCE}s"
            f" - Max Torrent Age={settings.COMETNET_GOSSIP_TORRENT_MAX_AGE}s",
        )

        logger.log(
            "COMETNET",
            f"Transport: Max Msg Size={settings.COMETNET_TRANSPORT_MAX_MESSAGE_SIZE}"
            f" - Max Conn/IP={settings.COMETNET_TRANSPORT_MAX_CONNECTIONS_PER_IP}"
            f" - Ping={settings.COMETNET_TRANSPORT_PING_INTERVAL}s"
            f" - Timeout={settings.COMETNET_TRANSPORT_CONNECTION_TIMEOUT}s"
            f" - Max Latency={settings.COMETNET_TRANSPORT_MAX_LATENCY_MS}ms"
            f" - RateLimit: {settings.COMETNET_TRANSPORT_RATE_LIMIT_ENABLED} "
            f"({settings.COMETNET_TRANSPORT_RATE_LIMIT_COUNT}/{settings.COMETNET_TRANSPORT_RATE_LIMIT_WINDOW}s)",
        )

        logger.log(
            "COMETNET",
            f"Discovery: PEX Batch={settings.COMETNET_PEX_BATCH_SIZE}"
            f" - Backoff={settings.COMETNET_PEER_CONNECT_BACKOFF_MAX}s"
            f" - Max Failures={settings.COMETNET_PEER_MAX_FAILURES}"
            f" - Cleanup Age={settings.COMETNET_PEER_CLEANUP_AGE}s",
        )

        logger.log(
            "COMETNET",
            f"Reputation: Init={settings.COMETNET_REPUTATION_INITIAL}"
            f" - Range=[{settings.COMETNET_REPUTATION_MIN}, {settings.COMETNET_REPUTATION_MAX}]"
            f" - Trust={settings.COMETNET_REPUTATION_THRESHOLD_TRUSTED}/{settings.COMETNET_REPUTATION_THRESHOLD_UNTRUSTED}"
            f" - Valid Bonus=+{settings.COMETNET_REPUTATION_BONUS_VALID_CONTRIBUTION}"
            f" - Anciennety Bonus=+{settings.COMETNET_REPUTATION_BONUS_PER_DAY_ANCIENNETY}/day (Max {settings.COMETNET_REPUTATION_BONUS_MAX_ANCIENNETY})"
            f" - Invalid Penalty=-{settings.COMETNET_REPUTATION_PENALTY_INVALID_CONTRIBUTION}"
            f" - Sig Penalty=-{settings.COMETNET_REPUTATION_PENALTY_INVALID_SIGNATURE}",
        )

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
                changes = False
                for pool_id, manifest in manifests.items():
                    if manifest.is_member(my_key):
                        if pool_id not in self.pool_store._memberships:
                            self.pool_store._memberships.add(pool_id)
                            changes = True
                            logger.log(
                                "COMETNET",
                                f"Restored missing membership for pool {pool_id}",
                            )

                if changes:
                    await self.pool_store._save_memberships()

        # System Clock Sync Check
        if not settings.COMETNET_SKIP_TIME_CHECK:
            logger.log("COMETNET", "Verifying system clock synchronization...")
            is_synced, msg, offset = await check_system_clock_sync(
                tolerance=settings.COMETNET_TIME_CHECK_TOLERANCE,
                timeout=settings.COMETNET_TIME_CHECK_TIMEOUT,
            )

            if is_synced:
                logger.log("COMETNET", f"✓ System clock is synchronized ({msg})")
            else:
                drift_info = (
                    f"Drift: {offset:.2f}s (Tolerance: {settings.COMETNET_TIME_CHECK_TOLERANCE}s)\n"
                    if abs(offset) > 0.001
                    else ""
                )
                logger.critical(
                    f"\nCometNet failed to start: System clock check failed.\n"
                    f"Status: {msg}\n"
                    f"{drift_info}\n"
                    "Accurate system time is critical for:\n"
                    "1. Validating message signatures\n"
                    "2. SSL/TLS connections\n"
                    "3. Distributed consensus\n\n"
                    "Please synchronize your clock (e.g. sudo ntpdate pool.ntp.org)\n"
                    "To skip this check: COMETNET_SKIP_TIME_CHECK=true"
                )
                await logger.complete()
                sys.exit(1)

        # Start transport layer
        await self.transport.start()

        # Handle UPnP if enabled
        if settings.COMETNET_UPNP_ENABLED:
            logger.log("COMETNET", "Initializing UPnP...")
            self.upnp = UPnPManager(
                port=self.listen_port,
                lease_duration=settings.COMETNET_UPNP_LEASE_DURATION,
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
                        f"UPnP mapped to {external_ip} but COMETNET_ADVERTISE_URL is already set. Using configured URL.",
                    )

        # Custom check for unencrypted transport
        if self.advertise_url and self.advertise_url.startswith("ws://"):
            try:
                parsed = urlparse(self.advertise_url)
                host = (parsed.hostname or "").lower()
                local_hosts = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"}
                is_local = host in local_hosts
            except Exception:
                is_local = False

            if not is_local:
                logger.warning(
                    "SECURITY WARNING: CometNet is configured with unencrypted 'ws://' URL. "
                    "Your P2P traffic (including metadata) is visible to interceptors. "
                    "It is STRONGLY recommended to use 'wss://' (SSL) for public instances."
                )

        # validation for private IP in advertise URL
        if self.advertise_url and not await is_valid_peer_address(
            self.advertise_url, allow_private=False
        ):
            # If we are in a private network or explicitly allow private PEX, we allow it (with warning)
            if settings.COMETNET_PRIVATE_NETWORK or settings.COMETNET_ALLOW_PRIVATE_PEX:
                logger.warning(
                    "Your COMETNET_ADVERTISE_URL contains a private/internal IP address. "
                    "This is allowed because COMETNET_PRIVATE_NETWORK or COMETNET_ALLOW_PRIVATE_PEX is enabled. "
                    "Public peers may not be able to connect."
                )
            else:
                # Do not allow starting with private IP on public network
                logger.critical(
                    f"\nCometNet failed to start because COMETNET_ADVERTISE_URL ('{self.advertise_url}') "
                    "is a private address.\n"
                    "Public nodes MUST be reachable via a public URL.\n"
                    "Please:\n"
                    "1. Set COMETNET_ADVERTISE_URL to your public URL (wss://your-domain.com/cometnet/ws)\n"
                    "2. Or enable UPnP with COMETNET_UPNP_ENABLED=true\n"
                    "3. Or if you are testing locally, set COMETNET_ALLOW_PRIVATE_PEX=true"
                )
                await logger.complete()
                sys.exit(1)

        # Require advertise_url on public networks
        if not self.advertise_url:
            if (
                not settings.COMETNET_PRIVATE_NETWORK
                and not settings.COMETNET_ALLOW_PRIVATE_PEX
            ):
                upnp_hint = (
                    "   (UPnP is enabled but failed to configure - check your router)\n"
                    if settings.COMETNET_UPNP_ENABLED
                    else "2. Or enable UPnP with COMETNET_UPNP_ENABLED=true (will auto-configure)\n"
                )
                logger.critical(
                    "\nCometNet failed to start because COMETNET_ADVERTISE_URL is not configured.\n"
                    "Without a public URL, your node's local address will be shared with peers,\n"
                    "polluting the network with unreachable addresses.\n\n"
                    "Please:\n"
                    "1. Set COMETNET_ADVERTISE_URL to your public URL (wss://your-domain.com/cometnet/ws)\n"
                    f"{upnp_hint}"
                    "3. Or if you are testing locally, set COMETNET_ALLOW_PRIVATE_PEX=true"
                )
                await logger.complete()
                sys.exit(1)

        # WebSocket reachability check
        # Verify we can connect to our own advertise URL (like a peer would)
        if self.advertise_url and not settings.COMETNET_SKIP_REACHABILITY_CHECK:
            max_retries = settings.COMETNET_REACHABILITY_RETRIES
            retry_delay = settings.COMETNET_REACHABILITY_RETRY_DELAY
            timeout = settings.COMETNET_REACHABILITY_TIMEOUT

            logger.log(
                "COMETNET",
                f"Verifying WebSocket reachability of {self.advertise_url}...",
            )

            is_reachable = False
            result_msg = None

            for attempt in range(1, max_retries + 1):
                if attempt > 1:
                    logger.log(
                        "COMETNET",
                        f"Retry {attempt}/{max_retries} after {retry_delay}s delay...",
                    )
                    await asyncio.sleep(retry_delay)

                is_reachable, result_msg = await check_advertise_url_reachability(
                    self.advertise_url, timeout=timeout
                )

                if is_reachable:
                    if attempt > 1:
                        logger.log(
                            "COMETNET",
                            f"✓ Reachability check passed on attempt {attempt}/{max_retries} ({result_msg})",
                        )
                    else:
                        logger.log(
                            "COMETNET", f"✓ Reachability check passed ({result_msg})"
                        )
                    break
                else:
                    logger.log(
                        "COMETNET",
                        f"✗ Attempt {attempt}/{max_retries} failed: {result_msg}",
                    )

            if not is_reachable:
                logger.critical(
                    f"\nCometNet failed to start: Cannot connect to COMETNET_ADVERTISE_URL\n"
                    f"URL: {self.advertise_url}\n"
                    f"Error: {result_msg}\n"
                    f"Failed after {max_retries} attempts\n\n"
                    "Other peers won't be able to connect to you either.\n\n"
                    "Troubleshooting:\n"
                    f"1. Ensure port {self.listen_port} is open and forwarded correctly\n"
                    "2. If using reverse proxy (e.g., Traefik), ensure WebSocket upgrade headers are forwarded\n"
                    "3. If using Traefik, the reverse proxy may take time to open - increase COMETNET_REACHABILITY_RETRIES/DELAY\n"
                    "4. Test manually: wscat -c " + self.advertise_url + "\n"
                    "5. To skip this check: COMETNET_SKIP_REACHABILITY_CHECK=true"
                )
                await logger.complete()
                sys.exit(1)

        # Start discovery and gossip services
        await self.discovery.start(self.identity.node_id, self.listen_port)
        await self.gossip.start()

        self._running = True
        self._started_at = time.time()

        # Reconnect to known pool peers (from previous sessions)
        await self._reconnect_pool_peers()

        # Start periodic state save task
        self._state_save_task = asyncio.create_task(self._periodic_state_save())

        # Log contribution mode and pool info
        pool_count = len(self.pool_store.get_subscriptions()) if self.pool_store else 0
        pool_info = (
            f", subscribed to {pool_count} pools" if pool_count > 0 else ", open mode"
        )

        alias_info = (
            f" ({settings.COMETNET_NODE_ALIAS})" if settings.COMETNET_NODE_ALIAS else ""
        )

        logger.log(
            "COMETNET",
            f"CometNet started - Node ID: {self.identity.node_id[:8]}{alias_info} (mode: {settings.COMETNET_CONTRIBUTION_MODE}{pool_info})",
        )

    async def stop(self) -> None:
        """Stop the CometNet service."""
        if not self._running:
            return

        logger.log("COMETNET", "Stopping CometNet...")

        self._running = False

        # Stop periodic state save task
        if self._state_save_task:
            self._state_save_task.cancel()
            try:
                await self._state_save_task
            except asyncio.CancelledError:
                pass

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

        # Shutdown the dedicated crypto thread pool
        shutdown_crypto_executor()

        logger.log("COMETNET", "CometNet stopped")

    async def _periodic_state_save(self) -> None:
        """
        Periodically save CometNet state to disk.
        """
        interval = settings.COMETNET_STATE_SAVE_INTERVAL

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

                logger.log("COMETNET", "Periodic state save completed")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Error during periodic state save: {e}")

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
                        # Small delay between connections
                        await asyncio.sleep(0.5)
                except Exception:
                    pass

        if connected > 0:
            logger.log("COMETNET", f"Reconnected to {connected} pool peers")

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
        except Exception as e:
            logger.critical(
                f"\nCometNet failed to start: Invalid COMETNET_ADVERTISE_URL format.\n"
                f"URL: {url}\n"
                f"Error: {e}\n\n"
                "Please provide a valid WebSocket URL like:\n"
                "  wss://your-domain.com/cometnet/ws\n"
                "  ws://123.45.67.89:8765"
            )
            await logger.complete()
            sys.exit(1)

        # Validate scheme
        if parsed.scheme not in ("ws", "wss"):
            logger.critical(
                f"\nCometNet failed to start: Invalid URL scheme '{parsed.scheme}'.\n"
                f"URL: {url}\n\n"
                "COMETNET_ADVERTISE_URL must use 'ws://' or 'wss://' scheme.\n"
                "Examples:\n"
                "  wss://your-domain.com/cometnet/ws (recommended)\n"
                "  ws://123.45.67.89:8765 (unencrypted)"
            )
            await logger.complete()
            sys.exit(1)

        # Validate hostname exists
        hostname = parsed.hostname
        if not hostname:
            logger.critical(
                f"\nCometNet failed to start: No hostname in COMETNET_ADVERTISE_URL.\n"
                f"URL: {url}\n\n"
                "Please specify a hostname or IP address."
            )
            await logger.complete()
            sys.exit(1)

        # Validate port range
        if parsed.port is not None and not (1 <= parsed.port <= 65535):
            logger.critical(
                f"\nCometNet failed to start: Invalid port {parsed.port}.\n"
                f"URL: {url}\n\n"
                "Port must be between 1 and 65535."
            )
            await logger.complete()
            sys.exit(1)

        # Check for internal domains (unless private network is allowed)
        if (
            not settings.COMETNET_PRIVATE_NETWORK
            and not settings.COMETNET_ALLOW_PRIVATE_PEX
        ):
            hostname_lower = hostname.lower()

            # Check for suspicious internal domain patterns
            if is_internal_domain(hostname_lower):
                logger.critical(
                    f"\nCometNet failed to start: COMETNET_ADVERTISE_URL uses an internal domain.\n"
                    f"URL: {url}\n"
                    f"Hostname: {hostname}\n\n"
                    "Internal domains (.local, .internal, .lan, etc.) are not routable on the public internet.\n"
                    "Public nodes MUST use a publicly resolvable domain or IP address.\n\n"
                    "If this is intentional:\n"
                    "1. For private networks: set COMETNET_PRIVATE_NETWORK=true\n"
                    "2. For testing: set COMETNET_ALLOW_PRIVATE_PEX=true"
                )
                await logger.complete()
                sys.exit(1)

            # Check if domain resolves to a private IP (DNS rebinding protection)
            if await is_private_or_internal_ip(hostname_lower):
                logger.critical(
                    f"\nCometNet failed to start: COMETNET_ADVERTISE_URL resolves to a private IP.\n"
                    f"URL: {url}\n"
                    f"Hostname: {hostname}\n\n"
                    "This domain resolves to a private/internal IP address.\n"
                    "This could be a DNS rebinding attack or a misconfiguration.\n"
                    "Public nodes MUST resolve to a public IP address.\n\n"
                    "If this is intentional:\n"
                    "1. For private networks: set COMETNET_PRIVATE_NETWORK=true\n"
                    "2. For testing: set COMETNET_ALLOW_PRIVATE_PEX=true"
                )
                await logger.complete()
                sys.exit(1)

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
        self, message: TorrentAnnounce, exclude: Optional[Set[str]] = None
    ) -> None:
        """Broadcast a gossip message to all peers."""
        await self.transport.broadcast(message, exclude)

    async def _handle_check_torrents_exist(self, info_hashes: List[str]) -> Set[str]:
        """Check if torrents exist locally."""
        # Prefer batch callback
        if self._check_torrents_exist_callback:
            try:
                return await self._check_torrents_exist_callback(info_hashes)
            except Exception:
                return set()

        # Fallback to legacy single callback loop if batch not set
        if self._check_torrent_exists_callback:
            existing = set()
            for ih in info_hashes:
                try:
                    if await self._check_torrent_exists_callback(ih):
                        existing.add(ih)
                except Exception:
                    pass
            return existing

        return set()

    async def _handle_received_torrent(self, metadata: TorrentMetadata) -> None:
        """Handle a torrent received from the network."""
        if self._save_torrent_callback:
            try:
                await self._save_torrent_callback(metadata)
            except Exception:
                pass

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
                    public_key=m.get("public_key", ""),
                    role=MemberRole(m.get("role", "member")),
                    added_at=m.get("added_at", 0),
                    added_by=m.get("added_by", ""),
                    contribution_count=m.get("contribution_count", 0),
                    last_seen=m.get("last_seen", 0.0),
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
                    except Exception:
                        pass
                return

            # Store the manifest (validation happens inside)
            if await self.pool_store.validate_manifest(manifest, self.keystore):
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
                                await run_in_executor(shutil.rmtree, pool_inv_dir)
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

        if not await validate_message_security(
            message, sender_id, self.keystore, self.reputation
        ):
            return

        pool_id = message.pool_id
        invite_code = message.invite_code
        requester_key = message.requester_key

        # Get the invite
        invite = self.pool_store.get_invite(pool_id, invite_code)
        if not invite or not invite.is_valid():
            return

        # Get the manifest
        manifest = self.pool_store.get_manifest(pool_id)
        if not manifest:
            return

        # Check if already a member
        if manifest.is_member(requester_key):
            # Already a member, just send them the manifest
            pass
        else:
            # Add as member

            manifest.members.append(
                PoolMember(
                    public_key=requester_key,
                    role=MemberRole.MEMBER,
                    added_by=invite.created_by,
                    alias=message.alias,
                )
            )
            manifest.version += 1
            manifest.updated_at = time.time()

            # Increment invite usage
            invite.uses += 1
            await self.pool_store._save_invite(invite)

            # Save updated manifest
            await self.pool_store.store_manifest(manifest, self.identity)

            requester_node_id = NodeIdentity.node_id_from_public_key(requester_key)
            logger.log(
                "COMETNET",
                f"Added {requester_node_id[:8]} to pool {pool_id} via join request",
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

        if not await validate_message_security(
            message, sender_id, self.keystore, self.reputation
        ):
            return

        current_manifest = self.pool_store.get_manifest(message.pool_id)

        current_manifest = self.pool_store.get_manifest(message.pool_id)
        if not current_manifest:
            return

        # Work on a copy to verify before updating
        manifest = PoolManifest(**current_manifest.model_dump())

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
        else:
            # Normal case: admin-initiated update
            # Verify the updater is an admin
            if not manifest.is_admin(message.updated_by):
                return

            # Verify signature of the update message
            if self.keystore:
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
        elif message.action == "demote":
            if target_member:
                target_member.role = MemberRole.MEMBER
                modified = True

        if not modified:
            return

        # Update version (we assume it increments by 1)
        manifest.version += 1
        manifest.updated_at = message.timestamp

        # For "leave" action, we don't require manifest signatures
        # The message signature from the leaving member is sufficient
        if is_self_leave:
            # Just store the updated manifest (no signature verification needed)
            # The message signature was already verified
            await self.pool_store.store_manifest(manifest)
            logger.log(
                "COMETNET",
                f"Member {NodeIdentity.node_id_from_public_key(message.member_key)[:8]} left pool {message.pool_id}",
            )
            # Re-broadcast to others
            await self.transport.broadcast(message, exclude={sender_id})
            return

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
                f"Applied pool update: {message.action} {message.member_key[:8]}",
            )

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
        logger.log(
            "COMETNET",
            f"Pool {message.pool_id} deleted by creator {message.deleted_by[:8]}",
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

    async def broadcast_torrents(self, metadata_list: List[Any]) -> None:
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

        client_ip, from_proxy = get_client_ip_any(websocket)

        node_id = await self.transport.handle_incoming_connection(websocket, client_ip)

        if node_id:
            # Record in discovery for future PEX
            real_address = self.transport.get_peer_address(node_id)
            if real_address:
                self.discovery.record_incoming_connection(node_id, real_address)

            # Sync manifests with the newly connected peer
            asyncio.create_task(self._sync_manifests_with_peers([node_id]))

    async def _on_peer_connected(self, node_id: str, address: Optional[str]) -> None:
        """Callback when a peer connects via the native WebSocket server."""
        if address:
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
                except Exception:
                    pass

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
            "contribution_mode": settings.COMETNET_CONTRIBUTION_MODE,
            "pool_stats": self.pool_store.get_stats() if self.pool_store else {},
            # Private network info
            "private_network": settings.COMETNET_PRIVATE_NETWORK,
            "network_id": settings.COMETNET_NETWORK_ID
            if settings.COMETNET_PRIVATE_NETWORK
            else None,
        }

    async def get_peers(self) -> Dict[str, Any]:
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
    ) -> Dict:
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
                alias=settings.COMETNET_NODE_ALIAS,
            )
            join_request.signature = await self.identity.sign_hex_async(
                join_request.to_signable_bytes()
            )

            success = await self.transport.send_to_peer(peer_id, join_request)
            if not success:
                return False

            logger.log(
                "COMETNET", f"Sent join request for pool {pool_id} to {peer_id[:8]}"
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
            f"Changed role of {member.node_id[:8]} to {new_role} in pool {pool_id}",
        )
        return True

    async def leave_pool(self, pool_id: str) -> bool:
        """Leave a pool (self-removal). Any member except creator can leave."""
        if not self._running or not self.pool_store:
            return False

        try:
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
                raise ValueError(
                    "Creator cannot leave the pool. Delete the pool instead."
                )

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
            logger.log("COMETNET", f"Broadcasted leave from pool {pool_id}")

            # Now do local cleanup
            result = await self.pool_store.leave_pool(
                pool_id=pool_id,
                identity=self.identity,
            )
            if result:
                logger.log("COMETNET", f"Successfully left pool {pool_id}")
            return result
        except (PermissionError, ValueError) as e:
            logger.warning(f"Failed to leave pool: {e}")
            raise

    async def _load_state(self) -> None:
        """Load saved state from disk."""
        state_path = self.keys_dir / self.STATE_FILE

        if not state_path.exists():
            return

        try:
            async with aiofiles.open(state_path, "r") as f:
                content = await f.read()
                state = json.loads(content)

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
                        f"State file integrity check failed (Stored: {stored_hash[:8]}..., Expected: {expected_hash[:8]}...). "
                        "Loading state anyway to prevent data loss."
                    )

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
                await self.discovery.from_dict(state["discovery"])

            # Load gossip stats
            if "gossip" in state and self.gossip:
                self.gossip.from_dict(state["gossip"])
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
                "gossip": self.gossip.to_dict() if self.gossip else {},
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

            async with aiofiles.open(state_path, "w") as f:
                await f.write(json.dumps(state, indent=2))
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

    if settings.FASTAPI_WORKERS > 1:
        logger.critical(
            f"\nCometNet failed to start because FASTAPI_WORKERS is set to {settings.FASTAPI_WORKERS}.\n"
            "You cannot run CometNet in basic mode (non-relay) with multiple workers.\n"
            "Please:\n"
            "1. Use CometNet Relay Mode (COMETNET_RELAY_URL=...)\n"
            "2. Or set FASTAPI_WORKERS=1"
        )
        sys.exit(1)

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
