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

import asyncio
import json
import math
import secrets
import shutil
import time
import urllib.parse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Set

import aiofiles
import msgpack
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from comet.cometnet.crypto import NodeIdentity
from comet.cometnet.utils import canonicalize_data, run_in_executor
from comet.core.logger import logger
from comet.core.models import settings
from comet.utils.atomic_file import write_text_atomic


def _validate_pool_id(value: object) -> str:
    if type(value) is not str or value != value.strip().lower():
        raise ValueError("pool_id must use its canonical lowercase form")
    if len(value) < 2 or len(value) > 64:
        raise ValueError("pool_id must be 2-64 characters")
    if not value.replace("-", "").replace("_", "").isalnum():
        raise ValueError("pool_id must be alphanumeric with - or _")
    return value


def _decode_pool_id_list(data: object, label: str) -> Set[str]:
    if type(data) is not list:
        raise ValueError(f"{label} root must be a list")
    pool_ids = [_validate_pool_id(value) for value in data]
    if len(pool_ids) != len(set(pool_ids)):
        raise ValueError(f"{label} pool IDs must be unique")
    return set(pool_ids)


def _encode_pool_id_set(values: object, label: str) -> List[str]:
    if type(values) is not set:
        raise ValueError(f"{label} must be a set")
    return sorted(_validate_pool_id(value) for value in values)


def _decode_pool_peers(data: object) -> Dict[str, Set[str]]:
    if type(data) is not dict:
        raise ValueError("pool peers root must be an object")

    result: Dict[str, Set[str]] = {}
    for raw_pool_id, raw_peers in data.items():
        pool_id = _validate_pool_id(raw_pool_id)
        if type(raw_peers) is not list or any(
            type(peer) is not str or not peer for peer in raw_peers
        ):
            raise ValueError("pool peer values must be lists of non-empty strings")
        if len(raw_peers) != len(set(raw_peers)):
            raise ValueError("pool peer addresses must be unique")
        result[pool_id] = set(raw_peers)
    return result


