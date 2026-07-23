"""
CometNet Protocol Module

Defines all message types and serialization logic for CometNet P2P communication.
Uses MsgPack for efficient binary serialization.
"""

import math
import time
from enum import Enum
from typing import Dict, List, Literal, Optional, Union

import msgpack
from pydantic import BaseModel, ConfigDict, Field, field_validator

from comet.cometnet.utils import canonicalize_data
from comet.utils.formatting import normalize_info_hash

# Exact current signed protocol version
PROTOCOL_VERSION = "1.0"


def _validate_current_pool_id(value: object) -> str:
    if type(value) is not str or value != value.strip().lower():
        raise ValueError("pool_id must use its canonical lowercase form")
    if not 2 <= len(value) <= 64:
        raise ValueError("pool_id must be 2-64 characters")
    if not value.replace("-", "").replace("_", "").isalnum():
        raise ValueError("pool_id must be alphanumeric with - or _")
    return value


def _validate_non_empty_string(value: object, field_name: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


class MessageType(str, Enum):
    """Types of messages in the CometNet protocol."""

    # Core messages
    HANDSHAKE = "handshake"
    PING = "ping"
    PONG = "pong"
    PEER_REQUEST = "peer_request"
    PEER_RESPONSE = "peer_response"
    TORRENT_ANNOUNCE = "torrent_announce"
    TORRENT_QUERY = "torrent_query"
    TORRENT_RESPONSE = "torrent_response"
    SYNC_REQUEST = "sync_request"
    SYNC_RESPONSE = "sync_response"

    # Pool management
    POOL_MANIFEST = "pool_manifest"
    POOL_JOIN_REQUEST = "pool_join"
    POOL_MEMBER_UPDATE = "pool_member_update"
    POOL_DELETE = "pool_delete"


class BaseMessage(BaseModel):
    """Base class for all CometNet messages."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(default=PROTOCOL_VERSION)
    type: MessageType
    timestamp: float = Field(default_factory=time.time)
    sender_id: str = ""  # Node ID of the sender
    signature: str = ""  # Hex-encoded signature

    @field_validator("version", mode="before")
    @classmethod
    def validate_protocol_version(cls, value):
        if type(value) is not str or value != PROTOCOL_VERSION:
            raise ValueError(f"version must be {PROTOCOL_VERSION!r}")
        return value

    @field_validator("timestamp", mode="before")
    @classmethod
    def reject_boolean_timestamp(cls, value):
        if type(value) not in (int, float):
            raise ValueError("timestamp must be a finite number")
        return value

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("timestamp must be a finite number")
        return value

    def to_signable_bytes(self) -> bytes:
        """
        Returns the bytes that should be signed.
        Excludes the signature field itself.
        Uses MsgPack with sorted keys for stable canonicalization.
        """
        data = self.model_dump(exclude={"signature"})
        return msgpack.packb(canonicalize_data(data))

    def to_bytes(self) -> bytes:
        """Serialize the message to MsgPack bytes."""
        return msgpack.packb(self.model_dump())

    @classmethod
    def from_bytes(cls, data: bytes) -> "BaseMessage":
        """Deserialize a message from MsgPack bytes."""
        return cls.model_validate(msgpack.unpackb(data, raw=False))


class HandshakeMessage(BaseMessage):
    """
    Initial handshake message sent when connecting to a peer.

    Contains the sender's public key for identity verification
    and future encrypted communications.
    """

    type: Literal[MessageType.HANDSHAKE] = MessageType.HANDSHAKE
    public_key: str = ""  # Hex-encoded public key
    listen_port: int = 0  # Port this node is listening on (for reverse connections)
    public_url: Optional[str] = None  # Full public URL (for reverse proxies)
    alias: Optional[str] = None  # Friendly name for the node
    capabilities: List[str] = Field(default_factory=list)  # Future extension
    network_token: Optional[str] = None  # HMAC token for private network auth

    @field_validator("listen_port", mode="before")
    @classmethod
    def validate_listen_port(cls, value):
        if type(value) is not int or not 0 <= value <= 65535:
            raise ValueError("listen_port must be an integer between 0 and 65535")
        return value


class PingMessage(BaseMessage):
    """Ping message to check if a peer is still alive."""

    type: Literal[MessageType.PING] = MessageType.PING
    nonce: str = ""  # Random nonce for matching pong


class PongMessage(BaseMessage):
    """Pong response to a ping message."""

    type: Literal[MessageType.PONG] = MessageType.PONG
    nonce: str = ""  # Echo of the ping nonce


class PeerInfo(BaseModel):
    """Information about a peer for exchange."""

    model_config = ConfigDict(extra="forbid")

    node_id: str
    address: str  # WebSocket URL (e.g., wss://host:port)
    last_seen: float = 0.0
    reputation: float = 50.0

    @field_validator("last_seen", "reputation", mode="before")
    @classmethod
    def validate_numeric_fields(cls, value, info):
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError(f"{info.field_name} must be a finite number")
        if info.field_name == "last_seen" and value < 0:
            raise ValueError("last_seen must be non-negative")
        if info.field_name == "reputation" and not 0 <= value <= 100:
            raise ValueError("reputation must be between 0 and 100")
        return value


class PeerRequest(BaseMessage):
    """Request for a list of known peers."""

    type: Literal[MessageType.PEER_REQUEST] = MessageType.PEER_REQUEST
    max_peers: int = 20  # Maximum number of peers to return

    @field_validator("max_peers", mode="before")
    @classmethod
    def validate_max_peers(cls, value):
        if type(value) is not int or not 1 <= value <= 1000:
            raise ValueError("max_peers must be an integer between 1 and 1000")
        return value


class PeerResponse(BaseMessage):
    """Response containing a list of known peers."""

    type: Literal[MessageType.PEER_RESPONSE] = MessageType.PEER_RESPONSE
    peers: List[PeerInfo] = Field(default_factory=list)


class TorrentMetadata(BaseModel):
    """
    Metadata for a torrent shared across the network.

    This is the core data structure that CometNet propagates.
    """

    info_hash: str  # 40-character hex string
    title: str
    size: int  # Size in bytes
    seeders: Optional[int] = None
    tracker: str  # Source/tracker name
    imdb_id: str
    file_index: Optional[int] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    sources: List[str] = Field(default_factory=list)
    parsed: Optional[dict] = None  # Serialized RTN ParsedData
    updated_at: float = Field(default_factory=time.time)
    contributor_id: str = ""  # Node ID of the original contributor
    contributor_public_key: str = (
        ""  # Public key of the original contributor (for validation)
    )
    contributor_signature: str = ""  # Signature from the contributor

    # Pool association
    pool_id: Optional[str] = None  # Pool this torrent belongs to (if any)

    @field_validator("info_hash")
    @classmethod
    def validate_info_hash(cls, v: str) -> str:
        """Validate that info_hash is a valid 40-character hex string."""
        v = normalize_info_hash(v)

        if len(v) != 40:
            raise ValueError("info_hash must be 40 characters")
        try:
            int(v, 16)
        except ValueError:
            raise ValueError("info_hash must be valid hexadecimal")
        return v

    @field_validator("size")
    @classmethod
    def validate_size(cls, v: int) -> int:
        """Validate that size is a reasonable value."""
        if v <= 0:
            raise ValueError("size must be positive")
        if v > 1024 * 1024 * 1024 * 1024 * 10:  # 10 TB max
            raise ValueError("size exceeds maximum allowed value")
        return v

    @field_validator(
        "size", "seeders", "file_index", "season", "episode", mode="before"
    )
    @classmethod
    def reject_boolean_integer_fields(cls, value):
        if value is not None and type(value) is not int:
            raise ValueError("torrent integer fields must be integers")
        return value

    @field_validator("seeders", "file_index", "season", "episode")
    @classmethod
    def validate_optional_non_negative_integer(
        cls, value: Optional[int]
    ) -> Optional[int]:
        if value is not None and value < 0:
            raise ValueError("torrent integer fields must be non-negative")
        return value

    @field_validator("updated_at", mode="before")
    @classmethod
    def reject_boolean_updated_at(cls, value):
        if type(value) not in (int, float):
            raise ValueError("updated_at must be a finite number")
        return value

    @field_validator("updated_at")
    @classmethod
    def validate_updated_at(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("updated_at must be a finite number")
        return value

    @field_validator("imdb_id")
    @classmethod
    def validate_imdb_id(cls, v: str) -> str:
        """Require a non-empty media identifier for network torrent metadata."""
        if not v:
            raise ValueError("imdb_id is required")
        return v

    def to_signable_bytes(self) -> bytes:
        """Returns bytes for signing (excludes contributor_signature)."""
        data = self.model_dump(exclude={"contributor_signature"})
        return msgpack.packb(canonicalize_data(data))


class TorrentAnnounce(BaseMessage):
    """
    Announce one or more torrents to the network.

    This is the primary gossip message for propagating torrent metadata.
    """

    type: Literal[MessageType.TORRENT_ANNOUNCE] = MessageType.TORRENT_ANNOUNCE
    torrents: List[TorrentMetadata] = Field(default_factory=list)
    ttl: int = 5  # Time-to-live (hops remaining)

    @field_validator("torrents")
    @classmethod
    def validate_torrents(cls, v: List[TorrentMetadata]) -> List[TorrentMetadata]:
        """Validate that we don't exceed max torrents per message."""
        if len(v) > 1000:
            raise ValueError("Maximum 1000 torrents per announce message")
        return v

    @field_validator("ttl", mode="before")
    @classmethod
    def validate_ttl(cls, value):
        if type(value) is not int or not 1 <= value <= 32:
            raise ValueError("ttl must be an integer between 1 and 32")
        return value

    visited_nodes: List[str] = Field(
        default_factory=list
    )  # List of nodes that have seen this message


class TorrentQuery(BaseMessage):
    """Query for specific torrents (by info_hash or media ID)."""

    type: Literal[MessageType.TORRENT_QUERY] = MessageType.TORRENT_QUERY
    info_hashes: List[str] = Field(default_factory=list)
    imdb_id: Optional[str] = None
    limit: int = 50

    @field_validator("limit", mode="before")
    @classmethod
    def validate_limit(cls, value):
        if type(value) is not int or not 1 <= value <= 1000:
            raise ValueError("limit must be an integer between 1 and 1000")
        return value


class TorrentResponse(BaseMessage):
    """Response to a torrent query."""

    type: Literal[MessageType.TORRENT_RESPONSE] = MessageType.TORRENT_RESPONSE
    torrents: List[TorrentMetadata] = Field(default_factory=list)
    query_id: str = ""  # Reference to the original query


# ==================== Pool Messages ====================


class PoolMemberPayload(BaseModel):
    """Exact member representation carried by a pool manifest message."""

    model_config = ConfigDict(extra="forbid")

    public_key: str
    role: Literal["creator", "admin", "member"] = "member"
    added_at: float
    added_by: str
    alias: Optional[str] = None
    contribution_count: int = 0
    last_seen: float = 0.0

    @field_validator("public_key", "added_by", mode="before")
    @classmethod
    def validate_keys(cls, value, info):
        return _validate_non_empty_string(value, info.field_name)

    @field_validator("added_at", "last_seen", mode="before")
    @classmethod
    def validate_timestamps(cls, value, info):
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError(f"{info.field_name} must be a finite non-negative number")
        return value

    @field_validator("contribution_count", mode="before")
    @classmethod
    def validate_contribution_count(cls, value):
        if type(value) is not int or value < 0:
            raise ValueError("contribution_count must be a non-negative integer")
        return value


class PoolManifestMessage(BaseMessage):
    """
    Broadcast or update a pool manifest.

    Used to propagate pool definitions across the network.
    """

    type: Literal[MessageType.POOL_MANIFEST] = MessageType.POOL_MANIFEST
    pool_id: str
    display_name: str
    description: str = ""
    creator_key: str
    members: List[PoolMemberPayload] = Field(default_factory=list)
    join_mode: Literal["invite"] = "invite"
    manifest_version: int = 1
    created_at: float = 0.0  # Creation timestamp
    updated_at: float = 0.0  # Last update timestamp
    manifest_signatures: dict = Field(default_factory=dict)  # admin_key -> sig

    @field_validator("pool_id", mode="before")
    @classmethod
    def validate_pool_id(cls, value):
        return _validate_current_pool_id(value)

    @field_validator("display_name", "creator_key", mode="before")
    @classmethod
    def validate_required_strings(cls, value, info):
        return _validate_non_empty_string(value, info.field_name)

    @field_validator("manifest_signatures", mode="before")
    @classmethod
    def validate_manifest_signatures(cls, value):
        if type(value) is not dict or any(
            type(key) is not str
            or not key
            or type(signature) is not str
            or not signature
            for key, signature in value.items()
        ):
            raise ValueError("manifest_signatures must map non-empty strings")
        return value

    @field_validator("manifest_version", mode="before")
    @classmethod
    def validate_manifest_version(cls, value):
        if type(value) is not int or value < 1:
            raise ValueError("manifest_version must be a positive integer")
        return value

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def validate_manifest_timestamp(cls, value, info):
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError(f"{info.field_name} must be a finite non-negative number")
        return value


class PoolJoinRequest(BaseMessage):
    """Request to join a pool."""

    type: Literal[MessageType.POOL_JOIN_REQUEST] = MessageType.POOL_JOIN_REQUEST
    pool_id: str
    invite_code: Optional[str] = None  # For invite-based join

    requester_key: str
    alias: Optional[str] = None  # Friendly name of the requester

    @field_validator("pool_id", mode="before")
    @classmethod
    def validate_pool_id(cls, value):
        return _validate_current_pool_id(value)

    @field_validator("invite_code", "requester_key", mode="before")
    @classmethod
    def validate_join_fields(cls, value, info):
        if value is None and info.field_name == "invite_code":
            return value
        return _validate_non_empty_string(value, info.field_name)


class PoolMemberUpdate(BaseMessage):
    """Notify network of membership changes."""

    type: Literal[MessageType.POOL_MEMBER_UPDATE] = MessageType.POOL_MEMBER_UPDATE
    pool_id: str
    action: Literal["add", "remove", "promote", "demote", "leave"]
    member_key: str
    new_role: Optional[Literal["admin", "member"]] = None
    updated_by: str  # Admin who made the change
    manifest_signatures: Dict[str, str] = Field(
        default_factory=dict
    )  # Signatures of the NEW manifest state

    @field_validator("pool_id", mode="before")
    @classmethod
    def validate_pool_id(cls, value):
        return _validate_current_pool_id(value)

    @field_validator("member_key", "updated_by", mode="before")
    @classmethod
    def validate_member_keys(cls, value, info):
        return _validate_non_empty_string(value, info.field_name)

    @field_validator("manifest_signatures", mode="before")
    @classmethod
    def validate_manifest_signatures(cls, value):
        if type(value) is not dict or any(
            type(key) is not str
            or not key
            or type(signature) is not str
            or not signature
            for key, signature in value.items()
        ):
            raise ValueError("manifest_signatures must map non-empty strings")
        return value


class PoolDeleteMessage(BaseMessage):
    """Notify network that a pool has been deleted by its creator."""

    type: Literal[MessageType.POOL_DELETE] = MessageType.POOL_DELETE
    pool_id: str
    deleted_by: str  # Public key of the creator who deleted it

    @field_validator("pool_id", mode="before")
    @classmethod
    def validate_pool_id(cls, value):
        return _validate_current_pool_id(value)

    @field_validator("deleted_by", mode="before")
    @classmethod
    def validate_deleted_by(cls, value):
        return _validate_non_empty_string(value, "deleted_by")


# Union type for all possible message types
AnyMessage = Union[
    HandshakeMessage,
    PingMessage,
    PongMessage,
    PeerRequest,
    PeerResponse,
    TorrentAnnounce,
    TorrentQuery,
    TorrentResponse,
    PoolManifestMessage,
    PoolJoinRequest,
    PoolMemberUpdate,
    PoolDeleteMessage,
]


def parse_message(data: Union[str, bytes]) -> Optional[AnyMessage]:
    """
    Parse MsgPack bytes into the appropriate message type.
    """
    try:
        # Strict MsgPack parsing
        if isinstance(data, str):
            # Should not happen in pure MsgPack env, but handle graceful fail
            return None

        payload = msgpack.unpackb(data, raw=False)
        if not isinstance(payload, dict):
            return None
        msg_type = payload.get("type")

        # Core messages
        if msg_type == MessageType.HANDSHAKE:
            return HandshakeMessage.model_validate(payload)
        elif msg_type == MessageType.PING:
            return PingMessage.model_validate(payload)
        elif msg_type == MessageType.PONG:
            return PongMessage.model_validate(payload)
        elif msg_type == MessageType.PEER_REQUEST:
            return PeerRequest.model_validate(payload)
        elif msg_type == MessageType.PEER_RESPONSE:
            return PeerResponse.model_validate(payload)
        elif msg_type == MessageType.TORRENT_ANNOUNCE:
            return TorrentAnnounce.model_validate(payload)
        elif msg_type == MessageType.TORRENT_QUERY:
            return TorrentQuery.model_validate(payload)
        elif msg_type == MessageType.TORRENT_RESPONSE:
            return TorrentResponse.model_validate(payload)
        # Pool messages
        elif msg_type == MessageType.POOL_MANIFEST:
            return PoolManifestMessage.model_validate(payload)
        elif msg_type == MessageType.POOL_JOIN_REQUEST:
            return PoolJoinRequest.model_validate(payload)
        elif msg_type == MessageType.POOL_MEMBER_UPDATE:
            return PoolMemberUpdate.model_validate(payload)
        elif msg_type == MessageType.POOL_DELETE:
            return PoolDeleteMessage.model_validate(payload)
        else:
            return None
    except (msgpack.exceptions.UnpackException, TypeError, ValueError):
        return None
