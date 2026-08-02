"""Persisted capability bindings for configured Usenet discovery sources."""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field

import orjson

from comet.core.capability_bindings import (
    provider_validation_outcome,
    resolve_capability_options,
    separate_capability_credentials,
)
from comet.core.capability_states import (
    CapabilityBinding,
    CapabilityStateRepository,
    CapabilityValidationOutcome,
    EffectiveCapabilityState,
    deterministic_cbor,
)
from comet.core.discovery_sources import effective_discovery_sources
from comet.core.provider_governor import ProviderGovernor
from comet.discovery.adapters.animetosho import (
    AnimeToshoAdapter,
    animetosho_configuration,
)
from comet.discovery.adapters.easynews import (
    EasynewsSearchAccount,
    EasynewsSearchAdapter,
)
from comet.discovery.adapters.newznab import (
    NewznabAdapter,
    newznab_account_from_options,
)
from comet.discovery.adapters.stremio_addon import (
    StremioAddonAdapter,
    stremio_addon_configuration,
)
from comet.playback.tokens import CapabilityCodec

_MAX_CREDENTIAL_MATERIAL_BYTES = 64 * 1024
_CREDENTIAL_KEYS = {
    "animetosho": frozenset(),
    "easynews": frozenset({"password", "username"}),
    "newznab": frozenset({"apiKey"}),
    "nzbhydra2": frozenset({"apiKey"}),
    "prowlarr_usenet": frozenset({"apiKey"}),
    "stremio_addon": frozenset({"authorization"}),
}


class _DiscoveryCredentialError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DiscoveryCapabilityBinding:
    source_configuration_id: str
    source_kind: str
    binding: CapabilityBinding
    validation_options: Mapping[str, object] = field(repr=False)


@dataclass(frozen=True, slots=True)
class DiscoveryBranchFingerprint:
    source_configuration_id: str
    branch_family: str
    fingerprint: str
    public_visibility: bool = False

    def __post_init__(self):
        if (
            len(self.fingerprint) != 64
            or any(
                character not in "0123456789abcdef" for character in self.fingerprint
            )
            or not isinstance(self.public_visibility, bool)
        ):
            raise ValueError("discovery branch fingerprint is invalid")


def build_discovery_branch_fingerprints(
    config: Mapping[str, object],
    codec: CapabilityCodec,
    *,
    account_partition: bytes | None,
) -> tuple[DiscoveryBranchFingerprint, ...]:
    """Derive search identities independently from playback presentation."""
    if config.get("schemaVersion") != 2:
        return ()
    if account_partition is not None and (
        not isinstance(account_partition, bytes) or len(account_partition) != 32
    ):
        raise ValueError("discovery account partition is invalid")
    accounts = config.get("accounts")
    if accounts is None:
        accounts = {}
    elif not isinstance(accounts, Mapping):
        raise ValueError("discovery accounts are invalid")
    result = []
    for entry in effective_discovery_sources(config):
        if not isinstance(entry, Mapping) or not entry.get("enabled"):
            continue
        configuration_id = entry.get("configurationId")
        kind = entry.get("kind")
        if (
            not isinstance(configuration_id, str)
            or not isinstance(kind, str)
            or kind not in _CREDENTIAL_KEYS
        ):
            raise ValueError("discovery branch configuration is invalid")
        options = resolve_capability_options(entry, accounts)
        normalized_options, credentials = separate_capability_credentials(
            options,
            _CREDENTIAL_KEYS[kind],
        )
        credential_material = orjson.dumps(credentials)
        if len(credential_material) > _MAX_CREDENTIAL_MATERIAL_BYTES:
            raise ValueError("discovery credential material is too large")
        configuration_generation = codec.provider_credential_fingerprint(
            f"discovery_{kind}",
            f"binding:{configuration_id}",
            credential_material,
        )
        public_visibility = kind == "animetosho"
        specific_partition = None if public_visibility else account_partition
        caps_version = (
            f"discovery_{kind}-v2" if public_visibility else f"discovery_{kind}-v1"
        )
        payload = deterministic_cbor(
            [
                kind,
                configuration_generation,
                "usenet",
                normalized_options,
                caps_version,
                specific_partition,
            ]
        )
        result.append(
            DiscoveryBranchFingerprint(
                configuration_id,
                "usenet",
                hashlib.sha256(b"comet-discovery-branch-v1\0" + payload).hexdigest(),
                public_visibility,
            )
        )
    return tuple(result)


def build_discovery_capability_bindings(
    config: Mapping[str, object],
    codec: CapabilityCodec,
    *,
    instance_capability_versions: Mapping[str, str] | None = None,
    source_configuration_ids: frozenset[str] | None = None,
) -> tuple[DiscoveryCapabilityBinding, ...]:
    """Build bindings for enabled Usenet discovery sources."""
    if config.get("schemaVersion") != 2:
        return ()
    versions = instance_capability_versions or {}
    accounts = config.get("accounts")
    if accounts is None:
        accounts = {}
    elif not isinstance(accounts, Mapping):
        raise ValueError("discovery accounts are invalid")
    result = []
    for entry in effective_discovery_sources(config):
        if not isinstance(entry, Mapping) or not entry.get("enabled"):
            continue
        configuration_id = entry.get("configurationId")
        kind = entry.get("kind")
        if (
            not isinstance(configuration_id, str)
            or not isinstance(kind, str)
            or kind not in _CREDENTIAL_KEYS
        ):
            raise ValueError("capability discovery binding is invalid")
        if (
            source_configuration_ids is not None
            and configuration_id not in source_configuration_ids
        ):
            continue
        options = resolve_capability_options(entry, accounts)
        normalized_options, credentials = separate_capability_credentials(
            options,
            _CREDENTIAL_KEYS[kind],
        )
        credential_material = orjson.dumps(credentials)
        if len(credential_material) > _MAX_CREDENTIAL_MATERIAL_BYTES:
            raise ValueError("discovery credential material is too large")
        binding_kind = f"discovery_{kind}"
        validator_version = versions.get(kind, f"{binding_kind}-v1")
        credential_fingerprint = codec.provider_credential_fingerprint(
            binding_kind,
            f"binding:{configuration_id}",
            credential_material,
        )
        fingerprint = codec.capability_binding_fingerprint(
            binding_kind=binding_kind,
            schema_version=2,
            normalized_endpoint_and_behavior_options={
                "configuration_id": configuration_id,
                "options": normalized_options,
            },
            credential_fingerprint=credential_fingerprint,
            instance_capability_version=validator_version,
        )
        result.append(
            DiscoveryCapabilityBinding(
                configuration_id,
                kind,
                CapabilityBinding(
                    fingerprint,
                    binding_kind,
                    2,
                    validator_version,
                ),
                options,
            )
        )
    return tuple(result)


