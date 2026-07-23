"""
CometNet Public Key Store

Manages storage and retrieval of peer public keys for signature verification.
"""

import math
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, Optional

from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePublicKey

from comet.cometnet.crypto import NodeIdentity
from comet.core.logger import logger


@dataclass
class PeerKey:
    """Stores a peer's public key and related metadata."""

    node_id: str
    public_key_hex: str
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    verified: bool = False  # True if we've verified this key in a handshake
    _cached_key: Optional[EllipticCurvePublicKey] = None

    def get_key_obj(self) -> Optional[EllipticCurvePublicKey]:
        """Get the cached key object, loading it if necessary."""
        if self._cached_key is None:
            self._cached_key = NodeIdentity.load_public_key(self.public_key_hex)
        return self._cached_key

    def update_seen(self) -> None:
        """Update the last seen timestamp."""
        self.last_seen = time.time()


class PublicKeyStore:
    """
    Stores public keys for all known peers.

    Keys are learned during handshakes and used to verify
    contributor signatures on torrent announcements.
    """

    def __init__(self, max_keys: int = 10000):
        if type(max_keys) is not int or max_keys <= 0:
            raise ValueError("max_keys must be a positive integer")
        self.max_keys = max_keys
        self._keys: OrderedDict[str, PeerKey] = OrderedDict()

    @staticmethod
    def _validate_key_identity(node_id: object, public_key_hex: object) -> str:
        if type(node_id) is not str or not node_id:
            raise ValueError("node_id must be a non-empty string")
        if type(public_key_hex) is not str or not public_key_hex:
            raise ValueError("public_key_hex must be a non-empty string")
        key = NodeIdentity.load_public_key(public_key_hex)
        if not isinstance(key, EllipticCurvePublicKey):
            raise ValueError("public_key_hex must contain a DER elliptic-curve key")
        if NodeIdentity.node_id_from_public_key(public_key_hex) != node_id:
            raise ValueError("node_id must derive from public_key_hex")
        return public_key_hex

    @classmethod
    def _peer_from_persisted(
        cls, node_id: object, value: object
    ) -> tuple[str, PeerKey]:
        if type(value) is not dict or set(value) != {
            "public_key_hex",
            "first_seen",
            "last_seen",
            "verified",
        }:
            raise ValueError("persisted peer key does not match the current schema")

        public_key_hex = cls._validate_key_identity(node_id, value["public_key_hex"])
        first_seen = value["first_seen"]
        last_seen = value["last_seen"]
        if any(
            type(timestamp) not in (int, float)
            or not math.isfinite(timestamp)
            or timestamp < 0
            for timestamp in (first_seen, last_seen)
        ):
            raise ValueError(
                "persisted peer timestamps must be finite and non-negative"
            )
        if last_seen < first_seen:
            raise ValueError("persisted peer last_seen cannot precede first_seen")
        if type(value["verified"]) is not bool:
            raise ValueError("persisted peer verified must be a boolean")

        return node_id, PeerKey(
            node_id=node_id,
            public_key_hex=public_key_hex,
            first_seen=first_seen,
            last_seen=last_seen,
            verified=value["verified"],
        )

    def store_key(self, node_id: str, public_key_hex: str) -> None:
        """Store a valid contributor key without handshake authority."""
        self._store_key(node_id, public_key_hex, verified=False)

    def store_verified_key(self, node_id: str, public_key_hex: str) -> None:
        """Store a key whose identity was proven by the transport handshake."""
        self._store_key(node_id, public_key_hex, verified=True)

    def _store_key(self, node_id: str, public_key_hex: str, *, verified: bool) -> None:
        self._validate_key_identity(node_id, public_key_hex)

        if node_id in self._keys:
            # Update existing entry
            if self._keys[node_id].public_key_hex != public_key_hex:
                raise ValueError("stored node ID cannot change public key")
            self._keys[node_id].update_seen()
            self._keys.move_to_end(node_id)
            if verified:
                self._keys[node_id].verified = True
        else:
            # Add new entry
            self._keys[node_id] = PeerKey(
                node_id=node_id,
                public_key_hex=public_key_hex,
                verified=verified,
            )

            # Enforce max size
            if len(self._keys) > self.max_keys:
                self._evict_oldest()

    def get_key(self, node_id: str) -> Optional[str]:
        """
        Get a peer's public key if we have it.

        Returns the public key hex string, or None if not found.
        """
        if node_id in self._keys:
            self._keys[node_id].update_seen()
            self._keys.move_to_end(node_id)
            return self._keys[node_id].public_key_hex
        return None

    def get_key_obj(self, node_id: str) -> Optional[EllipticCurvePublicKey]:
        """
        Get a peer's public key object.
        """
        if node_id in self._keys:
            self._keys[node_id].update_seen()
            self._keys.move_to_end(node_id)
            return self._keys[node_id].get_key_obj()
        return None

    def is_verified(self, node_id: str) -> bool:
        """Check if we have a verified key for this peer."""
        return node_id in self._keys and self._keys[node_id].verified

    def has_key(self, node_id: str) -> bool:
        """Check if we have any key for this peer."""
        return node_id in self._keys

    def remove_key(self, node_id: str) -> None:
        """Remove a peer's key."""
        self._keys.pop(node_id, None)

    def _evict_oldest(self) -> None:
        """Remove the oldest (least recently seen) keys."""
        if not self._keys:
            return

        # Remove oldest 10% (LRU)
        to_remove = max(1, len(self._keys) // 10)

        for _ in range(to_remove):
            self._keys.popitem(last=False)

        logger.debug(f"Evicted {to_remove} old keys from PublicKeyStore")

    def cleanup_old_keys(self, max_age_days: float = 30.0) -> int:
        """Remove keys that haven't been seen in a while."""
        if (
            type(max_age_days) not in (int, float)
            or not math.isfinite(max_age_days)
            or max_age_days <= 0
        ):
            raise ValueError("max_age_days must be a finite positive number")
        cutoff = time.time() - (max_age_days * 86400)
        to_remove = [
            node_id for node_id, key in self._keys.items() if key.last_seen < cutoff
        ]
        for node_id in to_remove:
            del self._keys[node_id]
        return len(to_remove)

    def get_stats(self) -> Dict:
        """Get statistics about stored keys."""
        verified_count = sum(1 for k in self._keys.values() if k.verified)
        return {
            "total_keys": len(self._keys),
            "verified_keys": verified_count,
            "unverified_keys": len(self._keys) - verified_count,
        }

    def to_dict(self) -> Dict:
        """Serialize for persistence."""
        return {
            "keys": {
                node_id: {
                    "public_key_hex": key.public_key_hex,
                    "first_seen": key.first_seen,
                    "last_seen": key.last_seen,
                    "verified": key.verified,
                }
                for node_id, key in self._keys.items()
            }
        }

    @classmethod
    def validate_persisted(cls, data: object, *, max_keys: int = 10000) -> None:
        """Validate a complete persisted candidate without publishing it."""
        if type(max_keys) is not int or max_keys <= 0:
            raise ValueError("max_keys must be a positive integer")
        if type(data) is not dict or set(data) != {"keys"}:
            raise ValueError("keystore does not match the current schema")
        if type(data["keys"]) is not dict:
            raise ValueError("keystore keys must be an object")
        if len(data["keys"]) > max_keys:
            raise ValueError("persisted keystore exceeds max_keys")
        for node_id, value in data["keys"].items():
            cls._peer_from_persisted(node_id, value)

    def from_dict(self, data: Dict) -> None:
        """Load from persisted data."""
        self.validate_persisted(data, max_keys=self.max_keys)

        validated = [
            self._peer_from_persisted(node_id, value)
            for node_id, value in data["keys"].items()
        ]
        # Sort by last_seen to preserve LRU order when reloading
        sorted_items = sorted(
            validated,
            key=lambda item: item[1].last_seen,
        )
        self._keys = OrderedDict(sorted_items)

        logger.log("COMETNET", f"Loaded {len(self._keys)} public keys from storage")
