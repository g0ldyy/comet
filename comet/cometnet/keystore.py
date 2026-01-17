"""
CometNet Public Key Store

Manages storage and retrieval of peer public keys for signature verification.
"""

import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from comet.core.logger import logger


@dataclass
class PeerKey:
    """Stores a peer's public key and related metadata."""

    node_id: str
    public_key_hex: str
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    verified: bool = False  # True if we've verified this key in a handshake

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
        self.max_keys = max_keys
        self._keys: Dict[str, PeerKey] = {}

    def store_key(
        self, node_id: str, public_key_hex: str, verified: bool = False
    ) -> None:
        """
        Store a peer's public key.

        Args:
            node_id: The peer's node ID (hash of public key)
            public_key_hex: The peer's public key in hex format
            verified: True if we verified this key during handshake
        """
        if node_id in self._keys:
            # Update existing entry
            self._keys[node_id].update_seen()
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
            return self._keys[node_id].public_key_hex
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

        # Sort by last_seen and remove oldest 10%
        sorted_keys = sorted(self._keys.items(), key=lambda x: x[1].last_seen)
        to_remove = max(1, len(sorted_keys) // 10)

        for node_id, _ in sorted_keys[:to_remove]:
            del self._keys[node_id]

        logger.debug(f"Evicted {to_remove} old keys from PublicKeyStore")

    def cleanup_old_keys(self, max_age_days: float = 30.0) -> int:
        """Remove keys that haven't been seen in a while."""
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

    def from_dict(self, data: Dict) -> None:
        """Load from persisted data."""
        keys_data = data.get("keys", {})
        for node_id, key_info in keys_data.items():
            self._keys[node_id] = PeerKey(
                node_id=node_id,
                public_key_hex=key_info["public_key_hex"],
                first_seen=key_info.get("first_seen", time.time()),
                last_seen=key_info.get("last_seen", time.time()),
                verified=key_info.get("verified", False),
            )

        logger.log("COMETNET", f"Loaded {len(self._keys)} public keys from storage")
