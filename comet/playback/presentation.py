"""Expand ranked releases into provider-pinned playback options."""

import uuid
from dataclasses import dataclass

from comet.core.capabilities import CapabilityPlan, EligibleProvider
from comet.core.sources import (
    TORRENT_PROVIDER_KINDS,
    Locator,
    ReleaseCandidate,
)
from comet.playback.groups import (
    build_presentation_groups,
    limit_presentation_groups,
)
from comet.playback.repository import RenderedCandidateIds
from comet.playback.tokens import MAX_NZB_HANDOFF_LOCATORS, CapabilityCodec


@dataclass(frozen=True, slots=True)
class ProviderOption:
    candidate_id: str
    provider: EligibleProvider
    locators: tuple[Locator, ...]
    cached: bool = False


def build_provider_options(
    candidates: tuple[ReleaseCandidate, ...], capability_plan: CapabilityPlan
) -> tuple[ProviderOption, ...]:
    """Aggregate compatible locators once for each configured provider binding."""
    options = []
    for candidate in candidates:
        locators_by_provider: dict[str, list[Locator]] = {}
        providers_by_id: dict[str, EligibleProvider] = {}
        for locator in candidate.locators:
            for provider in capability_plan.compatible_providers(locator):
                providers_by_id[provider.configuration_id] = provider
                locators_by_provider.setdefault(provider.configuration_id, []).append(
                    locator
                )
        candidate_options = []
        for provider_id, locators in locators_by_provider.items():
            provider = providers_by_id[provider_id]
            ordered = tuple(
                sorted(locators, key=lambda locator: locator.locator_id)[:16]
            )
            candidate_options.append(
                ProviderOption(
                    candidate.candidate_id,
                    provider,
                    ordered,
                )
            )
        options.extend(
            sorted(
                candidate_options,
                key=lambda option: (
                    option.provider.list_position,
                    option.provider.configuration_id,
                ),
            )
        )
    return tuple(options)


def select_presentation(
    candidates: tuple[ReleaseCandidate, ...],
    options: tuple[ProviderOption, ...],
    *,
    cached_only: bool,
    max_releases_per_resolution: int,
    season_norm: int = -1,
    episode_norm: int = -1,
    daily_date: str | None = None,
) -> tuple[tuple[ReleaseCandidate, ...], tuple[ProviderOption, ...]]:
    """Build the sole deterministic provider-expanded presentation.

    Release rank always dominates provider delivery details. Confirmed debrid
    cache hits are preferred only among equivalent releases; no preparation
    history participates in visibility or ordering.
    """
    candidate_order = {
        candidate.candidate_id: index for index, candidate in enumerate(candidates)
    }
    eligible = [
        option
        for option in options
        if option.candidate_id in candidate_order
        and not (
            cached_only
            and option.provider.kind in TORRENT_PROVIDER_KINDS
            and option.provider.kind != "direct_torrent"
            and not option.cached
        )
    ]

    candidate_options = {option.candidate_id for option in eligible}
    eligible_candidates = tuple(
        candidate
        for candidate in candidates
        if candidate.candidate_id in candidate_options
    )
    groups = build_presentation_groups(
        eligible_candidates,
        season_norm=season_norm,
        episode_norm=episode_norm,
        daily_date=daily_date,
    )
    retained_groups = limit_presentation_groups(groups, max_releases_per_resolution)
    retained_candidate_ids = {
        candidate.candidate_id
        for group in retained_groups
        for candidate in group.candidates
    }
    eligible = [
        option for option in eligible if option.candidate_id in retained_candidate_ids
    ]
    group_order = {
        candidate.candidate_id: group_index
        for group_index, group in enumerate(retained_groups)
        for candidate in group.candidates
    }
    eligible.sort(
        key=lambda option: (
            group_order[option.candidate_id],
            0 if option.cached else 1,
            option.provider.list_position,
            candidate_order[option.candidate_id],
            option.provider.configuration_id,
        ),
    )
    visible_candidate_ids = {option.candidate_id for option in eligible}
    visible_candidates = tuple(
        candidate
        for candidate in candidates
        if candidate.candidate_id in visible_candidate_ids
    )
    return visible_candidates, tuple(eligible)


def issue_provider_option_capability(
    codec: CapabilityCodec,
    *,
    partition: bytes,
    option: ProviderOption,
    persisted: RenderedCandidateIds,
    selection_intent: list,
    client: str,
) -> str:
    """Create a short-lived server playback capability from committed IDs."""
    candidate_id = uuid.UUID(persisted.candidate_id).bytes
    provider_id = uuid.UUID(option.provider.configuration_id).bytes
    locator_ids = [
        uuid.UUID(persisted.locator_ids[locator.locator_id]).bytes
        for locator in option.locators
    ]
    return codec.encode(
        "pi2",
        partition=partition,
        suffix=[candidate_id, provider_id, locator_ids, selection_intent, client],
        ttl=15 * 60,
    )


def issue_nzb_handoff_capability(
    codec: CapabilityCodec,
    *,
    partition: bytes,
    option: ProviderOption,
    persisted: RenderedCandidateIds,
    selection_intent: list,
    ttl: int,
) -> str:
    """Create one reusable lazy handoff from committed NZB transforms."""
    locators = option.locators[:MAX_NZB_HANDOFF_LOCATORS]
    suffix = [
        uuid.UUID(persisted.candidate_id).bytes,
        uuid.UUID(option.provider.configuration_id).bytes,
        [
            uuid.UUID(persisted.locator_ids[locator.locator_id]).bytes
            for locator in locators
        ],
        selection_intent,
        "stremio",
    ]
    return codec.encode(
        "ni2",
        partition=partition,
        suffix=suffix,
        ttl=ttl,
    )
