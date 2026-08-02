"""Transport-neutral persistence and reads for public torrent releases."""

import hashlib
import re
from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from comet.core.database import (
    build_json_list_membership_predicate,
    encode_json_param,
)
from comet.core.locator_codec import locator_from_json, parsed_json
from comet.core.sources import (
    MAX_SIGNED_BIGINT,
    TORRENT_PROVIDER_KINDS,
    LocatorKind,
    LocatorPolicy,
    ReleaseCandidate,
    ReleaseScope,
    TorrentLocator,
    TransportKind,
)
from comet.discovery.models import MediaQuery
from comet.discovery.repository import (
    ReleaseDiscoveryRepository,
    decode_candidate_attributes,
)
from comet.utils.parsing import MediaScope, load_cached_parsed, load_cached_string_list

ACCOUNT_TRACKER_PREFIX = "DebridAccount|"
_INFO_HASH = re.compile(r"[0-9a-f]{40}")
_CONTROL = re.compile(r"[\x00-\x1f]")
_PUBLIC_PARTITION = "0" * 64
_IDENTITY_MEMBERSHIP_SQL = build_json_list_membership_predicate(
    "identity_value",
    "info_hashes",
)
_CANDIDATE_MEMBERSHIP_SQL = build_json_list_membership_predicate(
    "candidate_id",
    "candidate_ids",
)
_EXISTENCE_CHUNK = 4_096
_GC_CHUNK = 5_000


def is_legacy_account_torrent(row: Any) -> bool:
    tracker = row["tracker"]
    return isinstance(tracker, str) and tracker.startswith(ACCOUNT_TRACKER_PREFIX)


def _scope(row: Any, parsed: object | None) -> str:
    if row["episode_norm"] >= 0:
        return "episode"
    if row["season_norm"] >= 0:
        return "season_pack"
    if getattr(parsed, "seasons", None) or getattr(parsed, "episodes", None):
        return "series_pack"
    return "movie"


def _legacy_title(value: object, info_hash: str) -> str:
    if not isinstance(value, str):
        return info_hash
    title = _CONTROL.sub(" ", value).strip()
    return title[:1_024] or info_hash


def _legacy_source(value: object) -> str:
    if not isinstance(value, str) or not value:
        return "Torrent cache"
    source = _CONTROL.sub(" ", value).strip()
    return source[:128] or "Torrent cache"


def _parsed(value: object) -> object | None:
    parsed = load_cached_parsed(value)
    if parsed is None:
        return None
    parsed_json(parsed)
    return parsed


def _validated_row(row: Any) -> dict[str, object]:
    media_id = row["media_id"]
    info_hash = row["info_hash"]
    if (
        not isinstance(media_id, str)
        or not 1 <= len(media_id) <= 128
        or _CONTROL.search(media_id)
    ):
        raise RuntimeError("torrent media identifier is invalid")
    if not isinstance(info_hash, str) or not _INFO_HASH.fullmatch(info_hash.lower()):
        raise RuntimeError("torrent info hash is invalid")
    if is_legacy_account_torrent(row):
        raise RuntimeError("account-derived torrent cannot use the public repository")
    info_hash = info_hash.lower()
    parsed = _parsed(row["parsed_json"])
    scope = _scope(row, parsed)
    season_norm = row["season_norm"]
    episode_norm = row["episode_norm"]
    if (
        isinstance(season_norm, bool)
        or not isinstance(season_norm, int)
        or season_norm < -1
        or isinstance(episode_norm, bool)
        or not isinstance(episode_norm, int)
        or episode_norm < -1
    ):
        raise RuntimeError("torrent scope is invalid")
    title = _legacy_title(row["title"], info_hash)
    size = row["size"]
    if type(size) is not int or not 1 <= size <= MAX_SIGNED_BIGINT:
        size = None
    file_index = row["file_index"]
    if type(file_index) is not int or file_index < 0:
        file_index = None
    return {
        "row": row,
        "media_id": media_id,
        "info_hash": info_hash,
        "parsed": parsed,
        "parsed_json": parsed_json(parsed),
        "scope": scope,
        "season_norm": season_norm,
        "episode_norm": episode_norm,
        "file_index": file_index,
        "title": title,
        "size": size,
        "source": _legacy_source(row["tracker"]),
        "tracker_sources": load_cached_string_list(row["sources_json"]),
    }