def _encode_pool_peers(data: object) -> Dict[str, List[str]]:
    if type(data) is not dict:
        raise ValueError("pool peers must be an object")

    result: Dict[str, List[str]] = {}
    for raw_pool_id, raw_peers in sorted(data.items()):
        pool_id = _validate_pool_id(raw_pool_id)
        if type(raw_peers) is not set or any(
            type(peer) is not str or not peer for peer in raw_peers
        ):
            raise ValueError("pool peer values must be sets of non-empty strings")
        result[pool_id] = sorted(raw_peers)
    return result


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

    model_config = ConfigDict(extra="forbid", validate_default=True)

    public_key: str
    role: MemberRole = MemberRole.MEMBER
    added_at: float = Field(default_factory=time.time)
    added_by: str  # Public key of admin who added this member
    alias: Optional[str] = None  # Friendly name

    # Stats (local tracking)
    contribution_count: int = 0
    last_seen: float = 0.0

    @field_validator("public_key", "added_by", mode="before")
    @classmethod
    def validate_required_keys(cls, value):
        if type(value) is not str or not value:
            raise ValueError("member public keys must be non-empty strings")
        return value

    @field_validator("alias", mode="before")
    @classmethod
    def validate_alias(cls, value):
        if value is not None and type(value) is not str:
            raise ValueError("member alias must be a string or null")
        return value

    @field_validator("added_at", "last_seen", mode="before")
    @classmethod
    def validate_timestamps(cls, value):
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError("member timestamps must be finite non-negative numbers")
        return value

    @field_validator("contribution_count", mode="before")
    @classmethod
    def validate_contribution_count(cls, value):
        if type(value) is not int or value < 0:
            raise ValueError("contribution_count must be a non-negative integer")
        return value

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

    model_config = ConfigDict(extra="forbid", validate_default=True)

    # Identity (immutable after creation)
    pool_id: str
    created_at: float = Field(default_factory=time.time)
    creator_key: str  # Public key of creator

    # Metadata (modifiable by admins)
    display_name: str
    description: str = ""

    # Members
    members: List[PoolMember]

    # Rules
    join_mode: JoinMode = JoinMode.INVITE

    # Versioning
    version: int = 1
    updated_at: float = Field(default_factory=time.time)

    # Signatures from admins (at least 1 required for validity)
    # Maps admin public key -> signature
    signatures: Dict[str, str] = Field(default_factory=dict)

    @field_validator("pool_id", mode="before")
    @classmethod
    def validate_pool_id(cls, value):
        """Validate pool ID format."""
        return _validate_pool_id(value)

    @field_validator("creator_key", "display_name", mode="before")
    @classmethod
    def validate_required_strings(cls, value):
        if type(value) is not str or not value:
            raise ValueError("manifest identity fields must be non-empty strings")
        return value

    @field_validator("description", mode="before")
    @classmethod
    def validate_description(cls, value):
        if type(value) is not str:
            raise ValueError("manifest description must be a string")
        return value

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def validate_timestamps(cls, value):
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError("manifest timestamps must be finite non-negative numbers")
        return value

    @field_validator("version", mode="before")
    @classmethod
    def validate_version(cls, value):
        if type(value) is not int or value < 1:
            raise ValueError("manifest version must be a positive integer")
        return value

    @field_validator("members", mode="before")
    @classmethod
    def validate_members_container(cls, value):
        if type(value) is not list:
            raise ValueError("manifest members must be a list")
        return value

    @field_validator("signatures", mode="before")
    @classmethod
    def validate_signatures(cls, value):
        if type(value) is not dict or any(
            type(key) is not str
            or not key
            or type(signature) is not str
            or not signature
            for key, signature in value.items()
        ):
            raise ValueError("manifest signatures must map non-empty strings")
        return value

    @model_validator(mode="after")
    def validate_member_structure(self):
        if not self.members:
            raise ValueError("manifest must contain at least one member")
        public_keys = [member.public_key for member in self.members]
        if len(public_keys) != len(set(public_keys)):
            raise ValueError("manifest member public keys must be unique")
        creators = [
            member for member in self.members if member.role is MemberRole.CREATOR
        ]
        if len(creators) != 1 or creators[0].public_key != self.creator_key:
            raise ValueError("manifest must contain exactly its declared creator")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self

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
        - Metadata: 'alias' is EXCLUDED (not part of consensus).
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

                if "alias" in m:
                    del m["alias"]

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
        return msgpack.packb(self.to_persisted_dict())

    def to_persisted_dict(self) -> Dict:
        """Serialize consensus and local fields without derived node IDs."""
        return self.model_dump(exclude={"members": {"__all__": {"node_id"}}})

    @classmethod
    def from_bytes(cls, data: bytes) -> "PoolManifest":
        """Deserialize from MsgPack bytes."""
        return cls.model_validate(msgpack.unpackb(data, raw=False))


