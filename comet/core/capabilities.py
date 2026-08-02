"""Pure per-request eligibility planning for discovery and playback."""

from collections.abc import Mapping
from dataclasses import dataclass

from comet.core.capability_states import EffectiveCapabilityState
from comet.core.discovery_sources import effective_discovery_sources
from comet.core.sources import (
    CAPABILITY_VALIDATED_PLAYBACK_PROVIDER_KINDS,
    LOCATOR_PROVIDER_KINDS,
    REAL_NZB_PROVIDER_KINDS,
    TORRENT_PROVIDER_KINDS,
    USENET_PLAYBACK_PROVIDER_KINDS,
    Locator,
    TransportKind,
)
from comet.usenet.access import NativeAccessAuthorizer

_REAL_NZB_DISCOVERY_KINDS = {
    "newznab",
    "nzbhydra2",
    "prowlarr_usenet",
    "animetosho",
    "stremio_addon",
}


@dataclass(frozen=True, slots=True)
class EligibleProvider:
    configuration_id: str
    kind: str
    list_position: int
    degraded: bool = False


@dataclass(frozen=True, slots=True)
class EligibleDiscovery:
    configuration_id: str
    branches: frozenset[TransportKind]
    degraded: bool = False
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class CapabilityStateSnapshot:
    providers: Mapping[str, EffectiveCapabilityState]
    discovery: Mapping[str, EffectiveCapabilityState]


@dataclass(frozen=True, slots=True)
class CapabilityPlan:
    transports: frozenset[TransportKind]
    discovery_source_ids: tuple[str, ...]
    providers: tuple[EligibleProvider, ...]
    diagnostics: tuple[str, ...]
    discovery: tuple[EligibleDiscovery, ...] = ()

    def branches_for(self, configuration_id: str) -> frozenset[TransportKind]:
        for source in self.discovery:
            if source.configuration_id == configuration_id:
                return source.branches
        return frozenset()

    def compatible_providers(self, locator: Locator) -> tuple[EligibleProvider, ...]:
        accepted = (
            LOCATOR_PROVIDER_KINDS[locator.kind] & locator.policy.allowed_provider_kinds
        )
        return tuple(
            provider
            for provider in self.providers
            if provider.kind in accepted
            and (
                locator.policy.exact_provider_configuration_id is None
                or provider.configuration_id
                == locator.policy.exact_provider_configuration_id
            )
        )


