"""
CometNet Backend Interface

Defines the common interface for both the local P2P service (CometNetService)
and the relay client (CometNetRelay). This allows the API and other components
to interact with CometNet transparently properly regardless of the running mode.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional


class CometNetBackend(ABC):
    """Abstract base class for CometNet backends."""

    @property
    @abstractmethod
    def running(self) -> bool:
        """Check if the backend is running."""
        pass

    @abstractmethod
    async def get_stats(self) -> Dict[str, Any]:
        """Get backend statistics."""
        pass

    @abstractmethod
    async def get_peers(self) -> Dict[str, Any]:
        """Get connected peers information."""
        pass

    @abstractmethod
    async def broadcast_torrent(self, metadata) -> None:
        """Broadcast a torrent to the network."""
        pass

    # --- Pool Management ---

    @abstractmethod
    async def get_pools(self) -> Dict[str, Any]:
        """Get all known pools."""
        pass

    @abstractmethod
    async def get_pool_details(self, pool_id: str) -> Optional[Dict[str, Any]]:
        """Get details for a specific pool."""
        pass

    @abstractmethod
    async def create_pool(
        self,
        pool_id: str,
        display_name: str,
        description: str = "",
        join_mode: str = "invite",
    ) -> Dict[str, Any]:
        """Create a new pool."""
        pass

    @abstractmethod
    async def delete_pool(self, pool_id: str) -> bool:
        """Delete a pool."""
        pass

    @abstractmethod
    async def join_pool_with_invite(
        self, pool_id: str, invite_code: str, node_url: Optional[str] = None
    ) -> bool:
        """Join a pool using an invite code."""
        pass

    @abstractmethod
    async def create_pool_invite(
        self,
        pool_id: str,
        expires_in: Optional[int] = None,
        max_uses: Optional[int] = None,
    ) -> Optional[str]:
        """Create an invitation link for a pool."""
        pass

    @abstractmethod
    async def get_pool_invites(self, pool_id: str) -> Dict[str, Any]:
        """Get all active invites for a pool."""
        pass

    @abstractmethod
    async def delete_pool_invite(self, pool_id: str, invite_code: str) -> bool:
        """Delete a pool invite."""
        pass

    @abstractmethod
    async def subscribe_to_pool(self, pool_id: str) -> bool:
        """Subscribe to a pool."""
        pass

    @abstractmethod
    async def unsubscribe_from_pool(self, pool_id: str) -> bool:
        """Unsubscribe from a pool."""
        pass

    @abstractmethod
    async def add_pool_member(
        self, pool_id: str, member_key: str, role: str = "member"
    ) -> bool:
        """Add a member to a pool."""
        pass

    @abstractmethod
    async def remove_pool_member(self, pool_id: str, member_key: str) -> bool:
        """Remove a member from a pool (kick)."""
        pass

    @abstractmethod
    async def update_member_role(
        self, pool_id: str, member_key: str, new_role: str
    ) -> bool:
        """Update a member's role."""
        pass
