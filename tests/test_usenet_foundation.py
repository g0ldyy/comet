import base64
import json
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from comet.core.capabilities import CapabilityPlanner, CapabilityStateSnapshot
from comet.core.capability_states import EffectiveCapabilityState
from comet.core.config_validation import _parse_and_validate_config
from comet.core.models import AppSettings
from comet.usenet.access import NativeAccessAuthorizer


def _encoded(payload):
    return base64.b64encode(json.dumps(payload).encode()).decode()


def _v2_config(token="native-token"):
    return {
        "schemaVersion": 2,
        "enabledTransports": ["bittorrent", "usenet"],
        "playbackProviders": [
            {
                "configurationId": "11111111-1111-4111-8111-111111111111",
                "displayName": "My native provider",
                "kind": "comet_native_usenet",
                "enabled": True,
                "options": {"source": "instance_pool"},
            },
            {
                "configurationId": "22222222-2222-4222-8222-222222222222",
                "displayName": "TorBox Usenet",
                "kind": "torbox_usenet",
                "enabled": True,
            },
        ],
        "discoverySources": [
            {
                "configurationId": "33333333-3333-4333-8333-333333333333",
                "kind": "newznab",
                "enabled": True,
                "options": {},
            }
        ],
        "nativeAccessToken": token,
    }


def test_config_parser_does_not_hide_implementation_failures():
    _parse_and_validate_config.cache_clear()
    try:
        with (
            patch(
                "comet.core.models.CometSettingsModel.model_copy",
                side_effect=RuntimeError("implementation failed"),
            ),
            pytest.raises(RuntimeError, match="implementation failed"),
        ):
            _parse_and_validate_config(_encoded(_v2_config()))
    finally:
        _parse_and_validate_config.cache_clear()


def test_v2_config_preserves_explicit_disabled_entries():
    config = _v2_config()
    config["playbackProviders"][1]["enabled"] = False

    parsed = _parse_and_validate_config(_encoded(config))

    assert parsed["playbackProviders"][1]["enabled"] is False
    assert parsed["enabledTransports"] == ("bittorrent", "usenet")


def test_v2_config_preserves_inactive_usenet_bindings():
    config = _v2_config()
    config["enabledTransports"] = ["bittorrent"]
    config["playbackProviders"] = []

    parsed = _parse_and_validate_config(_encoded(config))

    assert parsed is not None
    assert parsed["enabledTransports"] == ("bittorrent",)
    assert parsed["discoverySources"][0]["kind"] == "newznab"


def test_v2_discovery_source_display_name_is_normalized_and_preserved():
    config = _v2_config()
    config["discoverySources"][0]["displayName"] = "  My indexer  "

    parsed = _parse_and_validate_config(_encoded(config))

    assert parsed["discoverySources"][0]["displayName"] == "My indexer"


def test_stremio_nntp_binding_accepts_the_server_contract_without_acknowledgements():
    config = _v2_config()
    config["playbackProviders"] = [
        {
            "configurationId": "11111111-1111-4111-8111-111111111111",
            "displayName": "Client NNTP",
            "kind": "stremio_nntp",
            "enabled": True,
            "options": {
                "servers": [
                    {
                        "host": "news.example",
                        "port": 563,
                        "tls_mode": "implicit_tls",
                        "username": "member",
                        "password": "secret",
                        "connections": 2,
                    }
                ]
            },
        }
    ]

    parsed = _parse_and_validate_config(_encoded(config))

    assert parsed is not None
    assert (
        parsed["playbackProviders"][0]["options"]["servers"][0]["host"]
        == "news.example"
    )


@pytest.mark.parametrize(
    ("path", "field"),
    [
        (("playbackProviders", 0), "futureProviderField"),
        (("discoverySources", 0), "futureSourceField"),
    ],
)
def test_v2_config_rejects_unknown_usenet_envelope_fields(path, field):
    config = _v2_config()
    config["discoverySources"][0]["options"] = {}
    target = config
    for component in path:
        target = target[component]
    target[field] = "opaque future value\nnot consumed by Comet"

    assert _parse_and_validate_config(_encoded(config)) is None


def test_v2_config_ignores_unknown_top_level_fields():
    config = _v2_config()
    config["futureTopLevel"] = "opaque future value\nnot consumed by Comet"

    assert _parse_and_validate_config(_encoded(config)) is not None


