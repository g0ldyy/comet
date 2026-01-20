"""
CometNet Pools Module

Implements the Trust Pools system for CometNet.
Pools are groups of nodes that trust each other and share torrents.

Key concepts:
- Pool: A group with an ID, members, and rules
- Membership: Roles (admin, member) within a pool
- Invitations: Links to join pools
- Subscriptions: Pools this node trusts (accepts torrents from)
"""

import json
import secrets
import time
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Set

import msgpack
from pydantic import BaseModel, Field, computed_field, field_validator

from comet.cometnet.crypto import NodeIdentity
from comet.cometnet.utils import canonicalize_data
from comet.core.logger import logger
from comet.core.models import settings


class MemberRole(str, Enum):
    """Roles within a pool."""

    CREATOR = "creator"  # Pool creator (cannot be demoted)
    ADMIN = "admin"
    MEMBER = "member"


class JoinMode(str, Enum):
    """How nodes can join a pool."""

    INVITE = "invite"  # Requires invitation link


class PoolMember(BaseModel):
    """A member of a pool."""

    public_key: str
    role: MemberRole = MemberRole.MEMBER
    added_at: float = Field(default_factory=time.time)
    added_by: str = ""  # Public key of admin who added this member

    # Stats (local tracking)
    contribution_count: int = 0
    last_seen: float = 0.0

    @computed_field
    @property
    def node_id(self) -> str:
        """Derive node_id from public_key (SHA256 hash). Matches peer IDs in transport."""
        return NodeIdentity.node_id_from_public_key(self.public_key)


class PoolManifest(BaseModel):
    """
    Definition of a Trust Pool.

    This is the core data structure that defines a pool and is
    propagated across the network.
    """

    # Identity (immutable after creation)
    pool_id: str
    created_at: float = Field(default_factory=time.time)
    creator_key: str  # Public key of creator

    # Metadata (modifiable by admins)
    display_name: str
    description: str = ""

    # Members
    members: List[PoolMember] = Field(default_factory=list)

    # Rules
    join_mode: JoinMode = JoinMode.INVITE

    # Versioning
    version: int = 1
    updated_at: float = Field(default_factory=time.time)

    # Signatures from admins (at least 1 required for validity)
    # Maps admin public key -> signature
    signatures: Dict[str, str] = Field(default_factory=dict)

    @field_validator("pool_id")
    @classmethod
    def validate_pool_id(cls, v: str) -> str:
        """Validate pool ID format."""
        v = v.lower().strip()
        if len(v) < 2 or len(v) > 64:
            raise ValueError("pool_id must be 2-64 characters")
        if not v.replace("-", "").replace("_", "").isalnum():
            raise ValueError("pool_id must be alphanumeric with - or _")
        return v

    def get_admins(self) -> List[PoolMember]:
        """Get all admin members (including creator)."""
        return [
            m for m in self.members if m.role in (MemberRole.ADMIN, MemberRole.CREATOR)
        ]

    def get_member(self, public_key: str) -> Optional[PoolMember]:
        """Get a member by public key."""
        return next((m for m in self.members if m.public_key == public_key), None)

    def is_admin(self, public_key: str) -> bool:
        """Check if a public key belongs to an admin or creator."""
        member = self.get_member(public_key)
        return member is not None and member.role in (
            MemberRole.ADMIN,
            MemberRole.CREATOR,
        )

    def is_member(self, public_key: str) -> bool:
        """Check if a public key belongs to any member."""
        return self.get_member(public_key) is not None

    def to_signable_bytes(self) -> bytes:
        """
        Get bytes for signing (excludes signatures field).

        Standardization Rules:
        - Timestamps: Converted to integers (floor) to prevent float precision drift.
        - Computed Fields: 'node_id' is EXCLUDED (must be re-computed by receiver).
        - Local Stats: 'contribution_count' and 'last_seen' are EXCLUDED (not part of consensus).
        """
        data = self.model_dump(exclude={"signatures"})

        if "members" in data and isinstance(data["members"], list):
            for m in data["members"]:
                if "node_id" in m:
                    del m["node_id"]

                if "contribution_count" in m:
                    del m["contribution_count"]
                if "last_seen" in m:
                    del m["last_seen"]

                if "added_at" in m:
                    m["added_at"] = int(m["added_at"])

        if "created_at" in data:
            data["created_at"] = int(data["created_at"])
        if "updated_at" in data:
            data["updated_at"] = int(data["updated_at"])

        # Sort members by public key for deterministic ordering
        if "members" in data and isinstance(data["members"], list):
            data["members"] = sorted(
                data["members"], key=lambda m: m.get("public_key", "")
            )

        # Ensure consistent ordering for deterministic serialization
        return msgpack.packb(canonicalize_data(data))

    def to_bytes(self) -> bytes:
        """Serialize the manifest to MsgPack bytes."""
        return msgpack.packb(self.model_dump())

    @classmethod
    def from_bytes(cls, data: bytes) -> "PoolManifest":
        """Deserialize from MsgPack bytes."""
        return cls.model_validate(msgpack.unpackb(data, raw=False))


