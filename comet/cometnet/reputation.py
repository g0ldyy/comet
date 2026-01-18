"""
CometNet Reputation Module

Implements the reputation system for tracking peer trustworthiness.
"""

import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from comet.core.logger import logger
from comet.core.models import settings


@dataclass
class PeerReputation:
    """Tracks reputation and metadata for a single peer."""

    node_id: str
    reputation: float = field(
        default_factory=lambda: settings.COMETNET_REPUTATION_INITIAL
    )
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    valid_contributions: int = 0
    invalid_contributions: int = 0
    messages_received: int = 0
    is_blacklisted: bool = False

    @property
    def anciennety_days(self) -> float:
        """Returns the number of days since first seen."""
        return (time.time() - self.first_seen) / 86400.0

    @property
    def anciennety_bonus(self) -> float:
        """Returns the reputation bonus from anciennety."""
        return min(
            self.anciennety_days
            * settings.COMETNET_REPUTATION_BONUS_PER_DAY_ANCIENNETY,
            settings.COMETNET_REPUTATION_BONUS_MAX_ANCIENNETY,
        )

    @property
    def effective_reputation(self) -> float:
        """Returns the effective reputation including anciennety bonus."""
        if self.is_blacklisted:
            return 0.0
        return min(
            self.reputation + self.anciennety_bonus, settings.COMETNET_REPUTATION_MAX
        )

    @property
    def trust_level(self) -> str:
        """Returns the trust level as a string."""
        if self.is_blacklisted:
            return "blacklisted"
        score = self.effective_reputation
        if score < settings.COMETNET_REPUTATION_THRESHOLD_UNTRUSTED:
            return "untrusted"
        elif score < settings.COMETNET_REPUTATION_THRESHOLD_TRUSTED:
            return "neutral"
        else:
            return "trusted"

    def is_trusted(self) -> bool:
        """Returns True if the peer is trusted."""
        return (
            not self.is_blacklisted
            and self.effective_reputation
            >= settings.COMETNET_REPUTATION_THRESHOLD_TRUSTED
        )

    def is_acceptable(self) -> bool:
        """Returns True if messages from this peer should be processed."""
        return (
            not self.is_blacklisted
            and self.effective_reputation
            >= settings.COMETNET_REPUTATION_THRESHOLD_UNTRUSTED
        )

    def update_seen(self) -> None:
        """Update the last seen timestamp."""
        self.last_seen = time.time()

    def add_valid_contribution(self, count: int = 1) -> None:
        """Add valid contribution(s) and update reputation."""
        self.valid_contributions += count
        self._adjust_reputation(
            settings.COMETNET_REPUTATION_BONUS_VALID_CONTRIBUTION * count
        )

    def add_invalid_contribution(self, count: int = 1) -> None:
        """Add invalid contribution(s) and update reputation."""
        self.invalid_contributions += count
        self._adjust_reputation(
            -settings.COMETNET_REPUTATION_PENALTY_INVALID_CONTRIBUTION * count
        )

    def add_signature_failure_penalty(self) -> None:
        """Apply invalid signature penalty to reputation."""
        self._adjust_reputation(-settings.COMETNET_REPUTATION_PENALTY_INVALID_SIGNATURE)

    def blacklist(self) -> None:
        """Blacklist this peer."""
        self.is_blacklisted = True
        logger.log("COMETNET", f"Peer {self.node_id[:16]}... has been blacklisted")

    def unblacklist(self) -> None:
        """Remove this peer from the blacklist."""
        self.is_blacklisted = False
        logger.log(
            "COMETNET", f"Peer {self.node_id[:16]}... has been removed from blacklist"
        )

    def _adjust_reputation(self, delta: float) -> None:
        """Adjust reputation by delta, clamping to valid range."""
        self.reputation = max(
            settings.COMETNET_REPUTATION_MIN,
            min(settings.COMETNET_REPUTATION_MAX, self.reputation + delta),
        )


class ReputationStore:
    """
    Manages reputation for all known peers.

    This is an in-memory store that can be persisted to disk.
    """

    def __init__(self):
        self._peers: Dict[str, PeerReputation] = {}
        self._blacklist: set[str] = set()

    def get_or_create(self, node_id: str) -> PeerReputation:
        """Get an existing peer reputation or create a new one."""
        if node_id not in self._peers:
            self._peers[node_id] = PeerReputation(node_id=node_id)
            if node_id in self._blacklist:
                self._peers[node_id].is_blacklisted = True
        return self._peers[node_id]

    def get(self, node_id: str) -> Optional[PeerReputation]:
        """Get peer reputation if it exists."""
        return self._peers.get(node_id)

    def is_peer_acceptable(self, node_id: str) -> bool:
        """Check if a peer is acceptable (not blacklisted and above untrusted threshold)."""
        if node_id in self._blacklist:
            return False
        peer = self._peers.get(node_id)
        if peer is None:
            # New peers are acceptable
            self.get_or_create(node_id)
            return True
        return peer.is_acceptable()

    def blacklist_peer(self, node_id: str) -> None:
        """Blacklist a peer by node ID."""
        self._blacklist.add(node_id)
        if node_id in self._peers:
            self._peers[node_id].blacklist()

    def unblacklist_peer(self, node_id: str) -> None:
        """Remove a peer from the blacklist."""
        self._blacklist.discard(node_id)
        if node_id in self._peers:
            self._peers[node_id].unblacklist()

    def get_trusted_peers(self) -> list[PeerReputation]:
        """Get all trusted peers."""
        return [p for p in self._peers.values() if p.is_trusted()]

    def get_reputation_summary(self) -> Dict[str, int]:
        """Get a summary of peer reputations by trust level."""
        summary = {"trusted": 0, "neutral": 0, "untrusted": 0, "blacklisted": 0}
        for peer in self._peers.values():
            level = peer.trust_level
            if level in summary:
                summary[level] += 1
        return summary

    def to_dict(self) -> Dict:
        """Serialize the store to a dictionary for persistence."""
        return {
            "peers": {
                node_id: {
                    "reputation": peer.reputation,
                    "first_seen": peer.first_seen,
                    "last_seen": peer.last_seen,
                    "valid_contributions": peer.valid_contributions,
                    "invalid_contributions": peer.invalid_contributions,
                    "is_blacklisted": peer.is_blacklisted,
                }
                for node_id, peer in self._peers.items()
            },
            "blacklist": list(self._blacklist),
        }

    def from_dict(self, data: Dict) -> None:
        """Load the store from a dictionary."""
        self._blacklist = set(data.get("blacklist", []))
        for node_id, peer_data in data.get("peers", {}).items():
            peer = PeerReputation(
                node_id=node_id,
                reputation=peer_data.get(
                    "reputation", settings.COMETNET_REPUTATION_INITIAL
                ),
                first_seen=peer_data.get("first_seen", time.time()),
                last_seen=peer_data.get("last_seen", time.time()),
                valid_contributions=peer_data.get("valid_contributions", 0),
                invalid_contributions=peer_data.get("invalid_contributions", 0),
                is_blacklisted=peer_data.get("is_blacklisted", False),
            )
            self._peers[node_id] = peer