def test_v2_config_preserves_provider_specific_options():
    config = _v2_config()
    config["playbackProviders"][0]["options"]["futureProviderOption"] = "opaque"
    config["discoverySources"][0]["options"] = {"futureSourceOption": "opaque"}

    parsed = _parse_and_validate_config(_encoded(config))

    assert parsed is not None
    assert parsed["playbackProviders"][0]["options"]["futureProviderOption"] == "opaque"
    assert parsed["discoverySources"][0]["options"]["futureSourceOption"] == "opaque"


def test_v2_config_rejects_unknown_typed_account_fields():
    config = _v2_config()
    config["accounts"] = {
        "torbox": {
            "kind": "torbox",
            "apiKey": "secret",
            "futureCredential": "must-not-be-ignored",
        }
    }
    config["playbackProviders"][1]["accountId"] = "torbox"
    config["discoverySources"][0]["accountId"] = "torbox"

    assert _parse_and_validate_config(_encoded(config)) is None


def test_legacy_config_keeps_ignoring_unknown_fields_for_compatibility():
    parsed = _parse_and_validate_config(
        _encoded(
            {
                "schemaVersion": 1,
                "debridService": "torrent",
                "legacyExtension": {"preservedByOldClients": True},
            }
        )
    )

    assert parsed is not None
    assert parsed["_enableTorrent"] is True


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("discoverySources", 0, "displayName"), "   "),
        (("discoverySources", 0, "kind"), "generic_nzb_bridge"),
        (("playbackProviders", 0, "configurationId"), "native"),
    ],
    ids=("empty-display-name", "unknown-source-kind", "non-uuid-provider-id"),
)
def test_v2_config_rejects_noncanonical_fields(path, value):
    config = _v2_config()
    target = config
    for component in path[:-1]:
        target = target[component]
    target[path[-1]] = value

    assert _parse_and_validate_config(_encoded(config)) is None


def test_native_gate_compares_non_ascii_tokens():
    authorizer = NativeAccessAuthorizer("clé-native-secrète")

    assert authorizer.authorized("clé-native-secrète") is True
    assert authorizer.authorized("cle-native-secrete") is False
    assert NativeAccessAuthorizer("ascii-token").authorized("clé") is False
    assert authorizer.authorized(None) is False
    assert authorizer.error_code(None) == "native_access_token_required"
    assert authorizer.error_code("wrong") == "native_access_token_rejected"


def test_native_gate_builds_a_plan_for_a_non_ascii_access_token():
    token = "clé-native-secrète"
    config = _parse_and_validate_config(_encoded(_v2_config(token)))
    assert config is not None
    planner = CapabilityPlanner(
        usenet_offered=True,
        native_authorizer=NativeAccessAuthorizer(token),
        native_engine_enabled=True,
        native_instance_pool_available=True,
    )

    plan = planner.build(config)

    assert "comet_native_usenet" in [provider.kind for provider in plan.providers]


def test_native_gate_removes_only_native_provider_when_token_mismatches():
    config = _parse_and_validate_config(_encoded(_v2_config("wrong")))
    planner = CapabilityPlanner(
        usenet_offered=True,
        native_authorizer=NativeAccessAuthorizer("correct"),
        native_engine_enabled=True,
        native_instance_pool_available=True,
    )

    plan = planner.build(config)

    assert [provider.kind for provider in plan.providers] == ["torbox_usenet"]
    assert plan.discovery_source_ids == ("33333333-3333-4333-8333-333333333333",)
    assert len(plan.diagnostics) == 1


def test_persisted_capability_states_prune_failed_bindings_before_discovery():
    config = _parse_and_validate_config(_encoded(_v2_config()))
    planner = CapabilityPlanner(
        usenet_offered=True,
        native_authorizer=NativeAccessAuthorizer("native-token"),
        native_engine_enabled=True,
        native_instance_pool_available=True,
    )
    valid = EffectiveCapabilityState("valid", True, False, False)
    auth_failed = EffectiveCapabilityState(
        "auth_failed",
        False,
        False,
        False,
        "authentication_failed",
    )

    plan = planner.build(
        config,
        CapabilityStateSnapshot(
            providers={
                "11111111-1111-4111-8111-111111111111": valid,
                "22222222-2222-4222-8222-222222222222": auth_failed,
            },
            discovery={"33333333-3333-4333-8333-333333333333": valid},
        ),
    )

    assert [provider.kind for provider in plan.providers] == ["comet_native_usenet"]
    assert plan.discovery_source_ids == ("33333333-3333-4333-8333-333333333333",)
    assert plan.diagnostics == ("TorBox Usenet: authentication failed",)