class PoolInvite(BaseModel):
    """An invitation to join a pool."""

    pool_id: str
    invite_code: str = Field(default_factory=lambda: secrets.token_urlsafe(16))
    created_by: str  # Admin public key
    created_at: float = Field(default_factory=time.time)
    expires_at: Optional[float] = None  # None = never expires
    max_uses: Optional[int] = None  # None = unlimited
    uses: int = 0
    signature: str = ""  # Signature from creating admin
    node_url: str = ""  # URL of the node that created the invite

    def is_valid(self) -> bool:
        """Check if the invite is still valid."""
        if self.expires_at and time.time() > self.expires_at:
            return False
        if self.max_uses is not None and self.uses >= self.max_uses:
            return False
        return True

    def to_signable_bytes(self) -> bytes:
        """Get bytes for signing."""
        data = self.model_dump(exclude={"signature", "uses"})
        return msgpack.packb(canonicalize_data(data))

    def to_link(self) -> str:
        """
        Generate a shareable invite link.

        Format: cometnet://join?pool=<pool_id>&code=<invite_code>&node=<node_url>
        This format allows other nodes to:
        1. Know which pool to join
        2. Have the invite code
        3. Connect to the creator node to fetch the manifest
        """
        import urllib.parse

        params = {
            "pool": self.pool_id,
            "code": self.invite_code,
        }
        if self.node_url:
            params["node"] = self.node_url
        return f"cometnet://join?{urllib.parse.urlencode(params)}"

    @classmethod
    def parse_link(cls, link: str) -> Optional[Dict[str, str]]:
        """
        Parse an invite link into its components.

        Returns dict with 'pool', 'code', and optionally 'node' keys.
        """
        import urllib.parse

        if not link.startswith("cometnet://join?"):
            # Try legacy format
            if link.startswith("cometnet://pool/"):
                parts = link.replace("cometnet://pool/", "").split("/invite/")
                if len(parts) == 2:
                    return {"pool": parts[0], "code": parts[1]}
            return None
        query = link.split("?", 1)[1] if "?" in link else ""
        params = urllib.parse.parse_qs(query)
        result = {}
        if "pool" in params:
            result["pool"] = params["pool"][0]
        if "code" in params:
            result["code"] = params["code"][0]
        if "node" in params:
            result["node"] = params["node"][0]
        return result if result else None


