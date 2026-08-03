"""Credential-free validation bindings for configured playback providers."""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field

import orjson

from comet.core.capability_states import (
    CapabilityBinding,
    CapabilityStateRepository,
    CapabilityValidationOutcome,
    EffectiveCapabilityState,
    deterministic_cbor,
)
from comet.core.credentials import (
    API_CREDENTIAL_KEYS,
    EASYNEWS_CREDENTIAL_KEYS,
    account_options,
)
from comet.core.sources import (
    CAPABILITY_VALIDATED_PLAYBACK_PROVIDER_KINDS,
    TORRENT_PROVIDER_KINDS,
    USENET_PLAYBACK_PROVIDER_KINDS,
)
from comet.playback.base import ProviderStatus, Readiness
from comet.playback.tokens import CapabilityCodec

_CREDENTIAL_KEYS = {
    "altmount": frozenset({"apiKey"}),
    "comet_native_usenet": frozenset({"nativeAccessToken", "password", "username"}),
    "easynews": frozenset(EASYNEWS_CREDENTIAL_KEYS),
    "nzbdav": frozenset({"sabApiKey", "webdavPassword", "webdavUsername"}),
    "stremio_nntp": frozenset({"password", "username"}),
    "stremthru_newz": frozenset({"authorization", "authToken"}),
    "torbox_usenet": frozenset(API_CREDENTIAL_KEYS),
    **{
        kind: frozenset({"apiKey"})
        for kind in CAPABILITY_VALIDATED_PLAYBACK_PROVIDER_KINDS
        if kind in TORRENT_PROVIDER_KINDS
    },
}


@dataclass(frozen=True, slots=True)
class PlaybackCapabilityBinding:
    provider_configuration_id: str
    binding: CapabilityBinding
    account_partition: bytes
    credential_fingerprint: str
    validation_options: Mapping[str, object] = field(repr=False)


def _provider_canonical_endpoint(
    provider_kind: str,
    normalized_options: object,
) -> str:
    digest = hashlib.sha256(
        deterministic_cbor([provider_kind, normalized_options])
    ).hexdigest()
    return f"{provider_kind}:configuration:{digest}"


