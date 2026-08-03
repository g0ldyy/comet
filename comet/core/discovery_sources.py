"""Canonical user and instance discovery-source policy."""

from collections.abc import Mapping
from uuid import UUID, uuid5

from comet.core.models import settings

_INSTANCE_SOURCE_NAMESPACE = UUID("a338a7dc-a92d-4c14-9f3a-f11fba3abed2")


def instance_discovery_source_id(source_key: str) -> str:
    """Return the stable persisted identity of an operator-owned source."""
    return str(uuid5(_INSTANCE_SOURCE_NAMESPACE, source_key))


ANIMETOSHO_USENET_SOURCE_ID = instance_discovery_source_id("animetosho-usenet")


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