def test_last_known_good_capabilities_remain_eligible_but_degraded():
    config = _parse_and_validate_config(_encoded(_v2_config()))
    planner = CapabilityPlanner(
        usenet_offered=True,
        native_authorizer=NativeAccessAuthorizer("native-token"),
        native_engine_enabled=True,
        native_instance_pool_available=True,
    )
    degraded = EffectiveCapabilityState(
        "transiently_unreachable",
        True,
        True,
        True,
        "provider_timeout",
        30,
    )

    plan = planner.build(
        config,
        CapabilityStateSnapshot(
            providers={
                "11111111-1111-4111-8111-111111111111": degraded,
                "22222222-2222-4222-8222-222222222222": degraded,
            },
            discovery={
                "33333333-3333-4333-8333-333333333333": degraded,
            },
        ),
    )

    assert all(provider.degraded for provider in plan.providers)
    assert plan.discovery[0].degraded is True
    assert len(plan.diagnostics) == 3
    assert all(
        diagnostic.endswith(
            "temporarily unreachable; using last validated capabilities"
        )
        for diagnostic in plan.diagnostics
    )


def test_missing_capability_evidence_is_pending_and_ineligible():
    config = _parse_and_validate_config(_encoded(_v2_config()))
    planner = CapabilityPlanner(
        usenet_offered=True,
        native_authorizer=NativeAccessAuthorizer("native-token"),
        native_engine_enabled=True,
        native_instance_pool_available=True,
    )

    plan = planner.build(
        config,
        CapabilityStateSnapshot(
            providers={
                "11111111-1111-4111-8111-111111111111": EffectiveCapabilityState(
                    "pending_validation", False, False, False
                ),
                "22222222-2222-4222-8222-222222222222": EffectiveCapabilityState(
                    "pending_validation", False, False, False
                ),
            },
            discovery={},
        ),
    )

    assert plan.providers == ()
    assert plan.discovery_source_ids == ()
    assert plan.diagnostics == (
        "My native provider: validation required",
        "TorBox Usenet: validation required",
    )


def test_omitted_capability_evidence_does_not_fail_open():
    config = _parse_and_validate_config(_encoded(_v2_config()))
    valid = EffectiveCapabilityState("valid", True, False, False)
    planner = CapabilityPlanner(
        usenet_offered=True,
        native_authorizer=NativeAccessAuthorizer("native-token"),
        native_engine_enabled=True,
        native_instance_pool_available=True,
    )
    plan = planner.build(
        config,
        CapabilityStateSnapshot(
            providers={"22222222-2222-4222-8222-222222222222": valid},
            discovery={"33333333-3333-4333-8333-333333333333": valid},
        ),
    )

    assert [provider.kind for provider in plan.providers] == ["torbox_usenet"]
    assert plan.diagnostics == ("My native provider: validation required",)

    plan = planner.build(
        config,
        CapabilityStateSnapshot(
            providers={
                "11111111-1111-4111-8111-111111111111": valid,
                "22222222-2222-4222-8222-222222222222": valid,
            },
            discovery={},
        ),
    )

    assert plan.discovery_source_ids == ()
    assert plan.diagnostics == ("newznab: validation required",)


def test_direct_torrent_provider_does_not_require_capability_evidence():
    config = _v2_config()
    config["playbackProviders"].append(
        {
            "configurationId": "44444444-4444-4444-8444-444444444444",
            "displayName": "Direct torrent",
            "kind": "direct_torrent",
            "enabled": True,
        }
    )
    config = _parse_and_validate_config(_encoded(config))
    valid = EffectiveCapabilityState("valid", True, False, False)
    planner = CapabilityPlanner(
        usenet_offered=True,
        native_authorizer=NativeAccessAuthorizer("native-token"),
        native_engine_enabled=True,
        native_instance_pool_available=True,
    )

    plan = planner.build(
        config,
        CapabilityStateSnapshot(
            providers={
                "11111111-1111-4111-8111-111111111111": valid,
                "22222222-2222-4222-8222-222222222222": valid,
            },
            discovery={"33333333-3333-4333-8333-333333333333": valid},
        ),
    )

    assert [provider.kind for provider in plan.providers] == [
        "comet_native_usenet",
        "torbox_usenet",
        "direct_torrent",
    ]
    assert plan.discovery_source_ids == ("33333333-3333-4333-8333-333333333333",)
    assert plan.diagnostics == ()


