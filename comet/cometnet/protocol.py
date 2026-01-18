"""
CometNet Protocol Module

Defines all message types and serialization logic for CometNet P2P communication.
Uses MsgPack for efficient binary serialization.
"""

import time
from enum import Enum
from typing import Any, List, Optional, Union

import msgpack
from pydantic import BaseModel, Field, field_validator

from comet.utils.formatting import normalize_info_hash

# Protocol version for backwards compatibility
PROTOCOL_VERSION = "1.0"


def canonicalize_for_signing(data: Any) -> Any:
    """Recursively sort dict keys for deterministic serialization."""
    if isinstance(data, dict):
        return {k: canonicalize_for_signing(v) for k, v in sorted(data.items())}
    elif isinstance(data, list):
        return [canonicalize_for_signing(i) for i in data]
    return data


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

    version: str = Field(default=PROTOCOL_VERSION)
    type: MessageType
    timestamp: float = Field(default_factory=time.time)
    sender_id: str = ""  # Node ID of the sender
    signature: str = ""  # Hex-encoded signature

    def to_signable_bytes(self) -> bytes:
        """
        Returns the bytes that should be signed.
        Excludes the signature field itself.
        Uses MsgPack with sorted keys for stable canonicalization.
        """
        data = self.model_dump(exclude={"signature"})
        return msgpack.packb(canonicalize_for_signing(data))

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

    type: MessageType = MessageType.HANDSHAKE
    public_key: str = ""  # Hex-encoded public key
    listen_port: int = 0  # Port this node is listening on (for reverse connections)
    public_url: Optional[str] = None  # Full public URL (for reverse proxies)
    capabilities: List[str] = Field(default_factory=list)  # Future extension


class PingMessage(BaseMessage):
    """Ping message to check if a peer is still alive."""

    type: MessageType = MessageType.PING
    nonce: str = ""  # Random nonce for matching pong


class PongMessage(BaseMessage):
    """Pong response to a ping message."""

    type: MessageType = MessageType.PONG
    nonce: str = ""  # Echo of the ping nonce


class PeerInfo(BaseModel):
    """Information about a peer for exchange."""

    node_id: str
    address: str  # WebSocket URL (e.g., wss://host:port)
    last_seen: float = 0.0
    reputation: float = 50.0


class PeerRequest(BaseMessage):
    """Request for a list of known peers."""

    type: MessageType = MessageType.PEER_REQUEST
    max_peers: int = 20  # Maximum number of peers to return


class PeerResponse(BaseMessage):
    """Response containing a list of known peers."""

    type: MessageType = MessageType.PEER_RESPONSE
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
    imdb_id: Optional[str] = None
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
        if v < 0:
            raise ValueError("size must be non-negative")
        if v > 1024 * 1024 * 1024 * 1024 * 10:  # 10 TB max
            raise ValueError("size exceeds maximum allowed value")
        return v

    def to_signable_bytes(self) -> bytes:
        """Returns bytes for signing (excludes contributor_signature)."""
        data = self.model_dump(exclude={"contributor_signature"})
        return msgpack.packb(canonicalize_for_signing(data))


class TorrentAnnounce(BaseMessage):
    """
    Announce one or more torrents to the network.

    This is the primary gossip message for propagating torrent metadata.
    """

    type: MessageType = MessageType.TORRENT_ANNOUNCE
    torrents: List[TorrentMetadata] = Field(default_factory=list)
    ttl: int = 5  # Time-to-live (hops remaining)

    @field_validator("torrents")
    @classmethod
    def validate_torrents(cls, v: List[TorrentMetadata]) -> List[TorrentMetadata]:
        """Validate that we don't exceed max torrents per message."""
        if len(v) > 1000:
            raise ValueError("Maximum 1000 torrents per announce message")
        return v


class TorrentQuery(BaseMessage):
    """Query for specific torrents (by info_hash or media ID)."""

    type: MessageType = MessageType.TORRENT_QUERY
    info_hashes: List[str] = Field(default_factory=list)
    imdb_id: Optional[str] = None
    limit: int = 50


class TorrentResponse(BaseMessage):
    """Response to a torrent query."""

    type: MessageType = MessageType.TORRENT_RESPONSE
    torrents: List[TorrentMetadata] = Field(default_factory=list)
    query_id: str = ""  # Reference to the original query


# ==================== Pool Messages ====================


class PoolManifestMessage(BaseMessage):
    """
    Broadcast or update a pool manifest.

    Used to propagate pool definitions across the network.
    """

    type: MessageType = MessageType.POOL_MANIFEST
    pool_id: str
    display_name: str
    description: str = ""
    creator_key: str
    members: List[dict] = Field(default_factory=list)  # Serialized PoolMembers
    join_mode: str = "invite"
    version: int = 1
    created_at: float = 0.0  # Creation timestamp
    updated_at: float = 0.0  # Last update timestamp
    manifest_signatures: dict = Field(default_factory=dict)  # admin_key -> sig


class PoolJoinRequest(BaseMessage):
    """Request to join a pool."""

    type: MessageType = MessageType.POOL_JOIN_REQUEST
    pool_id: str
    invite_code: Optional[str] = None  # For invite-based join

    requester_key: str = ""


class PoolMemberUpdate(BaseMessage):
    """Notify network of membership changes."""

    type: MessageType = MessageType.POOL_MEMBER_UPDATE
    pool_id: str
    action: str  # "add", "remove", "promote", "demote"
    member_key: str
    new_role: Optional[str] = None
    updated_by: str = ""  # Admin who made the change
    manifest_signatures: dict = Field(
        default_factory=dict
    )  # Signatures of the NEW manifest state


class PoolDeleteMessage(BaseMessage):
    """Notify network that a pool has been deleted by its creator."""

    type: MessageType = MessageType.POOL_DELETE
    pool_id: str
    deleted_by: str = ""  # Public key of the creator who deleted it


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
    except (ValueError, Exception):
        return None
