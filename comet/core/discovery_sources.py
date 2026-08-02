"""Canonical user and instance discovery-source policy."""

from collections.abc import Mapping
from uuid import UUID, uuid5

from comet.core.models import settings

_INSTANCE_SOURCE_NAMESPACE = UUID("a338a7dc-a92d-4c14-9f3a-f11fba3abed2")
ANIMETOSHO_USENET_SOURCE_ID = str(
    uuid5(_INSTANCE_SOURCE_NAMESPACE, "animetosho-usenet")
)


def effective_discovery_sources(
    config: Mapping[str, object],
) -> tuple[Mapping, ...]:
    """Combine user sources with sources enabled by the operator."""
    configured = tuple(config.get("discoverySources") or ())
    if (
        config.get("schemaVersion") == 2
        and settings.USENET_ENABLED
        and settings.SCRAPE_ANIMETOSHO_USENET
    ):
        return configured + (
            {
                "configurationId": ANIMETOSHO_USENET_SOURCE_ID,
                "displayName": "AnimeTosho",
                "kind": "animetosho",
                "enabled": True,
                "options": {},
            },
        )
    return configured
