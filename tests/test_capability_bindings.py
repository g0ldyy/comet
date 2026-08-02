import asyncio
import base64
from unittest.mock import AsyncMock, patch

import pytest

from comet.core.capability_bindings import (
    build_playback_capability_bindings,
    build_playback_capability_validator,
    ensure_playback_capability_states,
    provider_validation_outcome,
    record_playback_capability_failure,
)
from comet.core.capability_states import EffectiveCapabilityState
from comet.playback.base import Actionability, ProviderStatus, Readiness
from comet.playback.tokens import CapabilityCodec

ROOT = base64.urlsafe_b64encode(b"a" * 32).decode().rstrip("=")
PROVIDER_ID = "11111111-1111-4111-8111-111111111111"


def _easynews_config(password: str = "secret") -> dict:
    return {
        "schemaVersion": 2,
        "accounts": {
            "easy": {
                "username": "member",
                "password": password,
            }
        },
        "playbackProviders": [
            {
                "configurationId": PROVIDER_ID,
                "displayName": "Easynews",
                "kind": "easynews",
                "enabled": True,
                "accountId": "easy",
                "options": {"rangeRequired": True},
            }
        ],
    }


def test_binding_resolves_account_credentials_without_exposing_them_in_repr():
    result = build_playback_capability_bindings(
        _easynews_config(),
        CapabilityCodec(ROOT),
    )

    assert len(result) == 1
    assert result[0].validation_options == {
        "rangeRequired": True,
        "username": "member",
        "password": "secret",
    }
    assert "secret" not in repr(result[0])
    assert result[0].binding.binding_kind == "easynews"
    assert len(result[0].account_partition) == 32
    assert len(result[0].credential_fingerprint) == 64


def test_debrid_binding_is_scoped_by_stable_configuration_and_hides_credential():
    config = {
        "schemaVersion": 2,
        "enabledTransports": ["bittorrent"],
        "_debridEntries": [
            {
                "configurationId": PROVIDER_ID,
                "service": "realdebrid",
                "apiKey": "debrid-secret",
            }
        ],
        "playbackProviders": [
            {
                "configurationId": PROVIDER_ID,
                "displayName": "Living room",
                "kind": "realdebrid",
                "enabled": True,
            }
        ],
    }

    result = build_playback_capability_bindings(config, CapabilityCodec(ROOT))

    assert len(result) == 1
    assert result[0].binding.binding_kind == "realdebrid"
    assert result[0].validation_options == {
        "service": "realdebrid",
        "apiKey": "debrid-secret",
    }
    assert "debrid-secret" not in repr(result[0])


def test_binding_changes_only_for_relevant_options_credentials_and_versions():
    codec = CapabilityCodec(ROOT)
    baseline_config = _easynews_config()
    renamed = _easynews_config()
    renamed["playbackProviders"][0]["displayName"] = "Living room"
    changed_password = _easynews_config("different")
    changed_behavior = _easynews_config()
    changed_behavior["playbackProviders"][0]["options"]["rangeRequired"] = False

    baseline_binding = build_playback_capability_bindings(
        baseline_config,
        codec,
    )[0]
    baseline = baseline_binding.binding.binding_fingerprint
    renamed_binding = build_playback_capability_bindings(
        renamed,
        codec,
    )[0]
    renamed_fingerprint = renamed_binding.binding.binding_fingerprint
    password_binding = build_playback_capability_bindings(
        changed_password,
        codec,
    )[0]
    password_fingerprint = password_binding.binding.binding_fingerprint
    behavior_fingerprint = build_playback_capability_bindings(
        changed_behavior,
        codec,
    )[0].binding.binding_fingerprint
    version_fingerprint = build_playback_capability_bindings(
        baseline_config,
        codec,
        instance_capability_versions={"easynews": "easynews-v2"},
    )[0].binding.binding_fingerprint
    instance_fingerprint = build_playback_capability_bindings(
        baseline_config,
        codec,
        instance_credential_material={"easynews": b"instance-account-v2"},
    )[0].binding.binding_fingerprint

    assert baseline == renamed_fingerprint
    assert baseline_binding.account_partition == renamed_binding.account_partition
    assert baseline_binding.account_partition != password_binding.account_partition
    assert (
        len(
            {
                baseline,
                password_fingerprint,
                behavior_fingerprint,
                version_fingerprint,
                instance_fingerprint,
            }
        )
        == 5
    )


