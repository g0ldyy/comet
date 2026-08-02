import asyncio
import base64
from unittest.mock import AsyncMock, patch

import pytest

from comet.core.capability_states import EffectiveCapabilityState
from comet.discovery.capabilities import (
    build_discovery_branch_fingerprints,
    build_discovery_capability_bindings,
    build_discovery_capability_validator,
    ensure_discovery_capability_states,
    record_discovery_capability_failure,
)
from comet.playback.base import Actionability, ProviderStatus, Readiness
from comet.playback.tokens import CapabilityCodec

ROOT = base64.urlsafe_b64encode(b"d" * 32).decode().rstrip("=")
SOURCE_ID = "11111111-1111-4111-8111-111111111111"


def _config(*, credential: str = "secret"):
    return {
        "schemaVersion": 2,
        "discoverySources": [
            {
                "configurationId": SOURCE_ID,
                "kind": "newznab",
                "enabled": True,
                "options": {
                    "endpoint": "https://indexer.example/api",
                    "apiKey": credential,
                },
            }
        ],
    }


def test_discovery_binding_separates_credentials_from_public_state():
    codec = CapabilityCodec(ROOT)
    baseline = build_discovery_capability_bindings(_config(), codec)[0]
    changed_credential = build_discovery_capability_bindings(
        _config(credential="different"),
        codec,
    )[0]
    assert "secret" not in repr(baseline)
    assert baseline.validation_options["apiKey"] == "secret"
    assert (
        baseline.binding.binding_fingerprint
        != changed_credential.binding.binding_fingerprint
    )


def test_discovery_binding_does_not_replace_malformed_accounts_with_empty_values():
    config = _config()
    config["accounts"] = []

    with pytest.raises(ValueError, match="accounts"):
        build_discovery_capability_bindings(config, CapabilityCodec(ROOT))


def test_discovery_binding_filter_selects_only_the_requested_source():
    config = _config()
    config["discoverySources"].append(
        {
            **config["discoverySources"][0],
            "configurationId": "22222222-2222-4222-8222-222222222222",
        }
    )

    bindings = build_discovery_capability_bindings(
        config,
        CapabilityCodec(ROOT),
        source_configuration_ids=frozenset({SOURCE_ID}),
    )

    assert [binding.source_configuration_id for binding in bindings] == [SOURCE_ID]


def test_newznab_discovery_validator_reuses_caps_probe():
    config = _config()
    bindings = build_discovery_capability_bindings(config, CapabilityCodec(ROOT))
    valid = ProviderStatus(Readiness.READY, Actionability.NONE)
    with patch(
        "comet.discovery.capabilities.NewznabAdapter.validate_config",
        new=AsyncMock(return_value=valid),
    ) as validate:
        outcome = asyncio.run(
            build_discovery_capability_validator(bindings, object())(
                bindings[0].binding
            )
        )

    assert outcome.state == "valid"
    validate.assert_awaited_once()


def test_discovery_validator_does_not_hide_provider_execution_failures():
    bindings = build_discovery_capability_bindings(
        _config(),
        CapabilityCodec(ROOT),
    )
    with patch(
        "comet.discovery.capabilities.NewznabAdapter.validate_config",
        new=AsyncMock(side_effect=ValueError("implementation failed")),
    ):
        validator = build_discovery_capability_validator(bindings, object())
        with pytest.raises(ValueError, match="implementation failed"):
            asyncio.run(validator(bindings[0].binding))


def test_newznab_validator_distinguishes_credentials_from_configuration():
    missing_credential = _config()
    missing_credential["discoverySources"][0]["options"] = {
        "endpoint": "https://indexer.example/api",
    }
    binding = build_discovery_capability_bindings(
        missing_credential, CapabilityCodec(ROOT)
    )[0]

    auth_outcome = asyncio.run(
        build_discovery_capability_validator((binding,), object())(binding.binding)
    )

    invalid_endpoint = _config()
    invalid_endpoint["discoverySources"][0]["kind"] = "newznab"
    invalid_endpoint["discoverySources"][0]["options"] = {
        "endpoint": "not-a-url",
        "apiKey": "secret",
    }
    binding = build_discovery_capability_bindings(
        invalid_endpoint, CapabilityCodec(ROOT)
    )[0]

    configuration_outcome = asyncio.run(
        build_discovery_capability_validator((binding,), object())(binding.binding)
    )

    assert (auth_outcome.state, auth_outcome.error_code) == (
        "auth_failed",
        "discovery_credentials_invalid",
    )
    assert (configuration_outcome.state, configuration_outcome.error_code) == (
        "plan_incompatible",
        "discovery_configuration_invalid",
    )


