"""Import-safe public accessors for the optional CometNet runtime."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from comet.cometnet.interface import CometNetBackend


def get_active_backend() -> CometNetBackend | None:
    from comet.cometnet.manager import get_cometnet_service
    from comet.cometnet.relay import get_relay

    service = get_cometnet_service()
    if service and service.running:
        return service
    relay = get_relay()
    if relay and relay.running:
        return relay
    return None


def __getattr__(name: str):
    if name == "CometNetBackend":
        from comet.cometnet.interface import CometNetBackend

        return CometNetBackend
    if name == "CometNetService":
        from comet.cometnet.manager import CometNetService

        return CometNetService
    if name == "CometNetRelay":
        from comet.cometnet.relay import CometNetRelay

        return CometNetRelay
    raise AttributeError(name)


__all__ = ("CometNetBackend", "CometNetRelay", "CometNetService", "get_active_backend")
