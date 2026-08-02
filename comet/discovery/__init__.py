"""Transport-neutral discovery adapters with lazy public exports."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from comet.discovery.manager import DiscoveryResult, SearchCoordinator
    from comet.discovery.registry import build_discovery_adapters

__all__ = ("DiscoveryResult", "SearchCoordinator", "build_discovery_adapters")


def __getattr__(name: str):
    if name in {"DiscoveryResult", "SearchCoordinator"}:
        from comet.discovery.manager import DiscoveryResult, SearchCoordinator

        return {
            "DiscoveryResult": DiscoveryResult,
            "SearchCoordinator": SearchCoordinator,
        }[name]
    if name == "build_discovery_adapters":
        from comet.discovery.registry import build_discovery_adapters

        return build_discovery_adapters
    raise AttributeError(name)