def test_easynews_discovery_validator_probes_the_search_codec():
    config = _config()
    config["accounts"] = {
        "easynews": {
            "username": "member",
            "password": "secret",
        }
    }
    config["discoverySources"][0] = {
        "configurationId": SOURCE_ID,
        "kind": "easynews",
        "enabled": True,
        "accountId": "easynews",
        "options": {},
    }
    bindings = build_discovery_capability_bindings(
        config,
        CapabilityCodec(ROOT),
    )
    valid = ProviderStatus(Readiness.READY, Actionability.NONE)
    with patch(
        "comet.discovery.capabilities.EasynewsSearchAdapter.validate_config",
        new=AsyncMock(return_value=valid),
    ) as validate:
        outcome = asyncio.run(
            build_discovery_capability_validator(bindings, object())(
                bindings[0].binding
            )
        )

    assert outcome.state == "valid"
    validate.assert_awaited_once_with()


def test_animetosho_discovery_validator_reuses_the_public_caps_probe():
    config = _config()
    config["discoverySources"][0] = {
        "configurationId": SOURCE_ID,
        "kind": "animetosho",
        "enabled": True,
        "options": {"maxResults": 100, "pageSize": 50},
    }
    bindings = build_discovery_capability_bindings(config, CapabilityCodec(ROOT))
    valid = ProviderStatus(Readiness.READY, Actionability.NONE)
    with patch(
        "comet.discovery.capabilities.AnimeToshoAdapter.validate_config",
        new=AsyncMock(return_value=valid),
    ) as validate:
        outcome = asyncio.run(
            build_discovery_capability_validator(bindings, object())(
                bindings[0].binding
            )
        )

    assert outcome.state == "valid"
    validate.assert_awaited_once()


def test_stremio_addon_validator_reuses_the_standard_manifest_probe():
    config = _config()
    config["discoverySources"][0] = {
        "configurationId": SOURCE_ID,
        "kind": "stremio_addon",
        "enabled": True,
        "options": {"baseUrl": "https://addon.example"},
    }
    bindings = build_discovery_capability_bindings(config, CapabilityCodec(ROOT))
    valid = ProviderStatus(Readiness.READY, Actionability.NONE)
    with patch(
        "comet.discovery.capabilities.StremioAddonAdapter.validate_config",
        new=AsyncMock(return_value=valid),
    ) as validate:
        outcome = asyncio.run(
            build_discovery_capability_validator(bindings, object())(
                bindings[0].binding
            )
        )

    assert outcome.state == "valid"
    validate.assert_awaited_once()


def test_discovery_preflight_projects_states_to_source_ids():
    bindings = build_discovery_capability_bindings(_config(), CapabilityCodec(ROOT))
    valid = EffectiveCapabilityState("valid", True, False, False)
    with patch(
        "comet.discovery.capabilities.CapabilityStateRepository.ensure_validated",
        new=AsyncMock(return_value={bindings[0].binding.binding_fingerprint: valid}),
    ) as ensure:
        states = asyncio.run(
            ensure_discovery_capability_states(
                _config(),
                CapabilityCodec(ROOT),
                object(),
                object(),
            )
        )

    assert states == {SOURCE_ID: valid}
    assert ensure.await_args.args[0] == [bindings[0].binding]


def test_runtime_failure_updates_only_the_exact_discovery_binding():
    config = _config()
    bindings = build_discovery_capability_bindings(
        config,
        CapabilityCodec(ROOT),
    )
    with patch(
        "comet.discovery.capabilities.CapabilityStateRepository.record_failure",
        new=AsyncMock(),
    ) as record:
        asyncio.run(
            record_discovery_capability_failure(
                config,
                CapabilityCodec(ROOT),
                object(),
                SOURCE_ID,
                state="auth_failed",
                error_code="credentials_rejected",
            )
        )

    binding = bindings[0].binding
    record.assert_awaited_once_with(
        binding,
        "auth_failed",
        error_code="credentials_rejected",
        retry_after=None,
    )


def test_public_branch_fingerprint_ignores_request_account_partition():
    config = _config()
    config["discoverySources"][0] = {
        "configurationId": SOURCE_ID,
        "kind": "animetosho",
        "enabled": True,
        "options": {"category": "anime"},
    }
    first = build_discovery_branch_fingerprints(
        config,
        CapabilityCodec(ROOT),
        account_partition=b"a" * 32,
    )[0]
    second = build_discovery_branch_fingerprints(
        config,
        CapabilityCodec(ROOT),
        account_partition=b"b" * 32,
    )[0]

    assert first.fingerprint == second.fingerprint
    assert first.public_visibility is True
    assert second.public_visibility is True


def test_unproven_credential_free_branch_remains_account_private():
    config = _config()
    config["discoverySources"][0] = {
        "configurationId": SOURCE_ID,
        "kind": "stremio_addon",
        "enabled": True,
        "options": {"baseUrl": "https://addon.example"},
    }
    first = build_discovery_branch_fingerprints(
        config,
        CapabilityCodec(ROOT),
        account_partition=b"a" * 32,
    )[0]
    second = build_discovery_branch_fingerprints(
        config,
        CapabilityCodec(ROOT),
        account_partition=b"b" * 32,
    )[0]

    assert first.public_visibility is False
    assert second.public_visibility is False
    assert first.fingerprint != second.fingerprint
