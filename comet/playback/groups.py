"""Pure in-memory grouping of conservatively equivalent releases."""

from collections import defaultdict
from dataclasses import dataclass

from comet.core.sources import ReleaseCandidate

_UNKNOWN_RESOLUTION = "unknown"


@dataclass(frozen=True, slots=True)
class PresentationGroup:
    resolution: str
    candidates: tuple[ReleaseCandidate, ...]

    def __post_init__(self):
        if not self.candidates:
            raise ValueError("presentation group requires a candidate")


def _normalized_text(value: str | None) -> str | None:
    if value is None:
        return None
    value = " ".join(value.casefold().split())
    return value or None


def _resolution(candidate: ReleaseCandidate) -> str:
    if candidate.parsed is None:
        return _UNKNOWN_RESOLUTION
    return _normalized_text(candidate.parsed.resolution) or _UNKNOWN_RESOLUTION


def _base_key(
    candidate: ReleaseCandidate,
    *,
    season_norm: int,
    episode_norm: int,
    daily_date: str | None,
) -> tuple[object, ...] | None:
    parsed = candidate.parsed
    if parsed is None or candidate.size is None:
        return None
    normalized_title = _normalized_text(parsed.normalized_title)
    release_group = _normalized_text(parsed.group)
    if normalized_title is None or release_group is None:
        return None
    return (
        candidate.media_id,
        candidate.scope.value,
        season_norm,
        episode_norm,
        daily_date,
        normalized_title,
        release_group,
        parsed.year,
        _normalized_text(parsed.edition),
        tuple(sorted(set(parsed.seasons))),
        tuple(sorted(set(parsed.episodes))),
        _normalized_text(parsed.date),
        candidate.size,
    )


def _singleton_key(
    candidate: ReleaseCandidate,
    *,
    season_norm: int,
    episode_norm: int,
    daily_date: str | None,
) -> tuple[object, ...]:
    return (
        "singleton",
        candidate.media_id,
        candidate.scope.value,
        season_norm,
        episode_norm,
        daily_date,
        candidate.candidate_id,
    )


def build_presentation_groups(
    candidates: tuple[ReleaseCandidate, ...],
    *,
    season_norm: int = -1,
    episode_norm: int = -1,
    daily_date: str | None = None,
) -> tuple[PresentationGroup, ...]:
    """Group equivalent releases without merging candidates or provider evidence."""
    if len({candidate.candidate_id for candidate in candidates}) != len(candidates):
        raise ValueError("presentation candidates must be unique")

    buckets: dict[tuple[object, ...], list[ReleaseCandidate]] = defaultdict(list)
    singleton_keys: dict[str, tuple[object, ...]] = {}
    for candidate in candidates:
        key = _base_key(
            candidate,
            season_norm=season_norm,
            episode_norm=episode_norm,
            daily_date=daily_date,
        )
        if key is None:
            singleton_keys[candidate.candidate_id] = _singleton_key(
                candidate,
                season_norm=season_norm,
                episode_norm=episode_norm,
                daily_date=daily_date,
            )
        else:
            buckets[key].append(candidate)

    assignments: dict[str, tuple[tuple[object, ...], str]] = {}
    for key, members in buckets.items():
        for candidate in members:
            assignments[candidate.candidate_id] = (key, _resolution(candidate))

    for candidate_id, key in singleton_keys.items():
        assignments[candidate_id] = (key, _UNKNOWN_RESOLUTION)

    grouped: dict[
        tuple[tuple[object, ...], str],
        list[ReleaseCandidate],
    ] = defaultdict(list)
    for candidate in candidates:
        grouped[assignments[candidate.candidate_id]].append(candidate)

    return tuple(
        PresentationGroup(group_key[1], tuple(members))
        for group_key, members in grouped.items()
    )


def limit_presentation_groups(
    groups: tuple[PresentationGroup, ...],
    max_releases_per_resolution: int,
) -> tuple[PresentationGroup, ...]:
    """Apply a resolution limit once per eligible group in ranked order."""
    if max_releases_per_resolution <= 0:
        return groups
    retained = []
    counts: dict[str, int] = {}
    for group in groups:
        count = counts.get(group.resolution, 0)
        if count >= max_releases_per_resolution:
            continue
        counts[group.resolution] = count + 1
        retained.append(group)
    return tuple(retained)