class PoolStore:
    """
    Manages pool manifests, memberships, and subscriptions.

    Storage structure:
    pools_dir/
    ├── manifests/
    │   ├── pool-id-1.json
    │   └── pool-id-2.json
    ├── invites/
    │   └── pool-id-1/
    │       ├── invite-code-1.json
    │       └── invite-code-2.json
    ├── memberships.json    # Pools where we are a member
    ├── subscriptions.json  # Pools we subscribe to
    └── pool_peers.json     # Known peer addresses for each pool
    """

    def __init__(self, pools_dir: Optional[str] = None):
        self.pools_dir = Path(pools_dir or settings.COMETNET_POOLS_DIR)
        self.manifests_dir = self.pools_dir / "manifests"
        self.invites_dir = self.pools_dir / "invites"

        # In-memory caches
        self._manifests: Dict[str, PoolManifest] = {}
        self._memberships: Set[str] = set()  # Pool IDs where we are member
        self._subscriptions: Set[str] = set()  # Pool IDs we trust
        self._invites: Dict[
            str, Dict[str, PoolInvite]
        ] = {}  # pool_id -> {code -> invite}
        self._pool_peers: Dict[str, Set[str]] = {}  # pool_id -> set of peer addresses

        # Ensure directories exist
        self.manifests_dir.mkdir(parents=True, exist_ok=True)
        self.invites_dir.mkdir(parents=True, exist_ok=True)

    async def load(self) -> None:
        """Load all data from disk."""
        await self._load_manifests()
        await self._load_memberships()
        await self._load_subscriptions()
        await self._load_invites()
        await self._load_pool_peers()

        logger.log(
            "COMETNET",
            f"Loaded {len(self._manifests)} pools, "
            f"{len(self._memberships)} memberships, "
            f"{len(self._subscriptions)} subscriptions",
        )

    async def save(self) -> None:
        """Save all data to disk."""
        await self._save_memberships()
        await self._save_subscriptions()
        await self._save_pool_peers()
        # Manifests and invites are saved individually

    # ==================== Manifest Operations ====================

    def get_manifest(self, pool_id: str) -> Optional[PoolManifest]:
        """Get a pool manifest by ID."""
        return self._manifests.get(pool_id)

    def get_all_manifests(self) -> Dict[str, PoolManifest]:
        """Get all known pool manifests."""
        return self._manifests.copy()

    async def store_manifest(
        self,
        manifest: PoolManifest,
        identity=None,
    ) -> bool:
        """
        Store or update a pool manifest.

        Args:
            manifest: The pool manifest to store
            identity: Optional NodeIdentity for signing (if we're admin)

        Returns:
            True if stored successfully
        """
        # If we have an identity and we're an admin, sign the manifest
        if identity and manifest.is_admin(identity.public_key_hex):
            manifest.signatures[
                identity.public_key_hex
            ] = await identity.sign_hex_async(manifest.to_signable_bytes())

        # Store in memory
        self._manifests[manifest.pool_id] = manifest

        # Persist to disk
        manifest_path = self.manifests_dir / f"{manifest.pool_id}.json"
        try:
            with open(manifest_path, "w") as f:
                json.dump(manifest.model_dump(), f, indent=2)
            return True
        except Exception as e:
            logger.warning(f"Failed to save pool manifest {manifest.pool_id}: {e}")
            return False

    async def create_pool(
        self,
        pool_id: str,
        display_name: str,
        identity,  # NodeIdentity
        description: str = "",
        join_mode: JoinMode = JoinMode.INVITE,
    ) -> PoolManifest:
        """
        Create a new pool with this node as the admin.

        Args:
            pool_id: Unique identifier for the pool
            display_name: Human-readable name
            identity: NodeIdentity of the creator
            description: Optional description
            join_mode: How nodes can join
        Returns:
            The created PoolManifest
        """
        if pool_id in self._manifests:
            raise ValueError(f"Pool {pool_id} already exists")

        # Create manifest
        manifest = PoolManifest(
            pool_id=pool_id,
            display_name=display_name,
            description=description,
            creator_key=identity.public_key_hex,
            join_mode=JoinMode.INVITE,
            members=[
                PoolMember(
                    public_key=identity.public_key_hex,
                    role=MemberRole.CREATOR,
                    added_by=identity.public_key_hex,
                )
            ],
        )

        # Sign and store
        await self.store_manifest(manifest, identity)

        # Auto-join as member
        self._memberships.add(pool_id)
        await self._save_memberships()

        logger.log("COMETNET", f"Created pool '{display_name}' ({pool_id})")
        return manifest

    async def delete_pool(self, pool_id: str) -> bool:
        """Delete a pool manifest (local only)."""
        if pool_id not in self._manifests:
            return False

        del self._manifests[pool_id]
        self._memberships.discard(pool_id)
        self._subscriptions.discard(pool_id)

        # Also remove pool peers for this pool
        if pool_id in self._pool_peers:
            del self._pool_peers[pool_id]

        # Delete invites for this pool
        if pool_id in self._invites:
            # Delete invite files
            pool_inv_dir = self.invites_dir / pool_id
            if pool_inv_dir.exists():
                try:
                    import shutil

                    shutil.rmtree(pool_inv_dir)
                except Exception:
                    pass
            del self._invites[pool_id]

        manifest_path = self.manifests_dir / f"{pool_id}.json"
        try:
            manifest_path.unlink(missing_ok=True)
        except Exception:
            pass

        await self._save_memberships()
        await self._save_subscriptions()
        await self._save_pool_peers()
        return True

    # ==================== Membership Operations ====================

    def is_member_of(self, pool_id: str) -> bool:
        """Check if we are a member of a pool."""
        return pool_id in self._memberships

    def get_memberships(self) -> Set[str]:
        """Get all pools we are a member of."""
        return self._memberships.copy()

    async def add_member(
        self,
        pool_id: str,
        new_member_key: str,
        identity,  # NodeIdentity (must be admin)
        role: MemberRole = MemberRole.MEMBER,
    ) -> bool:
        """
        Add a new member to a pool (admin action).

        Args:
            pool_id: Pool to add member to
            new_member_key: Public key of new member
            identity: Admin's identity
            role: Role for the new member

        Returns:
            True if member was added
        """
        manifest = self._manifests.get(pool_id)
        if not manifest:
            raise ValueError(f"Pool {pool_id} not found")

        if not manifest.is_admin(identity.public_key_hex):
            raise PermissionError("Only admins can add members")

        if manifest.is_member(new_member_key):
            return False  # Already a member

        # Add member
        manifest.members.append(
            PoolMember(
                public_key=new_member_key,
                role=role,
                added_by=identity.public_key_hex,
            )
        )

        # Update version
        manifest.version += 1
        manifest.updated_at = time.time()

        # Re-sign and save
        await self.store_manifest(manifest, identity)

        new_member_id = NodeIdentity.node_id_from_public_key(new_member_key)
        logger.log("COMETNET", f"Added member {new_member_id[:8]} to pool {pool_id}")
        return True

    async def remove_member(
        self,
        pool_id: str,
        member_key: str,
        identity,  # NodeIdentity (must be admin)
    ) -> bool:
        """Remove a member from a pool (admin action)."""
        manifest = self._manifests.get(pool_id)
        if not manifest:
            raise ValueError(f"Pool {pool_id} not found")

        if not manifest.is_admin(identity.public_key_hex):
            raise PermissionError("Only admins can remove members")

        member = manifest.get_member(member_key)
        if not member:
            return False

        # Don't allow removing the creator
        if member.role == MemberRole.CREATOR:
            raise ValueError("Cannot remove the pool creator")

        # Don't allow removing the last admin
        if member.role == MemberRole.ADMIN:
            admin_count = len(manifest.get_admins())
            if admin_count <= 1:
                raise ValueError("Cannot remove the last admin")

        manifest.members = [m for m in manifest.members if m.public_key != member_key]
        manifest.version += 1
        manifest.updated_at = time.time()

        await self.store_manifest(manifest, identity)

        logger.log(
            "COMETNET", f"Removed member {member.node_id[:8]} from pool {pool_id}"
        )
        return True

    async def leave_pool(
        self,
        pool_id: str,
        identity,  # NodeIdentity of the leaving member
    ) -> bool:
        """
        Leave a pool (self-removal).

        Any member can leave a pool, except the creator who must delete the pool instead.
        """
        manifest = self._manifests.get(pool_id)
        if not manifest:
            raise ValueError(f"Pool {pool_id} not found")

        member = manifest.get_member(identity.public_key_hex)
        if not member:
            return False  # Not a member

        # Creator cannot leave - they must delete the pool
        if member.role == MemberRole.CREATOR:
            raise ValueError("Creator cannot leave the pool. Delete the pool instead.")

        # If leaving admin is the last admin (besides creator), prevent it
        if member.role == MemberRole.ADMIN:
            admin_count = len(manifest.get_admins())
            # get_admins includes creator, so we check if there are other admins
            if admin_count <= 1:
                raise ValueError(
                    "Cannot leave as the last admin. Promote another member first."
                )

        # Remove from our memberships
        self._memberships.discard(pool_id)
        await self._save_memberships()

        # Unsubscribe from the pool
        self._subscriptions.discard(pool_id)
        await self._save_subscriptions()

        # Remove pool peers since we don't need to reconnect to this pool
        if pool_id in self._pool_peers:
            del self._pool_peers[pool_id]
            await self._save_pool_peers()

        # Remove the manifest from local storage
        if pool_id in self._manifests:
            del self._manifests[pool_id]
            manifest_path = self.manifests_dir / f"{pool_id}.json"
            try:
                manifest_path.unlink(missing_ok=True)
            except Exception:
                pass

        logger.log("COMETNET", f"Left pool {pool_id}")
        return True

    async def promote_member(
        self,
        pool_id: str,
        member_key: str,
        identity,  # NodeIdentity (must be admin)
    ) -> bool:
        """Promote a member to admin."""
        manifest = self._manifests.get(pool_id)
        if not manifest:
            raise ValueError(f"Pool {pool_id} not found")

        if not manifest.is_admin(identity.public_key_hex):
            raise PermissionError("Only admins can promote members")

        member = manifest.get_member(member_key)
        if not member or member.role in (MemberRole.ADMIN, MemberRole.CREATOR):
            return False  # Already admin/creator or not found

        member.role = MemberRole.ADMIN
        manifest.version += 1
        manifest.updated_at = time.time()

        await self.store_manifest(manifest, identity)

        logger.log(
            "COMETNET", f"Promoted {member.node_id[:8]} to admin in pool {pool_id}"
        )
        return True

    # ==================== Subscription Operations ====================

    def is_subscribed(self, pool_id: str) -> bool:
        """Check if we subscribe to a pool."""
        return pool_id in self._subscriptions

    def get_subscriptions(self) -> Set[str]:
        """Get all pools we subscribe to."""
        return self._subscriptions.copy()

    async def subscribe(self, pool_id: str) -> None:
        """Subscribe to a pool (trust its members' torrents)."""
        self._subscriptions.add(pool_id)
        await self._save_subscriptions()
        logger.log("COMETNET", f"Subscribed to pool {pool_id}")

    async def unsubscribe(self, pool_id: str) -> None:
        """Unsubscribe from a pool."""
        self._subscriptions.discard(pool_id)
        await self._save_subscriptions()
        logger.log("COMETNET", f"Unsubscribed from pool {pool_id}")

    def is_contributor_trusted(
        self, contributor_key: str, pool_id: Optional[str] = None
    ) -> bool:
        """
        Check if a contributor is trusted (member of a subscribed pool).

        If no pools are subscribed (open mode), all contributors are trusted.

        Args:
            contributor_key: Public key of the contributor
            pool_id: Optional pool ID claimed by the torrent

        Returns:
            True if the contributor should be trusted
        """
        # Open mode: trust everyone
        if not self._subscriptions:
            return True

        # If pool is specified, check if we subscribe to it and contributor is member
        if pool_id:
            if pool_id not in self._subscriptions:
                return False
            manifest = self._manifests.get(pool_id)
            if manifest and manifest.is_member(contributor_key):
                return True
            return False

        # Check if contributor is member of any subscribed pool
        for sub_pool_id in self._subscriptions:
            manifest = self._manifests.get(sub_pool_id)
            if manifest and manifest.is_member(contributor_key):
                return True

        return False

    # ==================== Invite Operations ====================

    async def create_invite(
        self,
        pool_id: str,
        identity,  # NodeIdentity (must be admin)
        expires_in: Optional[int] = None,  # Seconds
        max_uses: Optional[int] = None,
        node_url: Optional[str] = None,  # URL of this node for remote joining
    ) -> PoolInvite:
        """Create an invitation to join a pool."""
        manifest = self._manifests.get(pool_id)
        if not manifest:
            raise ValueError(f"Pool {pool_id} not found")

        if not manifest.is_admin(identity.public_key_hex):
            raise PermissionError("Only admins can create invites")

        invite = PoolInvite(
            pool_id=pool_id,
            created_by=identity.public_key_hex,
            expires_at=time.time() + expires_in if expires_in else None,
            max_uses=max_uses,
            node_url=node_url or "",
        )

        # Sign the invite
        invite.signature = await identity.sign_hex_async(invite.to_signable_bytes())

        # Store invite
        if pool_id not in self._invites:
            self._invites[pool_id] = {}
        self._invites[pool_id][invite.invite_code] = invite

        # Persist
        await self._save_invite(invite)

        logger.log("COMETNET", f"Created invite for pool {pool_id}: {invite.to_link()}")
        return invite

    def get_invites(self, pool_id: str) -> List[PoolInvite]:
        """Get all invites for a pool."""
        return list(self._invites.get(pool_id, {}).values())

    async def delete_invite(self, pool_id: str, invite_code: str) -> bool:
        """Delete an invite."""
        if pool_id in self._invites and invite_code in self._invites[pool_id]:
            # Delete from disk first
            pool_inv_dir = self.invites_dir / pool_id
            invite_file = pool_inv_dir / f"{invite_code}.json"
            if invite_file.exists():
                try:
                    invite_file.unlink()
                except Exception as e:
                    logger.warning(f"Failed to delete invite file: {e}")

            # Delete from memory
            del self._invites[pool_id][invite_code]
            logger.log("COMETNET", f"Deleted invite {invite_code} from pool {pool_id}")
            return True
        return False

    def get_invite(self, pool_id: str, invite_code: str) -> Optional[PoolInvite]:
        """Get an invite by pool ID and code."""
        return self._invites.get(pool_id, {}).get(invite_code)

    async def use_invite(
        self,
        pool_id: str,
        invite_code: str,
        identity,  # NodeIdentity of the joining node
    ) -> bool:
        """
        Use an invitation to join a pool.

        Args:
            pool_id: Pool to join
            invite_code: The invitation code
            identity: Identity of the node joining

        Returns:
            True if successfully joined
        """
        invite = self.get_invite(pool_id, invite_code)
        if not invite or not invite.is_valid():
            return False

        manifest = self._manifests.get(pool_id)
        if not manifest:
            return False

        # Already a member?
        if manifest.is_member(identity.public_key_hex):
            return True

        # Add as member (note: this is local, needs to be propagated)
        manifest.members.append(
            PoolMember(
                public_key=identity.public_key_hex,
                role=MemberRole.MEMBER,
                added_by=invite.created_by,
            )
        )

        manifest.version += 1
        manifest.updated_at = time.time()

        # Increment invite usage
        invite.uses += 1
        await self._save_invite(invite)

        # Store manifest (we can't sign it as we're not admin)
        await self.store_manifest(manifest)

        # Mark as member
        self._memberships.add(pool_id)
        await self._save_memberships()

        logger.log("COMETNET", f"Joined pool {pool_id} via invite")
        return True

    # ==================== Persistence ====================

    async def _load_manifests(self) -> None:
        """Load all manifests from disk."""
        self._manifests.clear()

        if not self.manifests_dir.exists():
            return

        for manifest_file in self.manifests_dir.glob("*.json"):
            try:
                with open(manifest_file, "r") as f:
                    data = json.load(f)
                manifest = PoolManifest.model_validate(data)
                self._manifests[manifest.pool_id] = manifest
            except Exception as e:
                logger.warning(f"Failed to load manifest {manifest_file}: {e}")

    async def _load_memberships(self) -> None:
        """Load memberships from disk."""
        self._memberships.clear()
        memberships_file = self.pools_dir / "memberships.json"

        if memberships_file.exists():
            try:
                with open(memberships_file, "r") as f:
                    self._memberships = set(json.load(f))
            except Exception as e:
                logger.warning(f"Failed to load memberships: {e}")

    async def _save_memberships(self) -> None:
        """Save memberships to disk."""
        memberships_file = self.pools_dir / "memberships.json"
        try:
            with open(memberships_file, "w") as f:
                json.dump(list(self._memberships), f)
        except Exception as e:
            logger.warning(f"Failed to save memberships: {e}")

    async def _load_subscriptions(self) -> None:
        """Load subscriptions from disk."""
        self._subscriptions.clear()

        # Load from settings first
        if settings.COMETNET_TRUSTED_POOLS:
            self._subscriptions.update(settings.COMETNET_TRUSTED_POOLS)

        # Then from file (can add more)
        subscriptions_file = self.pools_dir / "subscriptions.json"
        if subscriptions_file.exists():
            try:
                with open(subscriptions_file, "r") as f:
                    self._subscriptions.update(json.load(f))
            except Exception as e:
                logger.warning(f"Failed to load subscriptions: {e}")

    async def _save_subscriptions(self) -> None:
        """Save subscriptions to disk."""
        subscriptions_file = self.pools_dir / "subscriptions.json"
        try:
            with open(subscriptions_file, "w") as f:
                json.dump(list(self._subscriptions), f)
        except Exception as e:
            logger.warning(f"Failed to save subscriptions: {e}")

    async def _load_invites(self) -> None:
        """Load invites from disk."""
        self._invites.clear()

        if not self.invites_dir.exists():
            return

        for pool_dir in self.invites_dir.iterdir():
            if pool_dir.is_dir():
                pool_id = pool_dir.name
                self._invites[pool_id] = {}

                for invite_file in pool_dir.glob("*.json"):
                    try:
                        with open(invite_file, "r") as f:
                            data = json.load(f)
                        invite = PoolInvite.model_validate(data)
                        if invite.is_valid():
                            self._invites[pool_id][invite.invite_code] = invite
                        else:
                            # Clean up expired invites
                            invite_file.unlink()
                    except Exception as e:
                        logger.warning(f"Failed to load invite {invite_file}: {e}")

    async def _save_invite(self, invite: PoolInvite) -> None:
        """Save an invite to disk."""
        pool_inv_dir = self.invites_dir / invite.pool_id
        pool_inv_dir.mkdir(parents=True, exist_ok=True)

        invite_file = pool_inv_dir / f"{invite.invite_code}.json"
        try:
            with open(invite_file, "w") as f:
                json.dump(invite.model_dump(), f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save invite: {e}")

    async def _load_pool_peers(self) -> None:
        """Load known pool peers from disk."""
        self._pool_peers.clear()
        peers_file = self.pools_dir / "pool_peers.json"

        if peers_file.exists():
            try:
                with open(peers_file, "r") as f:
                    data = json.load(f)
                # Convert lists back to sets
                for pool_id, peers in data.items():
                    self._pool_peers[pool_id] = set(peers)
            except Exception as e:
                logger.warning(f"Failed to load pool peers: {e}")

    async def _save_pool_peers(self) -> None:
        """Save known pool peers to disk."""
        peers_file = self.pools_dir / "pool_peers.json"
        try:
            # Convert sets to lists for JSON serialization
            data = {pid: list(peers) for pid, peers in self._pool_peers.items()}
            with open(peers_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save pool peers: {e}")

    def add_pool_peer(self, pool_id: str, peer_address: str) -> None:
        """
        Add a known peer address for a pool.

        This is called when we receive a manifest from a peer,
        so we can reconnect to them later.
        """
        if not peer_address:
            return
        if pool_id not in self._pool_peers:
            self._pool_peers[pool_id] = set()
        self._pool_peers[pool_id].add(peer_address)

    def get_pool_peers(self, pool_id: str) -> Set[str]:
        """Get known peer addresses for a pool."""
        return self._pool_peers.get(pool_id, set()).copy()

    def get_all_pool_peers(self) -> Dict[str, Set[str]]:
        """Get all known pool peers for all pools we're a member of."""
        result: Dict[str, Set[str]] = {}
        for pool_id in self._memberships:
            peers = self._pool_peers.get(pool_id, set())
            if peers:
                result[pool_id] = peers.copy()
        return result

    # ==================== Contribution Tracking ====================

    async def record_contribution(
        self,
        contributor_public_key: str,
        pool_id: Optional[str] = None,
        count: int = 1,
    ) -> bool:
        """
        Record a contribution from a pool member.

        This increments the contribution_count for the member and persists
        the change to disk. If pool_id is not specified, the contribution
        is recorded in all pools the member belongs to.

        Args:
            contributor_public_key: Public key of the contributor
            pool_id: Optional pool ID to search in (if specified, only that pool is updated)
            count: Number of contributions to add (default 1)

        Returns:
            True if contribution was recorded in at least one pool, False if member not found
        """
        # If pool_id is specified, only update that specific pool
        if pool_id:
            manifest = self._manifests.get(pool_id)
            if manifest:
                member = manifest.get_member(contributor_public_key)
                if member:
                    member.contribution_count += count
                    member.last_seen = time.time()
                    await self._save_manifest_async(manifest)
                    return True
            return False

        # No pool_id specified: record contribution in all pools the member belongs to
        recorded = False
        for manifest in self._manifests.values():
            member = manifest.get_member(contributor_public_key)
            if member:
                member.contribution_count += count
                member.last_seen = time.time()
                await self._save_manifest_async(manifest)
                recorded = True

        return recorded

    async def _save_manifest_async(self, manifest: PoolManifest) -> None:
        """Save a manifest to disk (without re-signing)."""
        manifest_path = self.manifests_dir / f"{manifest.pool_id}.json"
        try:
            with open(manifest_path, "w") as f:
                json.dump(manifest.model_dump(), f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save pool manifest {manifest.pool_id}: {e}")

    # ==================== Validation ====================

    async def validate_manifest(self, manifest: PoolManifest, keystore=None) -> bool:
        """
        Validate a pool manifest.

        Checks:
        1. At least one valid admin signature
        2. Creator is an admin
        3. Basic structure validity

        Args:
            manifest: The manifest to validate
            keystore: Optional keystore (not used for direct signature verification but kept for API compatibility)

        Returns:
            True if the manifest is valid
        """
        from comet.cometnet.crypto import NodeIdentity

        # Must have at least one admin
        admins = manifest.get_admins()
        if not admins:
            return False

        # Creator must have admin privileges (CREATOR or ADMIN role)
        try:
            signable_data = manifest.to_signable_bytes()

            for admin_key, signature in manifest.signatures.items():
                if manifest.is_admin(admin_key):
                    if await NodeIdentity.verify_hex_async(
                        signable_data, signature, admin_key
                    ):
                        return True
        except Exception as e:
            logger.debug(f"Validation error for pool {manifest.pool_id}: {e}")
            pass

        if manifest.signatures:
            try:
                admin_key = next(iter(manifest.signatures.keys()))
                member = manifest.get_member(admin_key)
                admin_id = member.node_id if member else admin_key
                logger.warning(
                    f"Invalid pool manifest signature from admin {admin_id[:8]}"
                )
            except Exception:
                pass

        return False

    # ==================== Stats ====================

    def get_stats(self) -> Dict:
        """Get pool store statistics."""
        return {
            "pools_known": len(self._manifests),
            "memberships": len(self._memberships),
            "subscriptions": len(self._subscriptions),
            "pool_ids": list(self._manifests.keys()),
            "member_of": list(self._memberships),
            "subscribed_to": list(self._subscriptions),
        }