def _candidate_from_rows(
    rows: list[dict[str, object]],
) -> tuple[MediaQuery, ReleaseCandidate]:
    media_id = rows[0]["media_id"]
    info_hash = rows[0]["info_hash"]
    is_series = any(row["scope"] != "movie" for row in rows)
    scope = "series_pack" if is_series else "movie"
    query = MediaQuery(
        media_id,
        "series" if is_series else "movie",
        search_scope=scope,
    )
    ordered = sorted(
        rows,
        key=lambda row: (
            row["season_norm"],
            row["episode_norm"],
            -1 if row["file_index"] is None else row["file_index"],
            row["title"],
        ),
    )
    representative = max(
        ordered,
        key=lambda row: (
            row["season_norm"] == -1 and row["episode_norm"] == -1,
            -1 if row["size"] is None else row["size"],
            row["title"],
        ),
    )
    locators = []
    for row in ordered:
        locator_key = hashlib.sha256(
            (
                f"{media_id}\0{info_hash}\0{row['season_norm']}\0"
                f"{row['episode_norm']}\0{row['file_index']}\0{row['title']}"
            ).encode()
        ).hexdigest()
        locators.append(
            TorrentLocator(
                locator_id=f"lc1:{locator_key}",
                kind=LocatorKind.TORRENT,
                policy=LocatorPolicy(TORRENT_PROVIDER_KINDS),
                info_hash=info_hash,
                file_index=row["file_index"],
                season_norm=row["season_norm"],
                episode_norm=row["episode_norm"],
                selection_title=row["title"],
                selection_size=row["size"],
                selection_parsed_json=row["parsed_json"],
            )
        )
    seeders = [
        row["row"]["seeders"]
        for row in rows
        if type(row["row"]["seeders"]) is int and row["row"]["seeders"] >= 0
    ]
    stats = {"seeders": max(seeders)} if seeders else {}
    tracker_sources = tuple(
        dict.fromkeys(source for row in rows for source in row["tracker_sources"])
    )
    if tracker_sources:
        stats["tracker_sources"] = tracker_sources
    return (
        query,
        ReleaseCandidate(
            candidate_id=f"btih:{info_hash}",
            media_id=media_id,
            scope=query.scope,
            transport=TransportKind.BITTORRENT,
            title=representative["title"],
            locators=tuple(locators),
            size=representative["size"],
            source=representative["source"],
            parsed=representative["parsed"],
            transport_stats=stats,
            identities=(f"btih:{info_hash}",),
        ),
    )


def torrent_candidates_from_rows(
    raw_rows: list[Any],
) -> tuple[tuple[MediaQuery, ReleaseCandidate], ...]:
    grouped = _group_validated_rows(raw_rows)
    return tuple(_candidate_from_rows(rows) for rows in grouped.values())


