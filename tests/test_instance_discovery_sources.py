from unittest.mock import patch

import pytest
from pydantic import ValidationError

from comet.core.capabilities import CapabilityPlanner
from comet.core.discovery_sources import (
    ANIMETOSHO_USENET_SOURCE_ID,
    effective_discovery_sources,
)
from comet.core.models import ConfigModel, settings
from comet.core.sources import TransportKind
from comet.discovery.capabilities import (
    build_discovery_branch_fingerprints,
    build_discovery_capability_bindings,
)
from comet.discovery.registry import build_discovery_adapters
from comet.playback.tokens import CapabilityCodec
from comet.usenet.access import NativeAccessAuthorizer


@pytest.mark.parametrize(
    "kind",
    ("torrent", "comet", "torrentio", "jackett", "prowlarr", "animetosho"),
)
def test_server_discovery_kinds_are_not_user_configuration(kind):
    with pytest.raises(ValidationError, match="source kind"):
        ConfigModel.model_validate(
            {
                "schemaVersion": 2,
                "enabledTransports": ["usenet"],
                "discoverySources": [
                    {
                        "configurationId": ("11111111-1111-4111-8111-111111111111"),
                        "kind": kind,
                    }
                ],
            }
        )


def test_instance_flag_injects_only_animetosho_usenet():
    config = {
        "schemaVersion": 2,
        "discoverySources": [],
    }
    with (
        patch.object(settings, "USENET_ENABLED", True),
        patch.object(settings, "SCRAPE_ANIMETOSHO_USENET", True),
    ):
        sources = effective_discovery_sources(config)

    assert [source["kind"] for source in sources] == ["animetosho"]
    assert config["discoverySources"] == []


def test_instance_source_feeds_registry_plan_and_cache_identity():
    config = {
        "schemaVersion": 2,
        "enabledTransports": ["usenet"],
        "playbackProviders": [
            {
                "configurationId": "provider",
                "displayName": "TorBox Usenet",
                "kind": "torbox_usenet",
                "enabled": True,
                "options": {"apiKey": "user-playback-token"},
            }
        ],
        "discoverySources": [],
    }
    with (
        patch.object(settings, "USENET_ENABLED", True),
        patch.object(settings, "SCRAPE_ANIMETOSHO_USENET", True),
    ):
        codec = CapabilityCodec("Y2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2M")
        adapters = build_discovery_adapters(config, object())
        plan = CapabilityPlanner(
            usenet_offered=True,
            native_authorizer=NativeAccessAuthorizer(None),
        ).build(config)
        bindings = build_discovery_capability_bindings(config, codec)
        fingerprints = build_discovery_branch_fingerprints(
            config,
            codec,
            account_partition=b"a" * 32,
        )

    assert set(adapters) == {ANIMETOSHO_USENET_SOURCE_ID}
    assert plan.discovery_source_ids == (ANIMETOSHO_USENET_SOURCE_ID,)
    assert [binding.source_configuration_id for binding in bindings] == [
        ANIMETOSHO_USENET_SOURCE_ID
    ]
    assert all(
        source.branches == frozenset({TransportKind.USENET})
        for source in plan.discovery
    )
    assert {
        (fingerprint.source_configuration_id, fingerprint.branch_family)
        for fingerprint in fingerprints
    } == {
        (ANIMETOSHO_USENET_SOURCE_ID, "usenet"),
    }
    assert all(fingerprint.public_visibility for fingerprint in fingerprints)