def test_nested_nntp_credentials_are_removed_from_behavior_but_change_identity():
    codec = CapabilityCodec(ROOT)
    config = {
        "schemaVersion": 2,
        "nativeAccessToken": "access",
        "playbackProviders": [
            {
                "configurationId": PROVIDER_ID,
                "displayName": "NNTP",
                "kind": "comet_native_usenet",
                "enabled": True,
                "options": {
                    "source": "personal_servers",
                    "servers": [
                        {
                            "host": "news.example",
                            "port": 563,
                            "username": "member",
                            "password": "first",
                        }
                    ],
                },
            }
        ],
    }
    changed = {
        **config,
        "playbackProviders": [
            {
                **config["playbackProviders"][0],
                "options": {
                    **config["playbackProviders"][0]["options"],
                    "servers": [
                        {
                            **config["playbackProviders"][0]["options"]["servers"][0],
                            "password": "second",
                        }
                    ],
                },
            }
        ],
    }

    first = build_playback_capability_bindings(config, codec)[0]
    second = build_playback_capability_bindings(changed, codec)[0]

    assert first.binding.binding_fingerprint != second.binding.binding_fingerprint
    assert "first" not in repr(first)


def test_account_partition_is_independent_of_option_key_order():
    codec = CapabilityCodec(ROOT)
    first = _easynews_config()
    first["playbackProviders"][0]["options"] = {
        "rangeRequired": True,
        "password": "secret",
        "username": "member",
    }
    first["playbackProviders"][0].pop("accountId")
    second = _easynews_config()
    second["playbackProviders"][0]["options"] = {
        "username": "member",
        "password": "secret",
        "rangeRequired": True,
    }
    second["playbackProviders"][0].pop("accountId")

    first_binding = build_playback_capability_bindings(first, codec)[0]
    second_binding = build_playback_capability_bindings(second, codec)[0]

    assert first_binding.account_partition == second_binding.account_partition
    assert first_binding.credential_fingerprint == second_binding.credential_fingerprint


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("accounts", [], "accounts"),
        ("options", [], "options"),
    ],
)
def test_malformed_binding_containers_are_not_replaced_with_empty_values(
    field,
    value,
    message,
):
    config = _easynews_config()
    if field == "accounts":
        config[field] = value
    else:
        config["playbackProviders"][0][field] = value

    with pytest.raises(ValueError, match=message):
        build_playback_capability_bindings(config, CapabilityCodec(ROOT))


def test_provider_status_maps_to_closed_persisted_states():
    assert (
        provider_validation_outcome(
            ProviderStatus(Readiness.READY, Actionability.CLIENT_ON_DEMAND)
        ).state
        == "valid"
    )
    transient = provider_validation_outcome(
        ProviderStatus(
            Readiness.RETRYABLE_FAILURE,
            Actionability.REMOTE_PREPARE,
            code="validation_unavailable",
        )
    )
    auth = provider_validation_outcome(
        ProviderStatus(
            Readiness.TERMINAL_FAILURE,
            Actionability.NONE,
            code="credentials_rejected",
            auth_failed=True,
        )
    )
    incompatible = provider_validation_outcome(
        ProviderStatus(
            Readiness.TERMINAL_FAILURE,
            Actionability.NONE,
            code="native_endpoint_unavailable",
        )
    )

    assert (transient.state, transient.retry_after) == (
        "transiently_unreachable",
        30,
    )
    assert auth.state == "auth_failed"
    assert incompatible.state == "plan_incompatible"
    for code in ("api_key_missing", "api_key_invalid", "account_suspended"):
        assert (
            provider_validation_outcome(
                ProviderStatus(
                    Readiness.TERMINAL_FAILURE,
                    Actionability.NONE,
                    code=code,
                    auth_failed=True,
                )
            ).state
            == "auth_failed"
        )


@pytest.mark.parametrize(
    ("kind", "options"),
    [
        (
            "stremio_nntp",
            {
                "servers": [
                    {
                        "host": "news.example",
                        "username": "member",
                        "password": "secret",
                    }
                ]
            },
        ),
        (
            "nzbdav",
            {
                "internalBaseUrl": "https://dav.example",
                "sabApiKey": "secret",
                "webdavUsername": "member",
                "webdavPassword": "secret",
            },
        ),
        (
            "altmount",
            {
                "internalBaseUrl": "https://mount.example",
                "apiKey": "secret",
            },
        ),
        (
            "stremthru_newz",
            {
                "baseUrl": "https://stremthru.example",
                "authToken": "member:secret",
            },
        ),
        ("torbox_usenet", {"apiKey": "secret"}),
    ],
)
def test_all_credential_shapes_produce_safe_bounded_bindings(kind, options):
    result = build_playback_capability_bindings(
        {
            "schemaVersion": 2,
            "playbackProviders": [
                {
                    "configurationId": PROVIDER_ID,
                    "displayName": kind,
                    "kind": kind,
                    "enabled": True,
                    "options": options,
                }
            ],
        },
        CapabilityCodec(ROOT),
    )

    assert len(result) == 1
    assert "secret" not in repr(result[0])