def torrent_candidate_from_runtime(
    info_hash: str,
    torrent: Mapping[str, object],
    *,
    media_id: str,
    scope: ReleaseScope,
    season_norm: int,
    episode_norm: int,
) -> ReleaseCandidate:
    """Project one already-filtered torrent into the shared immutable model."""
    required = {"parsed", "title", "size", "fileIndex", "seeders", "tracker"}
    if not isinstance(torrent, Mapping) or not required <= torrent.keys():
        raise ValueError("runtime torrent is invalid")
    title = torrent["title"]
    size = torrent["size"]
    file_index = torrent["fileIndex"]
    seeders = torrent["seeders"]
    tracker = torrent["tracker"]
    if (
        not isinstance(info_hash, str)
        or _INFO_HASH.fullmatch(info_hash.lower()) is None
        or not isinstance(title, str)
        or not 1 <= len(title) <= 1_024
        or _CONTROL.search(title)
        or (
            size is not None
            and (type(size) is not int or not 1 <= size <= MAX_SIGNED_BIGINT)
        )
        or (file_index is not None and (type(file_index) is not int or file_index < 0))
        or (seeders is not None and (type(seeders) is not int or seeders < 0))
        or not isinstance(tracker, str)
    ):
        raise ValueError("runtime torrent is invalid")
    info_hash = info_hash.lower()
    parsed = torrent["parsed"]
    encoded_parsed = parsed_json(parsed)
    if (
        type(season_norm) is not int
        or season_norm < -1
        or type(episode_norm) is not int
        or episode_norm < -1
    ):
        raise ValueError("runtime torrent scope is invalid")
    locator_key = hashlib.sha256(
        (
            f"{media_id}\0{info_hash}\0{season_norm}\0{episode_norm}\0"
            f"{file_index}\0{title}"
        ).encode()
    ).hexdigest()
    transport_stats = {} if seeders is None else {"seeders": seeders}
    return ReleaseCandidate(
        candidate_id=f"btih:{info_hash}",
        media_id=media_id,
        scope=scope,
        transport=TransportKind.BITTORRENT,
        title=title,
        locators=(
            TorrentLocator(
                locator_id=f"lc1:{locator_key}",
                kind=LocatorKind.TORRENT,
                policy=LocatorPolicy(TORRENT_PROVIDER_KINDS),
                info_hash=info_hash,
                file_index=file_index,
                season_norm=season_norm,
                episode_norm=episode_norm,
                selection_title=title,
                selection_size=size,
                selection_parsed_json=encoded_parsed,
            ),
        ),
        size=size,
        source=tracker,
        parsed=parsed,
        transport_stats=transport_stats,
        identities=(f"btih:{info_hash}",),
    )


def torrent_candidate_from_scrape_result(
    torrent: Mapping[str, object],
    query: MediaQuery,
) -> ReleaseCandidate:
    """Translate one legacy torrent adapter result at the discovery boundary."""
    if not isinstance(torrent, Mapping):
        raise ValueError("torrent discovery result is invalid")
    required = {
        "title",
        "infoHash",
        "fileIndex",
        "seeders",
        "size",
        "tracker",
        "sources",
    }
    if not required <= torrent.keys():
        raise ValueError("torrent discovery result is incomplete")
    info_hash = torrent["infoHash"]
    title = torrent["title"]
    file_index = torrent["fileIndex"]
    seeders = torrent["seeders"]
    size = torrent["size"]
    tracker = torrent["tracker"]
    sources = torrent["sources"]
    if (
        not isinstance(info_hash, str)
        or _INFO_HASH.fullmatch(info_hash.lower()) is None
        or not isinstance(title, str)
        or not title
        or len(title) > 1_024
        or _CONTROL.search(title)
        or (file_index is not None and (type(file_index) is not int or file_index < 0))
        or (seeders is not None and (type(seeders) is not int or seeders < 0))
        or (
            size is not None
            and (type(size) is not int or not 1 <= size <= MAX_SIGNED_BIGINT)
        )
        or not isinstance(tracker, str)
        or not isinstance(sources, list)
        or not all(isinstance(source, str) for source in sources)
    ):
        raise ValueError("torrent discovery result is invalid")
    info_hash = info_hash.lower()
    season_norm = query.season if query.season is not None else -1
    episode_norm = query.episode if query.episode is not None else -1
    locator_key = hashlib.sha256(
        (
            f"{query.media_id}\0{info_hash}\0{season_norm}\0{episode_norm}\0"
            f"{file_index}\0{title}"
        ).encode()
    ).hexdigest()
    stats: dict[str, object] = {}
    if seeders is not None:
        stats["seeders"] = seeders
    if sources:
        stats["tracker_sources"] = tuple(sources)
    return ReleaseCandidate(
        candidate_id=f"btih:{info_hash}",
        media_id=query.media_id,
        scope=query.scope,
        transport=TransportKind.BITTORRENT,
        title=title,
        locators=(
            TorrentLocator(
                locator_id=f"lc1:{locator_key}",
                kind=LocatorKind.TORRENT,
                policy=LocatorPolicy(TORRENT_PROVIDER_KINDS),
                info_hash=info_hash,
                file_index=file_index,
                season_norm=season_norm,
                episode_norm=episode_norm,
                selection_title=title,
                selection_size=size,
                selection_parsed_json="{}",
            ),
        ),
        size=size,
        source=tracker,
        transport_stats=stats,
        identities=(f"btih:{info_hash}",),
    )