def build_playback_capability_bindings(
    config: Mapping[str, object],
    codec: CapabilityCodec,
    *,
    instance_capability_versions: Mapping[str, str] | None = None,
    instance_credential_material: Mapping[str, bytes] | None = None,
    provider_configuration_ids: frozenset[str] | None = None,
) -> tuple[PlaybackCapabilityBinding, ...]:
    """Build enabled server-provider bindings while retaining credentials in memory."""
    if config.get("schemaVersion") != 2:
        return ()
    versions = instance_capability_versions or {}
    instance_credentials = instance_credential_material or {}
    raw_accounts = config.get("accounts")
    accounts = {} if raw_accounts is None else dict(raw_accounts.items())
    configured_transports = config.get("enabledTransports")
    enabled_transports = (
        set(configured_transports) if configured_transports is not None else None
    )
    bindings = []
    for entry in config.get("playbackProviders") or ():
        if not entry.get("enabled") or (
            entry.get("kind") not in CAPABILITY_VALIDATED_PLAYBACK_PROVIDER_KINDS
        ):
            continue
        configuration_id = entry["configurationId"]
        kind = entry["kind"]
        if (
            enabled_transports is not None
            and kind in USENET_PLAYBACK_PROVIDER_KINDS
            and "usenet" not in enabled_transports
        ) or (
            enabled_transports is not None
            and kind in TORRENT_PROVIDER_KINDS
            and "bittorrent" not in enabled_transports
        ):
            continue
        if (
            provider_configuration_ids is not None
            and configuration_id not in provider_configuration_ids
        ):
            continue
        options = resolve_capability_options(entry, accounts)
        if kind in TORRENT_PROVIDER_KINDS:
            debrid_binding = next(
                (
                    item
                    for item in config.get("_debridEntries") or ()
                    if item.get("configurationId") == configuration_id
                    and item.get("service") == kind
                ),
                None,
            )
            if debrid_binding is None:
                raise ValueError("debrid capability binding is unavailable")
            options = {
                "service": kind,
                "apiKey": debrid_binding.get("apiKey"),
            }
        credential_keys = _CREDENTIAL_KEYS[kind]
        normalized_options, credentials = separate_capability_credentials(
            options, credential_keys
        )
        if kind == "comet_native_usenet":
            credentials.append(
                (
                    "nativeAccessToken",
                    config.get("nativeAccessToken"),
                )
            )
        instance_material = instance_credentials.get(kind)
        if instance_material is not None:
            credentials.append(("instanceCredentialMaterial", instance_material.hex()))
        credentials.sort(key=lambda item: item[0])
        credential_material = orjson.dumps(credentials)
        credential_fingerprint = codec.provider_credential_fingerprint(
            kind,
            f"binding:{configuration_id}",
            credential_material,
        )
        validator_version = versions.get(kind, f"{kind}-v1")
        account_credential_fingerprint, account_partition = (
            codec.provider_account_partition(
                provider_kind=kind,
                canonical_endpoint=_provider_canonical_endpoint(
                    kind,
                    normalized_options,
                ),
                account_identity="credential-bound",
                typed_credential_payload=credentials,
                provider_config_version=[2, validator_version],
            )
        )
        fingerprint = codec.capability_binding_fingerprint(
            binding_kind=kind,
            schema_version=2,
            normalized_endpoint_and_behavior_options={
                "configuration_id": configuration_id,
                "options": normalized_options,
            },
            credential_fingerprint=credential_fingerprint,
            instance_capability_version=validator_version,
        )
        bindings.append(
            PlaybackCapabilityBinding(
                configuration_id,
                CapabilityBinding(
                    fingerprint,
                    kind,
                    2,
                    validator_version,
                ),
                account_partition,
                account_credential_fingerprint,
                options,
            )
        )
    return tuple(bindings)


def playback_provider_account_scopes(
    config: Mapping[str, object],
    codec: CapabilityCodec,
    *,
    instance_capability_versions: Mapping[str, str] | None = None,
    instance_credential_material: Mapping[str, bytes] | None = None,
    provider_configuration_ids: frozenset[str] | None = None,
) -> dict[str, tuple[bytes, str]]:
    """Return exact account partitions/fingerprints for enabled bindings."""
    return {
        binding.provider_configuration_id: (
            binding.account_partition,
            binding.credential_fingerprint,
        )
        for binding in build_playback_capability_bindings(
            config,
            codec,
            instance_capability_versions=instance_capability_versions,
            instance_credential_material=instance_credential_material,
            provider_configuration_ids=provider_configuration_ids,
        )
    }


async def ensure_playback_capability_states(
    config: Mapping[str, object],
    codec: CapabilityCodec,
    database,
    providers: Mapping[str, object],
    *,
    instance_capability_versions: Mapping[str, str] | None = None,
    instance_credential_material: Mapping[str, bytes] | None = None,
    provider_configuration_ids: frozenset[str] | None = None,
    force_retest: bool = False,
) -> dict[str, EffectiveCapabilityState]:
    """Preflight every enabled server playback binding before pure planning."""
    bindings = build_playback_capability_bindings(
        config,
        codec,
        instance_capability_versions=instance_capability_versions,
        instance_credential_material=instance_credential_material,
        provider_configuration_ids=provider_configuration_ids,
    )
    if not bindings:
        return {}
    repository = CapabilityStateRepository(database)
    validator = build_playback_capability_validator(bindings, providers)
    binding_values = [item.binding for item in bindings]
    states = await (
        repository.retest(binding_values, validator)
        if force_retest
        else repository.ensure_validated(binding_values, validator)
    )
    if not force_retest:
        repository.schedule_refresh(binding_values, validator, states)
    return {
        item.provider_configuration_id: states[item.binding.binding_fingerprint]
        for item in bindings
    }