def test_unconsumed_credential_like_option_is_opaque():
    result = build_playback_capability_bindings(
        {
            "schemaVersion": 2,
            "playbackProviders": [
                {
                    "configurationId": PROVIDER_ID,
                    "displayName": "Easynews",
                    "kind": "easynews",
                    "enabled": True,
                    "options": {
                        "username": "member",
                        "password": "secret",
                        "bearerToken": "unexpected",
                    },
                }
            ],
        },
        CapabilityCodec(ROOT),
    )

    assert len(result) == 1
    assert "unexpected" not in repr(result[0])


def test_generated_validator_calls_only_the_exact_bound_provider():
    bindings = build_playback_capability_bindings(
        _easynews_config(),
        CapabilityCodec(ROOT),
    )
    provider = type("Provider", (), {})()
    provider.validate_config = AsyncMock(
        return_value=ProviderStatus(
            Readiness.READY,
            Actionability.SERVER_ON_DEMAND,
        )
    )
    validator = build_playback_capability_validator(
        bindings,
        {PROVIDER_ID: provider},
    )

    outcome = asyncio.run(validator(bindings[0].binding))

    assert outcome.state == "valid"
    provider.validate_config.assert_awaited_once_with(bindings[0].validation_options)


def test_generated_validator_reports_provider_construction_failure():
    bindings = build_playback_capability_bindings(
        _easynews_config(),
        CapabilityCodec(ROOT),
    )
    validator = build_playback_capability_validator(bindings, {})

    outcome = asyncio.run(validator(bindings[0].binding))

    assert outcome.state == "plan_incompatible"
    assert outcome.error_code == "provider_configuration_invalid"


def test_preflight_projects_fingerprint_states_back_to_configuration_ids():
    bindings = build_playback_capability_bindings(
        _easynews_config(),
        CapabilityCodec(ROOT),
    )
    valid = EffectiveCapabilityState("valid", True, False, False)

    with patch(
        "comet.core.capability_bindings.CapabilityStateRepository.ensure_validated",
        new=AsyncMock(return_value={bindings[0].binding.binding_fingerprint: valid}),
    ) as ensure:
        states = asyncio.run(
            ensure_playback_capability_states(
                _easynews_config(),
                CapabilityCodec(ROOT),
                object(),
                {},
            )
        )

    assert states == {PROVIDER_ID: valid}
    assert ensure.await_args.args[0] == [bindings[0].binding]


def test_runtime_failure_updates_only_the_exact_playback_binding():
    bindings = build_playback_capability_bindings(
        _easynews_config(),
        CapabilityCodec(ROOT),
    )
    with patch(
        "comet.core.capability_bindings.CapabilityStateRepository.record_failure",
        new=AsyncMock(),
    ) as record:
        asyncio.run(
            record_playback_capability_failure(
                _easynews_config(),
                CapabilityCodec(ROOT),
                object(),
                PROVIDER_ID,
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


def test_native_runtime_failure_keeps_the_instance_credential_generation():
    config = {
        "schemaVersion": 2,
        "nativeAccessToken": "access",
        "playbackProviders": [
            {
                "configurationId": PROVIDER_ID,
                "displayName": "Native",
                "kind": "comet_native_usenet",
                "enabled": True,
                "options": {"source": "instance_pool"},
            }
        ],
    }
    instance_material = {"comet_native_usenet": b"instance-generation"}
    binding = build_playback_capability_bindings(
        config,
        CapabilityCodec(ROOT),
        instance_credential_material=instance_material,
    )[0].binding

    with patch(
        "comet.core.capability_bindings.CapabilityStateRepository.record_failure",
        new=AsyncMock(),
    ) as record:
        asyncio.run(
            record_playback_capability_failure(
                config,
                CapabilityCodec(ROOT),
                object(),
                PROVIDER_ID,
                state="transiently_unreachable",
                error_code="native_engine_unavailable",
                retry_after=30,
                instance_credential_material=instance_material,
            )
        )

    record.assert_awaited_once_with(
        binding,
        "transiently_unreachable",
        error_code="native_engine_unavailable",
        retry_after=30,
    )