async def record_discovery_capability_failure(
    config: Mapping[str, object],
    codec: CapabilityCodec,
    database,
    source_configuration_id: str,
    *,
    state: str,
    error_code: str,
    retry_after: int | None = None,
) -> None:
    """Persist one runtime source failure against its exact credential binding."""
    bindings = build_discovery_capability_bindings(
        config,
        codec,
        source_configuration_ids=frozenset({source_configuration_id}),
    )
    if len(bindings) != 1:
        raise ValueError("discovery capability binding is unavailable")
    binding = bindings[0]
    await CapabilityStateRepository(database).record_failure(
        binding.binding,
        state,
        error_code=error_code,
        retry_after=retry_after,
    )


async def ensure_discovery_capability_states(
    config: Mapping[str, object],
    codec: CapabilityCodec,
    database,
    session,
    *,
    user_session=None,
    instance_capability_versions: Mapping[str, str] | None = None,
    source_configuration_ids: frozenset[str] | None = None,
    force_retest: bool = False,
) -> dict[str, EffectiveCapabilityState]:
    bindings = build_discovery_capability_bindings(
        config,
        codec,
        instance_capability_versions=instance_capability_versions,
        source_configuration_ids=source_configuration_ids,
    )
    if not bindings:
        return {}
    repository = CapabilityStateRepository(database)
    validator = build_discovery_capability_validator(
        bindings,
        session,
        database,
        user_session=user_session,
    )
    binding_values = [item.binding for item in bindings]
    states = await (
        repository.retest(binding_values, validator)
        if force_retest
        else repository.ensure_validated(binding_values, validator)
    )
    if not force_retest:
        repository.schedule_refresh(binding_values, validator, states)
    return {
        item.source_configuration_id: states[item.binding.binding_fingerprint]
        for item in bindings
    }


def build_discovery_capability_validator(
    bindings: tuple[DiscoveryCapabilityBinding, ...],
    session,
    database=None,
    *,
    user_session=None,
):
    user_session = session if user_session is None else user_session
    by_fingerprint = {item.binding.binding_fingerprint: item for item in bindings}

    async def validate(binding: CapabilityBinding) -> CapabilityValidationOutcome:
        item = by_fingerprint[binding.binding_fingerprint]
        options = item.validation_options
        governor = ProviderGovernor(database) if database is not None else None
        governor_scope = bytes.fromhex(item.binding.binding_fingerprint)
        try:
            if item.source_kind == "easynews":
                username = _text_credential(options, ("username",))
                password = _text_credential(options, ("password",))
                try:
                    account = EasynewsSearchAccount(
                        username,
                        password,
                        None,
                    )
                except ValueError as exc:
                    raise _DiscoveryCredentialError from exc
                provider = EasynewsSearchAdapter(
                    session,
                    account,
                    governor=governor,
                    governor_scope=governor_scope,
                )
            elif item.source_kind in {
                "newznab",
                "nzbhydra2",
                "prowlarr_usenet",
            }:
                _text_credential(options, ("apiKey",))
                account = newznab_account_from_options(
                    dict(options),
                    item.source_configuration_id,
                    label=item.source_kind,
                )
                provider = NewznabAdapter(
                    user_session,
                    account,
                    governor=governor,
                    governor_scope=governor_scope,
                )
            elif item.source_kind == "animetosho":
                provider = AnimeToshoAdapter(
                    session,
                    animetosho_configuration(
                        item.source_configuration_id,
                        dict(options),
                    ),
                    governor=governor,
                )
            elif item.source_kind == "stremio_addon":
                provider = StremioAddonAdapter(
                    stremio_addon_configuration(
                        item.source_configuration_id,
                        dict(options),
                    ),
                    governor=governor,
                    governor_scope=governor_scope,
                )
            else:
                raise RuntimeError(
                    f"unsupported discovery capability kind: {item.source_kind}"
                )
        except _DiscoveryCredentialError:
            return CapabilityValidationOutcome(
                "auth_failed",
                "discovery_credentials_invalid",
            )
        except ValueError:
            return CapabilityValidationOutcome(
                "plan_incompatible",
                "discovery_configuration_invalid",
            )
        status = await provider.validate_config()
        return provider_validation_outcome(status)

    return validate


def _text_credential(
    options: Mapping[str, object],
    names: tuple[str, ...],
) -> str:
    for name in names:
        value = options.get(name)
        if (
            isinstance(value, str)
            and value
            and len(value) <= 4096
            and not any(ord(character) < 32 for character in value)
        ):
            return value
    raise _DiscoveryCredentialError("discovery credentials are invalid")