def test_easynews_automatically_reaches_configured_real_nzb_targets():
    config = {
        "schemaVersion": 2,
        "enabledTransports": ["usenet"],
        "playbackProviders": [
            {
                "configurationId": "11111111-1111-4111-8111-111111111111",
                "displayName": "Client",
                "kind": "stremio_nntp",
                "enabled": True,
                "options": {},
            }
        ],
        "discoverySources": [
            {
                "configurationId": "22222222-2222-4222-8222-222222222222",
                "kind": "easynews",
                "enabled": True,
                "accountId": "account",
                "options": {},
            }
        ],
    }
    planner = CapabilityPlanner(
        usenet_offered=True,
        native_authorizer=NativeAccessAuthorizer(None),
    )

    compatible = planner.build(config)
    config["playbackProviders"][0]["kind"] = "direct_torrent"
    incompatible = planner.build(config)

    assert compatible.discovery_source_ids == ("22222222-2222-4222-8222-222222222222",)
    assert incompatible.discovery_source_ids == ()


def test_usenet_settings_reject_invalid_private_origin():
    with pytest.raises(ValidationError):
        AppSettings(USENET_PRIVATE_UPSTREAM_ORIGINS=["https://example.test/path"])


def test_usenet_private_origins_use_canonical_effective_ports():
    defaults = AppSettings(
        USENET_PRIVATE_UPSTREAM_ORIGINS=[
            "http://bridge.test",
            "https://secure.test",
        ]
    )
    settings = AppSettings(USENET_PRIVATE_UPSTREAM_ORIGINS=["HTTP://Bridge.test:8080"])

    assert defaults.USENET_PRIVATE_UPSTREAM_ORIGINS == [
        "http://bridge.test:80",
        "https://secure.test:443",
    ]
    assert settings.USENET_PRIVATE_UPSTREAM_ORIGINS == ["http://bridge.test:8080"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("USENET_NATIVE_MAX_STREAMS", 0),
        ("USENET_MEMORY_CACHE_BYTES", 1024),
        ("USENET_DISK_CACHE_BYTES", -1),
        ("USENET_SPOOL_MAX_BYTES", 1024),
        ("USENET_ARCHIVE_JOBS", 0),
        ("USENET_REPAIR_JOBS", 0),
        ("USENET_START_TIMEOUT_SECONDS", 4),
        ("USENET_DRAIN_TIMEOUT_SECONDS", 301),
    ],
)
def test_usenet_resource_settings_reject_unsafe_bounds(field, value):
    with pytest.raises(ValidationError):
        AppSettings(**{field: value})


def test_usenet_worker_concurrency_has_no_arbitrary_upper_cap():
    settings = AppSettings(USENET_ARCHIVE_JOBS=65, USENET_REPAIR_JOBS=65)

    assert settings.USENET_ARCHIVE_JOBS == 65
    assert settings.USENET_REPAIR_JOBS == 65


def test_native_provider_requires_an_offered_engine_and_source():
    config = _parse_and_validate_config(_encoded(_v2_config()))
    planner = CapabilityPlanner(
        usenet_offered=True,
        native_authorizer=NativeAccessAuthorizer("native-token"),
    )

    plan = planner.build(config)

    assert [provider.kind for provider in plan.providers] == ["torbox_usenet"]
    assert plan.diagnostics == ("My native provider: native engine is unavailable",)


def test_v2_config_rejects_invalid_native_server_source_options():
    config = _v2_config()
    config["playbackProviders"][0]["options"] = {"source": "personal_servers"}

    assert _parse_and_validate_config(_encoded(config)) is None

    config["playbackProviders"][0]["options"] = {
        "source": "instance_pool",
        "servers": [],
    }
    assert _parse_and_validate_config(_encoded(config)) is not None


def test_source_uses_its_intrinsic_usenet_branch():
    config = _v2_config()
    config["playbackProviders"] = [config["playbackProviders"][1]]
    parsed = _parse_and_validate_config(_encoded(config))
    planner = CapabilityPlanner(
        usenet_offered=True,
        native_authorizer=NativeAccessAuthorizer("native-token"),
    )

    plan = planner.build(parsed)

    assert plan.discovery_source_ids == ("33333333-3333-4333-8333-333333333333",)
    assert tuple(plan.branches_for("33333333-3333-4333-8333-333333333333")) == (
        "usenet",
    )
