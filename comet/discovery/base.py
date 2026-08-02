from typing import Protocol

from comet.discovery.models import DiscoveryBatch, DiscoveryContext, MediaQuery


class DiscoveryAdapter(Protocol):
    async def search(
        self, query: MediaQuery, context: DiscoveryContext
    ) -> DiscoveryBatch: ...
