"""
CometNet - Decentralized P2P Network for Comet Instances

This package implements a gossip-based P2P network that allows Comet instances
to share torrent metadata automatically across the network.
"""

from typing import Optional

from comet.cometnet.interface import CometNetBackend
from comet.cometnet.manager import CometNetService, get_cometnet_service
from comet.cometnet.relay import CometNetRelay, get_relay

__all__ = ["CometNetService", "CometNetRelay", "CometNetBackend", "get_active_backend"]


def get_active_backend() -> Optional[CometNetBackend]:
    """
    Get the active CometNet backend (either local service or relay).
    Returns the backend instance if running, otherwise None.
    """
    # Try local service first
    service = get_cometnet_service()
    if service and service.running:
        return service

    # Fall back to relay
    relay = get_relay()
    if relay and relay.running:
        return relay

    return None