class PoolInvite(BaseModel):
    """An invitation to join a pool."""

    model_config = ConfigDict(extra="forbid")

    pool_id: str
    invite_code: str = Field(default_factory=lambda: secrets.token_urlsafe(16))
    created_by: str  # Admin public key
    created_at: float = Field(default_factory=time.time)
    expires_at: Optional[float] = None  # None = never expires
    max_uses: Optional[int] = None  # None = unlimited
    uses: int = 0
    signature: str = ""  # Signature from creating admin
    node_url: str = ""  # URL of the node that created the invite

    @field_validator("pool_id")
    @classmethod
    def validate_pool_id(cls, value: str) -> str:
        return _validate_pool_id(value)

    @field_validator("invite_code", "created_by")
    @classmethod
    def validate_required_strings(cls, value: str) -> str:
        if not value:
            raise ValueError("invite identity fields must be non-empty")
        return value

    @field_validator("created_at", "expires_at", mode="before")
    @classmethod
    def validate_timestamps(cls, value):
        if value is None:
            return value
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError("invite timestamps must be finite non-negative numbers")
        return value

    @field_validator("max_uses", "uses", mode="before")
    @classmethod
    def validate_use_counts(cls, value, info):
        if value is None and info.field_name == "max_uses":
            return value
        minimum = 1 if info.field_name == "max_uses" else 0
        if type(value) is not int or value < minimum:
            raise ValueError(f"{info.field_name} must be an integer >= {minimum}")
        return value

    @model_validator(mode="after")
    def validate_usage(self):
        if self.max_uses is not None and self.uses > self.max_uses:
            raise ValueError("uses cannot exceed max_uses")
        return self

    def is_valid(self) -> bool:
        """Check if the invite is still valid."""
        if self.expires_at is not None and time.time() > self.expires_at:
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
        if type(link) is not str:
            return None
        try:
            parsed = urllib.parse.urlsplit(link)
            params = urllib.parse.parse_qs(
                parsed.query,
                keep_blank_values=True,
                strict_parsing=True,
            )
        except ValueError:
            return None
        if (
            parsed.scheme != "cometnet"
            or parsed.netloc != "join"
            or parsed.path
            or parsed.fragment
            or set(params) not in ({"pool", "code"}, {"pool", "code", "node"})
            or any(len(values) != 1 or not values[0] for values in params.values())
        ):
            return None

        pool_id = params["pool"][0]
        if (
            pool_id != pool_id.strip().lower()
            or not 2 <= len(pool_id) <= 64
            or not pool_id.replace("-", "").replace("_", "").isalnum()
        ):
            return None

        result = {"pool": pool_id, "code": params["code"][0]}
        if "node" in params:
            result["node"] = params["node"][0]
        return result


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
        self._dirty_manifests: Set[str] = set()  # Manifests that need saving
        self._mutation_lock = asyncio.Lock()
        self._auxiliary_lock = asyncio.Lock()

        # Ensure directories exist
        self.manifests_dir.mkdir(parents=True, exist_ok=True)
        self.invites_dir.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def serialized_manifest_mutation(self) -> AsyncIterator[None]:
        """Serialize a complete trusted-manifest read/validate/write transition."""
        async with self._mutation_lock:
            yield

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
        async with self._auxiliary_lock:
            await self._save_memberships()
            await self._save_subscriptions()
            await self._save_pool_peers()
        await self.flush_dirty_manifests()

    # ==================== Manifest Operations ====================

    def get_manifest(self, pool_id: str) -> Optional[PoolManifest]:
        """Get a pool manifest by ID."""
        manifest = self._manifests.get(pool_id)
        return manifest.model_copy(deep=True) if manifest else None

    def get_all_manifests(self) -> Dict[str, PoolManifest]:
        """Get all known pool manifests."""
        return {
            pool_id: manifest.model_copy(deep=True)
            for pool_id, manifest in self._manifests.items()
        }

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

        persisted_manifest = PoolManifest.model_validate(manifest.to_persisted_dict())

        manifest_path = self.manifests_dir / f"{manifest.pool_id}.json"
        await write_text_atomic(
            manifest_path,
            json.dumps(persisted_manifest.to_persisted_dict(), indent=2),
        )

        # Publish only a fully persisted snapshot. Keeping a detached copy also
        # prevents callers from mutating trusted state without another store.
        self._manifests[manifest.pool_id] = persisted_manifest
        return True

    async def accept_remote_manifest(
        self,
        manifest: PoolManifest,
    ) -> tuple[bool, Optional[PoolManifest]]:
        """Validate and serialize publication of a newer remote manifest."""
        async with self._mutation_lock:
            current = self.get_manifest(manifest.pool_id)
            if current and manifest.version <= current.version:
                return False, current
            if not await self.validate_manifest(manifest, current):
                return False, current
            await self.store_manifest(manifest)
            return True, current

    async def create_pool(
        self,
        pool_id: str,
        display_name: str,
        identity,  # NodeIdentity
        description: str = "",
        join_mode: JoinMode = JoinMode.INVITE,
    ) -> PoolManifest:
        """Serialize creation of a new local pool."""
        async with self._mutation_lock:
            return await self._create_pool(
                pool_id,
                display_name,
                identity,
                description,
                join_mode,
            )

    async def _create_pool(
        self,
        pool_id: str,
        display_name: str,
        identity,
        description: str,
        join_mode: JoinMode,
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
                    alias=settings.COMETNET_NODE_ALIAS,
                )
            ],
        )

        # Sign and store
        await self.store_manifest(manifest, identity)

        await self.add_membership(pool_id)

        logger.log("COMETNET", f"Created pool '{display_name}' ({pool_id})")
        return manifest

    async def delete_pool(self, pool_id: str) -> bool:
        """Serialize complete deletion of a local pool."""
        async with self._mutation_lock:
            return await self._delete_pool(pool_id)

    async def _delete_pool(self, pool_id: str) -> bool:
        """Delete a pool manifest (local only)."""
        if pool_id not in self._manifests:
            return False

        await self.remove_membership(pool_id)
        await self.unsubscribe(pool_id)
        await self.remove_pool_peer(pool_id)

        await self._delete_pool_invites(pool_id)

        manifest_path = self.manifests_dir / f"{pool_id}.json"
        if manifest_path.exists():
            await run_in_executor(manifest_path.unlink)
        del self._manifests[pool_id]

        return True

    async def _delete_pool_invites(self, pool_id: str) -> None:
        pool_inv_dir = self.invites_dir / pool_id
        if pool_inv_dir.exists():
            await run_in_executor(shutil.rmtree, pool_inv_dir)
        self._invites.pop(pool_id, None)

    # ==================== Membership Operations ====================

    def is_member_of(self, pool_id: str) -> bool:
        """Check if we are a member of a pool."""
        return pool_id in self._memberships

    def get_memberships(self) -> Set[str]:
        """Get all pools we are a member of."""
        return self._memberships.copy()

    async def add_membership(self, pool_id: str) -> None:
        """Persist and publish a pool membership."""
        async with self._auxiliary_lock:
            memberships = self._memberships | {pool_id}
            await self._replace_memberships(memberships)

    async def remove_membership(self, pool_id: str) -> None:
        """Persist and publish removal of a pool membership."""
        async with self._auxiliary_lock:
            memberships = self._memberships - {pool_id}
            await self._replace_memberships(memberships)

    async def add_member(
        self,
        pool_id: str,
        new_member_key: str,
        identity,  # NodeIdentity (must be admin)
        role: MemberRole = MemberRole.MEMBER,
        alias: Optional[str] = None,
    ) -> bool:
        """Serialize an administrator member addition."""
        async with self._mutation_lock:
            return await self._add_member(
                pool_id,
                new_member_key,
                identity,
                role,
                alias,
            )

    async def _add_member(
        self,
        pool_id: str,
        new_member_key: str,
        identity,
        role: MemberRole,
        alias: Optional[str],
    ) -> bool:
        """
        Add a new member to a pool (admin action).

        Args:
            pool_id: Pool to add member to
            new_member_key: Public key of new member
            identity: Admin's identity
            role: Role for the new member
            alias: Optional friendly name for the member

        Returns:
            True if member was added
        """
        manifest = self.get_manifest(pool_id)
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
                alias=alias,
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
        """Serialize an administrator member removal."""
        async with self._mutation_lock:
            return await self._remove_member(pool_id, member_key, identity)

    async def _remove_member(
        self,
        pool_id: str,
        member_key: str,
        identity,
    ) -> bool:
        """Remove a member from a pool (admin action)."""
        manifest = self.get_manifest(pool_id)
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

    async def set_member_role(
        self,
        pool_id: str,
        member_key: str,
        role: MemberRole,
        identity,
    ) -> bool:
        """Serialize an administrator role change."""
        if role not in (MemberRole.ADMIN, MemberRole.MEMBER):
            raise ValueError("Member role must be admin or member")

        async with self._mutation_lock:
            manifest = self.get_manifest(pool_id)
            if not manifest:
                raise ValueError(f"Pool {pool_id} not found")
            if not manifest.is_admin(identity.public_key_hex):
                raise PermissionError("Only admins can change member roles")

            member = manifest.get_member(member_key)
            if not member:
                raise ValueError("Member not found in pool")
            if member.role == MemberRole.CREATOR:
                raise ValueError("Cannot change the role of the pool creator")
            if member.role == role:
                return False

            member.role = role
            manifest.version += 1
            manifest.updated_at = time.time()
            await self.store_manifest(manifest, identity)
            return True

    async def leave_pool(
        self,
        pool_id: str,
        identity,  # NodeIdentity of the leaving member
    ) -> bool:
        """Serialize complete departure from a local pool."""
        async with self._mutation_lock:
            return await self._leave_pool(pool_id, identity)

    async def _leave_pool(
        self,
        pool_id: str,
        identity,
    ) -> bool:
        """
        Leave a pool (self-removal).

        Any member can leave a pool, except the creator who must delete the pool instead.
        """
        manifest = self.get_manifest(pool_id)
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

        await self.remove_membership(pool_id)
        await self.unsubscribe(pool_id)
        await self.remove_pool_peer(pool_id)

        await self._delete_pool_invites(pool_id)

        manifest_path = self.manifests_dir / f"{pool_id}.json"
        if manifest_path.exists():
            await run_in_executor(manifest_path.unlink)
        del self._manifests[pool_id]

        logger.log("COMETNET", f"Left pool {pool_id}")
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
        async with self._auxiliary_lock:
            subscriptions = self._subscriptions | {pool_id}
            await self._replace_subscriptions(subscriptions)
        logger.log("COMETNET", f"Subscribed to pool {pool_id}")

    async def unsubscribe(self, pool_id: str) -> None:
        """Unsubscribe from a pool."""
        async with self._auxiliary_lock:
            subscriptions = self._subscriptions - {pool_id}
            await self._replace_subscriptions(subscriptions)
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
        manifest = self.get_manifest(pool_id)
        if not manifest:
            raise ValueError(f"Pool {pool_id} not found")

        if not manifest.is_admin(identity.public_key_hex):
            raise PermissionError("Only admins can create invites")

        if expires_in is not None and (type(expires_in) is not int or expires_in <= 0):
            raise ValueError("expires_in must be a positive integer or None")
        if max_uses is not None and (type(max_uses) is not int or max_uses <= 0):
            raise ValueError("max_uses must be a positive integer or None")

        invite = PoolInvite(
            pool_id=pool_id,
            created_by=identity.public_key_hex,
            expires_at=time.time() + expires_in if expires_in is not None else None,
            max_uses=max_uses,
            node_url=node_url or "",
        )

        # Sign the invite
        invite.signature = await identity.sign_hex_async(invite.to_signable_bytes())

        # Persist before publishing it to callers.
        await self._save_invite(invite)

        logger.log("COMETNET", f"Created invite for pool {pool_id}: {invite.to_link()}")
        return invite

    def get_invites(self, pool_id: str) -> List[PoolInvite]:
        """Get all invites for a pool."""
        return [
            invite.model_copy(deep=True)
            for invite in self._invites.get(pool_id, {}).values()
        ]

    async def delete_invite(self, pool_id: str, invite_code: str) -> bool:
        """Delete an invite."""
        if pool_id in self._invites and invite_code in self._invites[pool_id]:
            # Delete from disk first
            pool_inv_dir = self.invites_dir / pool_id
            invite_file = pool_inv_dir / f"{invite_code}.json"
            if invite_file.exists():
                await run_in_executor(invite_file.unlink)

            # Delete from memory
            del self._invites[pool_id][invite_code]
            logger.log("COMETNET", f"Deleted invite {invite_code} from pool {pool_id}")
            return True
        return False

    def get_invite(self, pool_id: str, invite_code: str) -> Optional[PoolInvite]:
        """Get an invite by pool ID and code."""
        invite = self._invites.get(pool_id, {}).get(invite_code)
        return invite.model_copy(deep=True) if invite else None

    async def use_invite(
        self,
        pool_id: str,
        invite_code: str,
        identity,  # NodeIdentity of the joining node
        alias: Optional[str] = None,
    ) -> bool:
        """
        Use an invitation to join a pool.

        Args:
            pool_id: Pool to join
            invite_code: The invitation code
            identity: Identity of the node joining
            alias: Optional alias for the joining node

        Returns:
            True if successfully joined
        """
        accepted = await self.accept_invite_member(
            pool_id,
            invite_code,
            identity.public_key_hex,
            alias=alias,
        )
        if accepted is None:
            return False

        await self.add_membership(pool_id)

        logger.log("COMETNET", f"Joined pool {pool_id} via invite")
        return True

    async def accept_invite_member(
        self,
        pool_id: str,
        invite_code: str,
        member_key: str,
        *,
        alias: Optional[str] = None,
        signing_identity=None,
    ) -> Optional[tuple[PoolManifest, PoolInvite, bool]]:
        """Atomically validate/consume an invite and publish its member manifest."""
        async with self._mutation_lock:
            invite = self.get_invite(pool_id, invite_code)
            if not invite or not invite.is_valid():
                return None

            manifest = self.get_manifest(pool_id)
            if not manifest:
                return None
            if manifest.is_member(member_key):
                return manifest, invite, False

            manifest.members.append(
                PoolMember(
                    public_key=member_key,
                    role=MemberRole.MEMBER,
                    added_by=invite.created_by,
                    alias=alias,
                )
            )
            manifest.version += 1
            manifest.updated_at = time.time()

            invite.uses += 1
            await self._save_invite(invite)
            await self.store_manifest(manifest, signing_identity)
            return manifest, invite, True

    # ==================== Persistence ====================

    async def _load_manifests(self) -> None:
        """Load all manifests from disk."""
        self._manifests.clear()

        if not self.manifests_dir.exists():
            return

        for manifest_file in self.manifests_dir.glob("*.json"):
            try:
                async with aiofiles.open(manifest_file, "r") as f:
                    content = await f.read()
                    data = json.loads(content)
                manifest = PoolManifest.model_validate(data)
                if manifest.pool_id != manifest_file.stem:
                    raise ValueError("manifest pool_id must match its filename")
                self._manifests[manifest.pool_id] = manifest
            except Exception as e:
                logger.warning(f"Failed to load manifest {manifest_file}: {e}")

    async def _load_memberships(self) -> None:
        """Load memberships from disk."""
        self._memberships.clear()
        memberships_file = self.pools_dir / "memberships.json"

        if memberships_file.exists():
            try:
                async with aiofiles.open(memberships_file, "r") as f:
                    content = await f.read()
                data = json.loads(content)
                self._memberships = _decode_pool_id_list(data, "memberships")
            except Exception as e:
                logger.warning(f"Failed to load memberships: {e}")

    async def _save_memberships(self) -> None:
        """Save memberships to disk."""
        await self._write_memberships(self._memberships)

    async def _replace_memberships(self, memberships: Set[str]) -> None:
        await self._write_memberships(memberships)
        self._memberships = memberships

    async def _write_memberships(self, memberships: Set[str]) -> None:
        memberships_file = self.pools_dir / "memberships.json"
        payload = _encode_pool_id_set(memberships, "memberships")
        await write_text_atomic(memberships_file, json.dumps(payload))

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
                async with aiofiles.open(subscriptions_file, "r") as f:
                    content = await f.read()
                data = json.loads(content)
                self._subscriptions.update(_decode_pool_id_list(data, "subscriptions"))
            except Exception as e:
                logger.warning(f"Failed to load subscriptions: {e}")

    async def _save_subscriptions(self) -> None:
        """Save subscriptions to disk."""
        await self._write_subscriptions(self._subscriptions)

    async def _replace_subscriptions(self, subscriptions: Set[str]) -> None:
        await self._write_subscriptions(subscriptions)
        self._subscriptions = subscriptions

    async def _write_subscriptions(self, subscriptions: Set[str]) -> None:
        subscriptions_file = self.pools_dir / "subscriptions.json"
        payload = _encode_pool_id_set(subscriptions, "subscriptions")
        await write_text_atomic(subscriptions_file, json.dumps(payload))

    async def _load_invites(self) -> None:
        """Load invites from disk."""
        self._invites.clear()

        if not self.invites_dir.exists():
            return

        for pool_dir in self.invites_dir.iterdir():
            if not pool_dir.is_dir():
                continue
            try:
                pool_id = _validate_pool_id(pool_dir.name)
            except ValueError as error:
                logger.warning(f"Failed to load invite directory {pool_dir}: {error}")
                continue

            self._invites[pool_id] = {}
            for invite_file in pool_dir.glob("*.json"):
                try:
                    async with aiofiles.open(invite_file, "r") as f:
                        content = await f.read()
                        data = json.loads(content)
                    invite = PoolInvite.model_validate(data)
                    if invite.pool_id != pool_id:
                        raise ValueError("invite pool_id must match its directory")
                    if invite.invite_code != invite_file.stem:
                        raise ValueError("invite code must match its filename")
                    if not invite.signature:
                        raise ValueError("persisted invite signature must be non-empty")
                    if invite.is_valid():
                        self._invites[pool_id][invite.invite_code] = invite
                    else:
                        # Clean up expired or exhausted invites.
                        await run_in_executor(invite_file.unlink)
                except Exception as e:
                    logger.warning(f"Failed to load invite {invite_file}: {e}")

    async def _save_invite(self, invite: PoolInvite) -> None:
        """Save an invite to disk."""
        pool_inv_dir = self.invites_dir / invite.pool_id
        pool_inv_dir.mkdir(parents=True, exist_ok=True)

        invite_file = pool_inv_dir / f"{invite.invite_code}.json"
        await write_text_atomic(invite_file, json.dumps(invite.model_dump(), indent=2))
        self._invites.setdefault(invite.pool_id, {})[invite.invite_code] = (
            invite.model_copy(deep=True)
        )

    async def _load_pool_peers(self) -> None:
        """Load known pool peers from disk."""
        self._pool_peers.clear()
        peers_file = self.pools_dir / "pool_peers.json"

        if peers_file.exists():
            try:
                async with aiofiles.open(peers_file, "r") as f:
                    content = await f.read()
                data = json.loads(content)
                self._pool_peers = _decode_pool_peers(data)
            except Exception as e:
                logger.warning(f"Failed to load pool peers: {e}")

    async def _save_pool_peers(self) -> None:
        """Save known pool peers to disk."""
        await self._write_pool_peers(self._pool_peers)

    async def _replace_pool_peers(self, pool_peers: Dict[str, Set[str]]) -> None:
        await self._write_pool_peers(pool_peers)
        self._pool_peers = pool_peers

    async def _write_pool_peers(self, pool_peers: Dict[str, Set[str]]) -> None:
        peers_file = self.pools_dir / "pool_peers.json"
        data = _encode_pool_peers(pool_peers)
        await write_text_atomic(peers_file, json.dumps(data, indent=2))

    async def add_pool_peer(self, pool_id: str, peer_address: str) -> None:
        """
        Add a known peer address for a pool.

        This is called when we receive a manifest from a peer,
        so we can reconnect to them later.
        """
        if not peer_address:
            return
        async with self._auxiliary_lock:
            pool_peers = {
                existing_pool_id: set(peers)
                for existing_pool_id, peers in self._pool_peers.items()
            }
            pool_peers.setdefault(pool_id, set()).add(peer_address)
            await self._replace_pool_peers(pool_peers)

    async def remove_pool_peer(self, pool_id: str) -> None:
        """Persist and publish removal of all known peers for a pool."""
        async with self._auxiliary_lock:
            pool_peers = {
                existing_pool_id: set(peers)
                for existing_pool_id, peers in self._pool_peers.items()
                if existing_pool_id != pool_id
            }
            await self._replace_pool_peers(pool_peers)

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
        if type(contributor_public_key) is not str or not contributor_public_key:
            raise ValueError("contributor_public_key must be a non-empty string")
        if pool_id is not None and (type(pool_id) is not str or not pool_id):
            raise ValueError("pool_id must be a non-empty string or None")
        if type(count) is not int or count <= 0:
            raise ValueError("count must be a positive integer")

        async with self._mutation_lock:
            contribution_time = time.time()

            # If pool_id is specified, only update that specific pool
            if pool_id:
                manifest = self._manifests.get(pool_id)
                if manifest:
                    member = manifest.get_member(contributor_public_key)
                    if member:
                        member.contribution_count += count
                        member.last_seen = contribution_time
                        self._dirty_manifests.add(manifest.pool_id)
                        return True
                return False

            # No pool_id specified: update all pools containing this member.
            recorded = False
            for manifest in self._manifests.values():
                member = manifest.get_member(contributor_public_key)
                if member:
                    member.contribution_count += count
                    member.last_seen = contribution_time
                    self._dirty_manifests.add(manifest.pool_id)
                    recorded = True

            return recorded

    async def flush_dirty_manifests(self) -> None:
        """Save all modified manifests to disk."""
        async with self._mutation_lock:
            count = 0
            for pool_id in sorted(tuple(self._dirty_manifests)):
                manifest = self._manifests.get(pool_id)
                if manifest is None:
                    self._dirty_manifests.discard(pool_id)
                    continue

                # Keep the ID dirty until persistence succeeds. Any write error
                # remains visible to periodic/shutdown callers and can be retried.
                await self.store_manifest(manifest)
                self._dirty_manifests.discard(pool_id)
                count += 1

            if count > 0:
                logger.debug(f"Flushed {count} dirty pool manifests")

    # ==================== Validation ====================

    async def validate_manifest(
        self,
        manifest: PoolManifest,
        trusted_manifest: Optional[PoolManifest] = None,
    ) -> bool:
        """
        Validate a pool manifest.

        Checks:
        1. At least one valid admin signature
        2. Creator is an admin
        3. Basic structure validity

        Returns:
            True if the manifest is valid
        """
        signature_authority = trusted_manifest or manifest
        if trusted_manifest and (
            manifest.creator_key != trusted_manifest.creator_key
            or manifest.created_at != trusted_manifest.created_at
        ):
            return False

        try:
            signable_data = manifest.to_signable_bytes()

            for admin_key, signature in manifest.signatures.items():
                if signature_authority.is_admin(admin_key):
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
