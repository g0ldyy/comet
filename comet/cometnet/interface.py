"""
CometNet Backend Interface

Defines the common interface for both the local P2P service (CometNetService)
and the relay client (CometNetRelay). This allows the API and other components
to interact with CometNet transparently properly regardless of the running mode.
"""

from abc import ABC, abstractmethod
from typing import Any


class CometNetBackend(ABC):
    """Abstract base class for CometNet backends."""

    @property
    @abstractmethod
    def running(self) -> bool:
        """Check if the backend is running."""

    @abstractmethod
    async def get_stats(self) -> dict[str, Any]:
        """Get backend statistics."""

    @abstractmethod
    async def get_peers(self) -> dict[str, Any]:
        """Get connected peers information."""

    @abstractmethod
    async def broadcast_torrent(self, metadata) -> None:
        """Broadcast a torrent to the network."""

    @abstractmethod
    async def broadcast_torrents(self, metadata_list: list[Any]) -> None:
        """Broadcast multiple torrents to the network."""

    # --- Pool Management ---

    @abstractmethod
    async def get_pools(self) -> dict[str, Any]:
        """Get all known pools."""

    @abstractmethod
    async def get_pool_details(self, pool_id: str) -> dict[str, Any] | None:
        """Get details for a specific pool."""

    @abstractmethod
    async def create_pool(
        self,
        pool_id: str,
        display_name: str,
        description: str = "",
        join_mode: str = "invite",
    ) -> dict[str, Any]:
        """Create a new pool."""

    @abstractmethod
    async def delete_pool(self, pool_id: str) -> bool:
        """Delete a pool."""

    @abstractmethod
    async def join_pool_with_invite(
        self, pool_id: str, invite_code: str, node_url: str | None = None
    ) -> bool:
        """Join a pool using an invite code."""

    @abstractmethod
    async def create_pool_invite(
        self,
        pool_id: str,
        expires_in: int | None = None,
        max_uses: int | None = None,
    ) -> str | None:
        """Create an invitation link for a pool."""

    @abstractmethod
    async def get_pool_invites(self, pool_id: str) -> dict[str, Any]:
        """Get all active invites for a pool."""

    @abstractmethod
    async def delete_pool_invite(self, pool_id: str, invite_code: str) -> bool:
        """Delete a pool invite."""

    @abstractmethod
    async def subscribe_to_pool(self, pool_id: str) -> bool:
        """Subscribe to a pool."""

    @abstractmethod
    async def unsubscribe_from_pool(self, pool_id: str) -> bool:
        """Unsubscribe from a pool."""

    @abstractmethod
    async def add_pool_member(
        self, pool_id: str, member_key: str, role: str = "member"
    ) -> bool:
        """Add a member to a pool."""

    @abstractmethod
    async def remove_pool_member(self, pool_id: str, member_key: str) -> bool:
        """Remove a member from a pool (kick)."""

    @abstractmethod
    async def update_member_role(
        self, pool_id: str, member_key: str, new_role: str
    ) -> bool:
        """Update a member's role."""

    @abstractmethod
    async def leave_pool(self, pool_id: str) -> bool:
        """Leave a pool (self-removal). Any member except creator can leave."""
