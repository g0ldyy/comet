"""Transport-neutral RTN ranking for every release family."""

from collections.abc import Iterable, Mapping

from RTN import ParsedData, check_fetch_and_rank_many
from RTN.extras import RESOLUTION_MAP, Resolution

from comet.core.sources import ReleaseCandidate


def _resolution_key(parsed: ParsedData) -> Resolution:
    return RESOLUTION_MAP.get(parsed.resolution.lower(), Resolution.UNKNOWN)


def rank_release_records(
    records: Mapping[str, Mapping[str, object]],
    rtn_settings,
    rtn_ranking,
    max_results_per_resolution: int,
    max_size: int,
    remove_trash: int,
) -> list[str]:
    """Apply RTN rank/filter rules without constructing torrent identifiers."""
    eligible = []
    for record_id, record in records.items():
        size = record["size"]
        if max_size and size is not None and size > max_size:
            continue
        parsed = record["parsed"]
        if parsed is not None:
            eligible.append((record_id, parsed))

    rank_results = check_fetch_and_rank_many(
        (parsed for _record_id, parsed in eligible),
        rtn_settings,
        rtn_ranking,
    )
    ranked = []
    for (record_id, parsed), (is_fetchable, _reasons, rank) in zip(
        eligible, rank_results, strict=True
    ):
        if remove_trash and not is_fetchable:
            continue
        ranked.append((_resolution_key(parsed), rank, record_id))

    ranked.sort(key=lambda item: (item[0].value, item[1], item[2]), reverse=True)
    if max_results_per_resolution <= 0:
        return [record_id for _resolution, _rank, record_id in ranked]

    selected = []
    per_resolution = {}
    for resolution, _rank, record_id in ranked:
        count = per_resolution.get(resolution, 0)
        if count >= max_results_per_resolution:
            continue
        per_resolution[resolution] = count + 1
        selected.append(record_id)
    return selected


def sort_candidates(
    candidates: Iterable[ReleaseCandidate],
    rtn_settings,
    rtn_ranking,
    max_results_per_resolution: int,
    max_size: int,
    remove_trash: int,
) -> tuple[ReleaseCandidate, ...]:
    """Rank one mixed transport-neutral batch with deterministic candidate ties."""
    ordered = tuple(candidates)
    by_id = {candidate.candidate_id: candidate for candidate in ordered}
    ranked_ids = rank_release_records(
        {
            candidate.candidate_id: {
                "parsed": candidate.parsed,
                "size": candidate.size,
            }
            for candidate in ordered
        },
        rtn_settings,
        rtn_ranking,
        max_results_per_resolution,
        max_size,
        remove_trash,
    )
    return tuple(by_id[candidate_id] for candidate_id in ranked_ids)