class CapabilityPlanner:
    """Builds an immutable plan without cache, network, or provider side effects."""

    def __init__(
        self,
        *,
        usenet_offered: bool,
        native_authorizer: NativeAccessAuthorizer,
        native_engine_enabled: bool = False,
        native_instance_pool_available: bool = False,
        native_user_servers_allowed: bool = False,
    ):
        self._usenet_offered = usenet_offered
        self._native_authorizer = native_authorizer
        self._native_engine_enabled = native_engine_enabled
        self._native_instance_pool_available = native_instance_pool_available
        self._native_user_servers_allowed = native_user_servers_allowed

    def build(
        self,
        config: dict,
        capability_states: CapabilityStateSnapshot | None = None,
    ) -> CapabilityPlan:
        if config["schemaVersion"] != 2:
            return self._legacy_plan(config)
        transports = frozenset(
            TransportKind(value) for value in config["enabledTransports"]
        )
        diagnostics = []
        if TransportKind.USENET in transports and not self._usenet_offered:
            transports = frozenset(
                transport
                for transport in transports
                if transport != TransportKind.USENET
            )
            diagnostics.append("Usenet is not offered by this instance")

        providers = []
        for position, provider in enumerate(config.get("playbackProviders") or []):
            if not provider["enabled"]:
                continue
            kind = provider["kind"]
            if (
                kind in USENET_PLAYBACK_PROVIDER_KINDS
                and TransportKind.USENET not in transports
            ):
                continue
            if (
                kind in TORRENT_PROVIDER_KINDS
                and TransportKind.BITTORRENT not in transports
            ):
                continue
            if kind == "comet_native_usenet":
                if not self._native_authorizer.authorized(
                    config.get("nativeAccessToken")
                ):
                    diagnostics.append(
                        f"{provider['displayName']}: native access is unavailable"
                    )
                    continue
                source = provider["options"]["source"]
                if not self._native_engine_enabled:
                    diagnostics.append(
                        f"{provider['displayName']}: native engine is unavailable"
                    )
                    continue
                if (
                    source == "instance_pool"
                    and not self._native_instance_pool_available
                ):
                    diagnostics.append(
                        f"{provider['displayName']}: instance NNTP servers are unavailable"
                    )
                    continue
                if (
                    source == "personal_servers"
                    and not self._native_user_servers_allowed
                ):
                    diagnostics.append(
                        f"{provider['displayName']}: personal NNTP servers are unavailable"
                    )
                    continue
            state = None
            if (
                capability_states is not None
                and kind in CAPABILITY_VALIDATED_PLAYBACK_PROVIDER_KINDS
            ):
                state = capability_states.providers.get(provider["configurationId"])
                if not _state_is_eligible(state, provider["displayName"], diagnostics):
                    continue
            providers.append(
                EligibleProvider(
                    provider["configurationId"],
                    kind,
                    position,
                    state.degraded if state is not None else False,
                )
            )

        provider_kinds = {provider.kind for provider in providers}
        provider_config_ids = {provider.configuration_id for provider in providers}
        eligible_provider_entries = {
            entry["configurationId"]: entry
            for entry in config.get("playbackProviders") or ()
            if entry["configurationId"] in provider_config_ids
        }
        sources = []
        discovery = []
        for source in effective_discovery_sources(config):
            if not source["enabled"]:
                continue
            if TransportKind.USENET not in transports:
                continue
            source_kind = source["kind"]
            source_label = source.get("displayName") or source_kind
            if source_kind == "easynews":
                account_id = source.get("accountId")
                direct_reachable = any(
                    provider.kind == "easynews"
                    and (
                        account_id is None
                        or eligible_provider_entries[provider.configuration_id].get(
                            "accountId"
                        )
                        == account_id
                    )
                    for provider in providers
                )
                generated_reachable = bool(provider_kinds & REAL_NZB_PROVIDER_KINDS)
                if not direct_reachable and not generated_reachable:
                    continue
            elif (
                source_kind in _REAL_NZB_DISCOVERY_KINDS
                and not provider_kinds & REAL_NZB_PROVIDER_KINDS
            ):
                continue
            state = None
            if capability_states is not None:
                state = capability_states.discovery.get(source["configurationId"])
                if not _state_is_eligible(state, source_label, diagnostics):
                    continue
            sources.append(source["configurationId"])
            discovery.append(
                EligibleDiscovery(
                    source["configurationId"],
                    frozenset({TransportKind.USENET}),
                    state.degraded if state is not None else False,
                    source.get("displayName"),
                )
            )
        return CapabilityPlan(
            transports,
            tuple(sources),
            tuple(providers),
            tuple(diagnostics),
            tuple(discovery),
        )

    def _legacy_plan(self, config: dict) -> CapabilityPlan:
        providers = []
        for position, entry in enumerate(config.get("_debridEntries", [])):
            providers.append(
                EligibleProvider(entry["service"], entry["service"], position)
            )
        if config.get("_enableTorrent"):
            providers.append(
                EligibleProvider("direct_torrent", "direct_torrent", len(providers))
            )
        return CapabilityPlan(
            frozenset({TransportKind.BITTORRENT}), (), tuple(providers), ()
        )


def _state_is_eligible(
    state: EffectiveCapabilityState | None,
    label: str,
    diagnostics: list[str],
) -> bool:
    if state is None or state.state == "pending_validation":
        diagnostics.append(f"{label}: validation required")
        return False
    if not state.eligible:
        messages = {
            "auth_failed": "authentication failed",
            "plan_incompatible": "current plan is incompatible",
        }
        diagnostics.append(f"{label}: {messages[state.state]}")
        return False
    if state.degraded:
        diagnostics.append(
            f"{label}: temporarily unreachable; using last validated capabilities"
        )
    return True