async def record_playback_capability_failure(
    config: Mapping[str, object],
    codec: CapabilityCodec,
    database,
    provider_configuration_id: str,
    *,
    state: str,
    error_code: str,
    retry_after: int | None = None,
    instance_capability_versions: Mapping[str, str] | None = None,
    instance_credential_material: Mapping[str, bytes] | None = None,
) -> None:
    """Persist one runtime failure against its exact credential binding."""
    bindings = build_playback_capability_bindings(
        config,
        codec,
        instance_capability_versions=instance_capability_versions,
        instance_credential_material=instance_credential_material,
        provider_configuration_ids=frozenset({provider_configuration_id}),
    )
    if len(bindings) != 1:
        raise ValueError("playback capability binding is unavailable")
    binding = bindings[0]
    await CapabilityStateRepository(database).record_failure(
        binding.binding,
        state,
        error_code=error_code,
        retry_after=retry_after,
    )


def native_instance_credential_material(
    access_token: str | None,
    servers: object,
) -> bytes:
    """Canonically bind operator-owned native credentials without retaining them."""
    return orjson.dumps(
        {
            "access_token": access_token,
            "servers": servers,
        },
        option=orjson.OPT_SORT_KEYS,
    )


def provider_validation_outcome(
    status: ProviderStatus,
) -> CapabilityValidationOutcome:
    """Map provider-local results onto the closed persisted state machine."""
    if status.readiness is Readiness.RETRYABLE_FAILURE:
        return CapabilityValidationOutcome(
            "transiently_unreachable",
            status.code or "validation_unavailable",
            30,
        )
    if status.readiness is Readiness.TERMINAL_FAILURE:
        return CapabilityValidationOutcome(
            "auth_failed" if status.auth_failed else "plan_incompatible",
            status.code or "validation_failed",
        )
    return CapabilityValidationOutcome("valid")


def build_playback_capability_validator(
    bindings: tuple[PlaybackCapabilityBinding, ...],
    providers: Mapping[str, object],
):
    """Create one exact non-mutating validator over the in-memory provider graph."""
    by_fingerprint = {item.binding.binding_fingerprint: item for item in bindings}

    async def validate(binding: CapabilityBinding) -> CapabilityValidationOutcome:
        item = by_fingerprint[binding.binding_fingerprint]
        provider = providers.get(item.provider_configuration_id)
        if provider is None:
            return CapabilityValidationOutcome(
                "plan_incompatible",
                "provider_configuration_invalid",
            )
        status = await provider.validate_config(dict(item.validation_options))
        return provider_validation_outcome(status)

    return validate


def resolve_capability_options(
    entry: Mapping[str, object],
    accounts: Mapping[str, object],
) -> dict[str, object]:
    raw_options = entry.get("options")
    options = {} if raw_options is None else dict(raw_options.items())
    account_id = entry.get("accountId")
    if account_id is not None:
        for key, value in account_options(accounts.get(account_id)).items():
            options.setdefault(key, value)
    return options


def separate_capability_credentials(
    value: object,
    credential_keys: frozenset[str],
    *,
    path: str = "",
) -> tuple[object, list[tuple[str, object]]]:
    if isinstance(value, Mapping):
        normalized = {}
        credentials = []
        for key, nested in value.items():
            nested_path = f"{path}.{key}" if path else key
            if key in credential_keys:
                credentials.append((nested_path, nested))
                continue
            normalized_value, nested_credentials = separate_capability_credentials(
                nested,
                credential_keys,
                path=nested_path,
            )
            normalized[key] = normalized_value
            credentials.extend(nested_credentials)
        return normalized, credentials
    if isinstance(value, (list, tuple)):
        normalized_items = []
        credentials = []
        for index, nested in enumerate(value):
            normalized, nested_credentials = separate_capability_credentials(
                nested,
                credential_keys,
                path=f"{path}[{index}]",
            )
            normalized_items.append(normalized)
            credentials.extend(nested_credentials)
        return normalized_items, credentials
    return value, []