def _group_validated_rows(
    raw_rows: list[Any],
) -> dict[tuple[str, str], list[dict[str, object]]]:
    grouped: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for raw_row in raw_rows:
        row = _validated_row(raw_row)
        grouped[(row["media_id"], row["info_hash"])].append(row)
    return grouped


class TorrentReleaseRepository:
    def __init__(self, database):
        self._database = database

    async def persist_rows(self, rows: list[Any]) -> int:
        grouped: dict[MediaQuery, list[ReleaseCandidate]] = defaultdict(list)
        observed_at: dict[MediaQuery, float] = {}
        validated_groups = _group_validated_rows(rows)
        for candidate_group in validated_groups.values():
            query, candidate = _candidate_from_rows(candidate_group)
            grouped[query].append(candidate)
            for row in candidate_group:
                updated_at = row["row"]["updated_at"]
                if (
                    isinstance(updated_at, bool)
                    or not isinstance(updated_at, (int, float))
                    or updated_at < 0
                ):
                    raise RuntimeError("torrent timestamp is invalid")
                observed_at[query] = max(
                    observed_at.get(query, 0),
                    float(updated_at),
                )
        persisted = 0
        release_repository = ReleaseDiscoveryRepository(self._database)
        for query, candidates in grouped.items():
            stored = await release_repository.persist_legacy_torrent_batch(
                query,
                candidates,
                now=observed_at[query],
            )
            expected_locator_count = sum(
                len(candidate.locators) for candidate in candidates
            )
            actual_locator_count = sum(
                len(stored[candidate.candidate_id].locator_ids)
                for candidate in candidates
            )
            if (
                len(stored) != len(candidates)
                or actual_locator_count != expected_locator_count
            ):
                raise RuntimeError("torrent persistence lost a candidate")
            persisted += actual_locator_count
        return persisted

    async def load_cache_rows(
        self,
        media_id: str,
        media_scope: MediaScope,
        season: int | None,
        episode: int | None,
    ) -> list[dict[str, object]]:
        where = [
            "candidate.visibility_partition = :public_partition",
            "candidate.media_id = :media_id",
            "candidate.transport = 'bittorrent'",
            "identity.identity_scheme = 'btih'",
            "locator.locator_kind = 'torrent'",
            "locator.tombstoned_at_ms IS NULL",
        ]
        params: dict[str, object] = {
            "public_partition": _PUBLIC_PARTITION,
            "media_id": media_id,
        }
        rows = await self._database.fetch_all(
            f"""
            SELECT candidate.title, candidate.byte_size,
                   candidate.season_norm, candidate.episode_norm,
                   candidate.parsed_json, candidate.attributes_json,
                   candidate.updated_at_ms, identity.identity_value,
                   locator.locator_id, locator.locator_json,
                   locator.policy_json
            FROM release_candidates AS candidate
            JOIN candidate_identities AS identity
              ON identity.candidate_id = candidate.candidate_id
            JOIN release_locators AS locator
              ON locator.candidate_id = candidate.candidate_id
            WHERE {" AND ".join(where)}
            ORDER BY candidate.updated_at_ms DESC, candidate.candidate_id
            """,
            params,
            force_primary=True,
        )
        result = []
        for row in rows:
            attributes = decode_candidate_attributes(row["attributes_json"])
            stats = attributes["transport_stats"]
            locator = locator_from_json(
                row["locator_id"],
                "torrent",
                row["locator_json"],
                row["policy_json"],
            )
            has_selection = locator.selection_title is not None
            selected_season_norm = (
                locator.season_norm if has_selection else row["season_norm"]
            )
            selected_episode_norm = (
                locator.episode_norm if has_selection else row["episode_norm"]
            )
            locator_season = None if selected_season_norm < 0 else selected_season_norm
            locator_episode = (
                None if selected_episode_norm < 0 else selected_episode_norm
            )
            if (
                media_scope is not MediaScope.SERIES
                and season is not None
                and locator_season is not None
                and locator_season != season
            ):
                continue
            if (
                media_scope in (MediaScope.MOVIE, MediaScope.EPISODE)
                and locator_episode is not None
                and locator_episode != episode
            ):
                continue
            result.append(
                {
                    "info_hash": row["identity_value"],
                    "file_index": locator.file_index,
                    "title": (
                        locator.selection_title if has_selection else row["title"]
                    ),
                    "seeders": stats.get("seeders"),
                    "size": (
                        locator.selection_size if has_selection else row["byte_size"]
                    ),
                    "tracker": attributes["source"],
                    "sources_json": encode_json_param(stats.get("tracker_sources", ())),
                    "parsed_json": (
                        locator.selection_parsed_json
                        if has_selection
                        else row["parsed_json"]
                    ),
                    "season": locator_season,
                    "episode": locator_episode,
                    "updated_at": row["updated_at_ms"] / 1_000,
                }
            )
        return result

    async def existing_hashes(self, info_hashes: tuple[str, ...]) -> set[str]:
        existing = set()
        for start in range(0, len(info_hashes), _EXISTENCE_CHUNK):
            chunk = info_hashes[start : start + _EXISTENCE_CHUNK]
            rows = await self._database.fetch_all(
                f"""
                SELECT identity.identity_value
                FROM candidate_identities AS identity
                JOIN release_candidates AS candidate
                  ON candidate.candidate_id = identity.candidate_id
                WHERE candidate.visibility_partition = :public_partition
                  AND candidate.transport = 'bittorrent'
                  AND identity.identity_scheme = 'btih'
                  AND {_IDENTITY_MEMBERSHIP_SQL}
                """,
                {
                    "public_partition": _PUBLIC_PARTITION,
                    "info_hashes": encode_json_param(chunk),
                },
                force_primary=True,
            )
            existing.update(row["identity_value"] for row in rows)
        return existing

    async def existing_media_keys(
        self,
        media_id: str,
        info_hashes: tuple[str, ...],
    ) -> set[tuple[str, int | None, int | None]]:
        existing = set()
        for start in range(0, len(info_hashes), _EXISTENCE_CHUNK):
            chunk = info_hashes[start : start + _EXISTENCE_CHUNK]
            rows = await self._database.fetch_all(
                f"""
                SELECT identity.identity_value,
                       candidate.season_norm, candidate.episode_norm,
                       locator.locator_id, locator.locator_json,
                       locator.policy_json
                FROM candidate_identities AS identity
                JOIN release_candidates AS candidate
                  ON candidate.candidate_id = identity.candidate_id
                JOIN release_locators AS locator
                  ON locator.candidate_id = candidate.candidate_id
                WHERE candidate.visibility_partition = :public_partition
                  AND candidate.media_id = :media_id
                  AND candidate.transport = 'bittorrent'
                  AND identity.identity_scheme = 'btih'
                  AND locator.locator_kind = 'torrent'
                  AND locator.tombstoned_at_ms IS NULL
                  AND {_IDENTITY_MEMBERSHIP_SQL}
                """,
                {
                    "public_partition": _PUBLIC_PARTITION,
                    "media_id": media_id,
                    "info_hashes": encode_json_param(chunk),
                },
                force_primary=True,
            )
            for row in rows:
                locator = locator_from_json(
                    row["locator_id"],
                    "torrent",
                    row["locator_json"],
                    row["policy_json"],
                )
                has_selection = locator.selection_title is not None
                season_norm = (
                    locator.season_norm if has_selection else row["season_norm"]
                )
                episode_norm = (
                    locator.episode_norm if has_selection else row["episode_norm"]
                )
                existing.add(
                    (
                        row["identity_value"],
                        None if season_norm < 0 else season_norm,
                        None if episode_norm < 0 else episode_norm,
                    )
                )
        return existing

    async def find_context(
        self,
        info_hash: str,
        *,
        media_id: str | None = None,
    ) -> dict[str, object] | None:
        where = [
            "candidate.visibility_partition = :public_partition",
            "candidate.transport = 'bittorrent'",
            "identity.identity_scheme = 'btih'",
            "identity.identity_value = :info_hash",
        ]
        params: dict[str, object] = {
            "public_partition": _PUBLIC_PARTITION,
            "info_hash": info_hash,
        }
        if media_id is not None:
            where.append("candidate.media_id = :media_id")
            params["media_id"] = media_id
        row = await self._database.fetch_one(
            f"""
            SELECT candidate.media_id, candidate.attributes_json
            FROM candidate_identities AS identity
            JOIN release_candidates AS candidate
              ON candidate.candidate_id = identity.candidate_id
            WHERE {" AND ".join(where)}
            ORDER BY candidate.updated_at_ms DESC, candidate.candidate_id
            LIMIT 1
            """,
            params,
            force_primary=True,
        )
        if row is None:
            return None
        attributes = decode_candidate_attributes(row["attributes_json"])
        tracker_sources = attributes["transport_stats"].get("tracker_sources", [])
        if not isinstance(tracker_sources, list) or not all(
            isinstance(source, str) for source in tracker_sources
        ):
            raise RuntimeError("torrent tracker sources are invalid")
        return {
            "media_id": row["media_id"],
            "sources": tracker_sources,
        }

    async def delete_stale_public(self, *, before_ms: int) -> int:
        """Delete unreferenced public torrent candidates in bounded batches."""
        if type(before_ms) is not int or before_ms < 0:
            raise ValueError("torrent cache cutoff is invalid")
        deleted = 0
        while True:
            rows = await self._database.fetch_all(
                """
                SELECT candidate.candidate_id
                FROM release_candidates AS candidate
                WHERE candidate.visibility_partition = :public_partition
                  AND candidate.transport = 'bittorrent'
                  AND candidate.last_seen_at_ms < :before_ms
                  AND NOT EXISTS (
                      SELECT 1
                      FROM candidate_redirects AS redirect
                      WHERE redirect.redirected_candidate_id =
                                candidate.candidate_id
                         OR redirect.canonical_candidate_id =
                                candidate.candidate_id
                  )
                ORDER BY candidate.last_seen_at_ms, candidate.candidate_id
                LIMIT :limit
                """,
                {
                    "public_partition": _PUBLIC_PARTITION,
                    "before_ms": before_ms,
                    "limit": _GC_CHUNK,
                },
                force_primary=True,
            )
            if not rows:
                break
            candidate_ids = tuple(row["candidate_id"] for row in rows)
            async with self._database.transaction():
                await self._database.execute(
                    f"""
                    DELETE FROM release_candidates
                    WHERE {_CANDIDATE_MEMBERSHIP_SQL}
                      AND visibility_partition = :public_partition
                      AND transport = 'bittorrent'
                      AND last_seen_at_ms < :before_ms
                      AND NOT EXISTS (
                          SELECT 1
                          FROM candidate_redirects AS redirect
                          WHERE redirect.redirected_candidate_id =
                                    release_candidates.candidate_id
                             OR redirect.canonical_candidate_id =
                                    release_candidates.candidate_id
                      )
                    """,
                    {
                        "candidate_ids": encode_json_param(candidate_ids),
                        "public_partition": _PUBLIC_PARTITION,
                        "before_ms": before_ms,
                    },
                    force_primary=True,
                )
            deleted += len(candidate_ids)

        return deleted
