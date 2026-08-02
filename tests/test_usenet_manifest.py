import asyncio
from unittest.mock import patch

import pytest

from comet.core.capability_states import EffectiveCapabilityState
from comet.core.manifest_branding import eligible_usenet_provider_badges
from comet.debrid.manager import build_addon_name


def test_manifest_badges_include_only_configured_eligible_usenet_providers():
    config = {
        "schemaVersion": 2,
        "enabledTransports": ["usenet"],
        "playbackProviders": [
            {
                "configurationId": "11111111-1111-4111-8111-111111111111",
                "displayName": "Primary",
                "kind": "easynews",
                "enabled": True,
                "options": {"username": "member", "password": "secret"},
            },
            {
                "configurationId": "22222222-2222-4222-8222-222222222222",
                "displayName": "Bridge",
                "kind": "nzbdav",
                "enabled": True,
                "options": {
                    "internalBaseUrl": "https://nzbdav.test",
                    "sabApiKey": "sab-secret",
                    "webdavUsername": "member",
                    "webdavPassword": "secret",
                },
            },
            {
                "configurationId": "33333333-3333-4333-8333-333333333333",
                "displayName": "Disabled",
                "kind": "altmount",
                "enabled": False,
                "options": {},
            },
        ],
    }
    states = [
        EffectiveCapabilityState("valid", True, False, False),
        EffectiveCapabilityState(
            "auth_failed",
            False,
            False,
            False,
            "credentials_rejected",
        ),
    ]
    with patch(
        "comet.core.manifest_branding.CapabilityStateRepository.effective",
        side_effect=states,
    ):
        badges = asyncio.run(
            eligible_usenet_provider_badges(
                config,
                object(),
                usenet_offered=True,
                capability_secret="Y2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2M",
                native_access_token=None,
                native_servers=[],
            )
        )

    assert badges == ("EASYNEWS",)
    assert build_addon_name("Comet", config, badges) == "Comet | EASYNEWS"


def test_manifest_badges_disappear_with_the_usenet_transport():
    assert (
        asyncio.run(
            eligible_usenet_provider_badges(
                {"schemaVersion": 2, "enabledTransports": ["bittorrent"]},
                object(),
                usenet_offered=True,
                capability_secret="Y2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2M",
                native_access_token=None,
                native_servers=[],
            )
        )
        == ()
    )


def test_manifest_badges_do_not_hide_capability_storage_failures():
    config = {
        "schemaVersion": 2,
        "enabledTransports": ["usenet"],
        "playbackProviders": [
            {
                "configurationId": "11111111-1111-4111-8111-111111111111",
                "displayName": "Primary",
                "kind": "easynews",
                "enabled": True,
                "options": {"username": "member", "password": "secret"},
            }
        ],
    }
    with (
        patch(
            "comet.core.manifest_branding.CapabilityStateRepository.effective",
            side_effect=RuntimeError("storage failed"),
        ),
        pytest.raises(RuntimeError, match="storage failed"),
    ):
        asyncio.run(
            eligible_usenet_provider_badges(
                config,
                object(),
                usenet_offered=True,
                capability_secret="Y2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2M",
                native_access_token=None,
                native_servers=[],
            )
        )
