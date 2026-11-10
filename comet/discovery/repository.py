"""Transport-neutral persistence for discovered release candidates."""

import hashlib
import secrets
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import orjson
from sqlalchemy.engine.url import make_url

from comet.core.database import (
    build_changed_rows_upsert,
    build_json_list_membership_predicate,
    encode_json_param,
    settings,
)
from comet.core.locator_codec import (
    locator_from_json,
    locator_json,
    parsed_json,
    policy_json,
)
from comet.core.sources import (
    NzbArtifactRef,
    ReleaseCandidate,
    ReleaseScope,
    TorrentLocator,
    TransportKind,
)
from comet.discovery.coverage import (
    SearchCoverageRepository,
    query_fingerprint,
    search_scope,
)
from comet.discovery.models import MediaQuery
from comet.utils.parsing import load_cached_parsed

_PUBLIC_PARTITION = bytes(32)
LEGACY_TORRENT_DISCOVERY_CONFIGURATION_ID = "4d92fc74-6f21-5e67-96e5-80bc4f38092c"
_FINGERPRINT_CHARS = frozenset("0123456789abcdef")
_COVERAGE_CHUNK = 500
_LOCATOR_ID_MEMBERSHIP_SQL = build_json_list_membership_predicate(
    "locator_id", "locator_ids"
)
_IDENTITY_SCHEMES = frozenset({"btih", "nm1"})
_CANDIDATE_ID_MEMBERSHIP_SQL = build_json_list_membership_predicate(
    "redirect.redirected_candidate_id",
    "candidate_ids",
)
_UPSERT_CANDIDATE_QUERY = build_changed_rows_upsert(
    "release_candidates",
    columns={
        "candidate_id": "VARCHAR(36)",
        "visibility_partition": "CHAR(64)",
        "media_id": "TEXT",
        "transport": "VARCHAR(16)",
        "release_key": "TEXT",
        "scope": "VARCHAR(32)",
        "season_norm": "INTEGER",
        "episode_norm": "INTEGER",
        "daily_date": "VARCHAR(10)",
        "title": "TEXT",
        "byte_size": "BIGINT",
        "published_at_ms": "BIGINT",
        "parsed_json": "TEXT",
        "attributes_json": "TEXT",
        "is_private": "BOOLEAN",
        "created_at_ms": "BIGINT",
        "updated_at_ms": "BIGINT",
        "last_seen_at_ms": "BIGINT",
    },
    conflict_columns=(
        "visibility_partition",
        "media_id",
        "transport",
        "release_key",
        "scope",
        "season_norm",
        "episode_norm",
    ),
    compared_columns=(
        "daily_date",
        "title",
        "byte_size",
        "published_at_ms",
        "parsed_json",
        "attributes_json",
    ),
    refreshed_column="last_seen_at_ms",
    unchanged_conditions=("(current.is_private OR NOT CAST(:is_private AS BOOLEAN))",),
    update_expressions={
        "is_private": "release_candidates.is_private OR EXCLUDED.is_private",
        "updated_at_ms": "EXCLUDED.updated_at_ms",
    },
)
_CANDIDATE_IDS_QUERY = f"""
    SELECT candidate_id, transport, release_key
    FROM release_candidates
    WHERE visibility_partition = :visibility_partition
      AND media_id = :media_id
      AND scope = :scope
      AND season_norm = :season_norm
      AND episode_norm = :episode_norm
      AND {build_json_list_membership_predicate("release_key", "release_keys")}
"""
_UPSERT_CANONICAL_IDENTITY_QUERY = build_changed_rows_upsert(
    "candidate_identities",
    columns={
        "candidate_id": "VARCHAR(36)",
        "visibility_partition": "CHAR(64)",
        "transport": "VARCHAR(16)",
        "identity_scheme": "VARCHAR(16)",
        "identity_value": "TEXT",
        "created_at_ms": "BIGINT",
        "updated_at_ms": "BIGINT",
    },
    conflict_columns=("candidate_id", "identity_scheme", "identity_value"),
    compared_columns=(),
    refreshed_column="updated_at_ms",
)
_UPSERT_LOCATOR_QUERY = build_changed_rows_upsert(
    "release_locators",
    columns={
        "locator_id": "VARCHAR(36)",
        "candidate_id": "VARCHAR(36)",
        "origin_kind": "VARCHAR(32)",
        "discovery_configuration_id": "VARCHAR(36)",
        "owner_configuration_partition": "CHAR(64)",
        "account_partition": "CHAR(64)",
        "locator_kind": "VARCHAR(32)",
        "locator_json": "TEXT",
        "policy_json": "TEXT",
        "content_key": "VARCHAR(256)",
        "source_expires_at_ms": "BIGINT",
        "updated_at_ms": "BIGINT",
        "tombstoned_at_ms": "BIGINT",
    },
    conflict_columns=(
        "candidate_id",
        "locator_kind",
        "content_key",
        "owner_configuration_partition",
    ),
    compared_columns=(
        "account_partition",
        "locator_json",
        "policy_json",
        "source_expires_at_ms",
    ),
    refreshed_column="updated_at_ms",
    unchanged_conditions=("current.tombstoned_at_ms IS NULL",),
    update_expressions={"tombstoned_at_ms": "NULL"},
)
_LOCATOR_IDS_QUERY = f"""
    SELECT locator_id, candidate_id, locator_kind, content_key
    FROM release_locators
    WHERE owner_configuration_partition = :owner_configuration_partition
      AND {build_json_list_membership_predicate("candidate_id", "candidate_ids")}
"""


def _refresh_before_ms(now_ms: int) -> int:
    """Rows seen again are only rewritten once half their cache lifetime elapsed."""
    lifetimes = [
        ttl
        for ttl in (settings.LIVE_TORRENT_CACHE_TTL, settings.TORRENT_CACHE_TTL)
        if ttl >= 0
    ]
    return now_ms - min(lifetimes, default=31_536_000) * 500


@dataclass(frozen=True, slots=True)
class StoredCandidateIds:
    candidate_id: str
    locator_ids: dict[str, str]


class ReleaseDiscoveryRepository:
    def __init__(self, database):
        self._database = database
        backend = make_url(str(database.url)).get_backend_name()
        if backend not in {"postgresql", "sqlite"}:
            raise RuntimeError("database backend is unsupported")
        self._is_postgres = backend == "postgresql"

    async def persist_success(
        self,
        query: MediaQuery,
        branch_fingerprint: str,
        candidates: Iterable[ReleaseCandidate],
        *,
        discovery_configuration_id: str,
        owner_configuration_partition: bytes,
        account_partition: bytes | None = None,
        visibility_partition: bytes | None = None,
        public_visibility: bool = False,
        next_refresh_at: float,
        now: float | None = None,
    ) -> Mapping[str, StoredCandidateIds]:
        source_id = discovery_configuration_id
        owner = owner_configuration_partition.hex()
        stored_owner = _PUBLIC_PARTITION.hex() if public_visibility else owner
        account = (
            account_partition.hex()
            if account_partition is not None and not public_visibility
            else None
        )
        visibility = _visibility_partition(
            owner_configuration_partition,
            visibility_partition,
            public_visibility,
        )
        branch = branch_fingerprint
        query_key = query_fingerprint(query)
        scope = search_scope(query)
        candidate_batch = tuple(candidates)
        observed_at = time.time() if now is None else now
        observed_at_ms = int(observed_at * 1_000)
        async with self._database.transaction():
            stored, covered_locator_ids = await self._persist_candidate_rows(
                query,
                scope,
                candidate_batch,
                discovery_configuration_id=source_id,
                owner_configuration_partition=owner_configuration_partition,
                stored_owner_partition=stored_owner,
                account_partition=account,
                visibility_partition=visibility,
                public_visibility=public_visibility,
                content_namespace=source_id,
                observed_at_ms=observed_at_ms,
            )
            unique_locator_ids = list(dict.fromkeys(covered_locator_ids))
            retired = await self._database.fetch_all(
                f"""
                UPDATE release_locator_coverage
                SET tombstoned_at_ms = :now_ms
                WHERE query_fingerprint = :query_fingerprint
                  AND branch_fingerprint = :branch_fingerprint
                  AND tombstoned_at_ms IS NULL
                  AND NOT {_LOCATOR_ID_MEMBERSHIP_SQL}
                RETURNING locator_id
                """,
                {
                    "now_ms": observed_at_ms,
                    "query_fingerprint": query_key,
                    "branch_fingerprint": branch,
                    "locator_ids": encode_json_param(unique_locator_ids),
                },
                force_primary=True,
            )
            for offset in range(0, len(unique_locator_ids), _COVERAGE_CHUNK):
                chunk = unique_locator_ids[offset : offset + _COVERAGE_CHUNK]
                rows = ", ".join(
                    f"(:query_fingerprint, :branch_fingerprint, :locator_{index},"
                    " :now_ms, NULL)"
                    for index in range(len(chunk))
                )
                await self._database.execute(
                    f"""
                    INSERT INTO release_locator_coverage (
                        query_fingerprint, branch_fingerprint, locator_id,
                        last_seen_at_ms, tombstoned_at_ms
                    ) VALUES {rows}
                    ON CONFLICT (
                        query_fingerprint, branch_fingerprint, locator_id
                    ) DO UPDATE SET
                        last_seen_at_ms = excluded.last_seen_at_ms,
                        tombstoned_at_ms = NULL
                    """,
                    {
                        "query_fingerprint": query_key,
                        "branch_fingerprint": branch,
                        "now_ms": observed_at_ms,
                        **{
                            f"locator_{index}": locator_id
                            for index, locator_id in enumerate(chunk)
                        },
                    },
                    force_primary=True,
                )

            # Only locators whose coverage was just retired can have lost
            # their last active coverage.
            if retired:
                await self._database.execute(
                    f"""
                    UPDATE release_locators
                    SET tombstoned_at_ms = :now_ms
                    WHERE {_LOCATOR_ID_MEMBERSHIP_SQL}
                      AND discovery_configuration_id =
                          :discovery_configuration_id
                      AND tombstoned_at_ms IS NULL
                      AND NOT EXISTS (
                          SELECT 1
                          FROM release_locator_coverage coverage
                          WHERE coverage.locator_id =
                                release_locators.locator_id
                            AND coverage.tombstoned_at_ms IS NULL
                      )
                    """,
                    {
                        "now_ms": observed_at_ms,
                        "discovery_configuration_id": source_id,
                        "locator_ids": encode_json_param(
                            [row["locator_id"] for row in retired]
                        ),
                    },
                    force_primary=True,
                )
            await SearchCoverageRepository(self._database).record_success(
                query,
                branch,
                next_refresh_at=next_refresh_at,
            )
        return stored

    async def persist_public_torrent_batch(
        self,
        query: MediaQuery,
        candidates: Iterable[ReleaseCandidate],
        *,
        now: float | None = None,
    ) -> Mapping[str, StoredCandidateIds]:
        """Persist public torrent-cache rows without discovery coverage."""
        candidate_batch = tuple(candidates)
        if not candidate_batch:
            return {}
        scope = search_scope(query)
        observed_at = time.time() if now is None else now
        observed_at_ms = int(observed_at * 1_000)
        async with self._database.transaction():
            stored, _locator_ids = await self._persist_candidate_rows(
                query,
                scope,
                candidate_batch,
                discovery_configuration_id=(LEGACY_TORRENT_DISCOVERY_CONFIGURATION_ID),
                owner_configuration_partition=_PUBLIC_PARTITION,
                stored_owner_partition=_PUBLIC_PARTITION.hex(),
                account_partition=None,
                visibility_partition=_PUBLIC_PARTITION.hex(),
                public_visibility=True,
                content_namespace=LEGACY_TORRENT_DISCOVERY_CONFIGURATION_ID,
                observed_at_ms=observed_at_ms,
            )
        return stored

    async def resolve_candidate_id(self, candidate_id: str) -> str:
        """Resolve the permanent direct redirect for a merged candidate."""
        canonical_id, _redirected = await self._resolve_candidate_id(candidate_id)
        return canonical_id

    async def attach_identity(
        self,
        candidate_id: str,
        identity: str,
        *,
        now: float | None = None,
    ) -> str:
        """Attach exact content identity evidence and merge duplicates."""
        scheme, value = _candidate_identity(identity)
        observed_at = time.time() if now is None else now
        observed_at_ms = int(observed_at * 1_000)
        async with self._database.transaction():
            return await self._attach_identity(
                candidate_id,
                scheme,
                value,
                observed_at_ms,
            )

    async def _persist_candidate_rows(
        self,
        query: MediaQuery,
        scope: ReleaseScope,
        candidate_batch: tuple[ReleaseCandidate, ...],
        *,
        discovery_configuration_id: str,
        owner_configuration_partition: bytes,
        stored_owner_partition: str,
        account_partition: str | None,
        visibility_partition: str,
        public_visibility: bool,
        content_namespace: str,
        observed_at_ms: int,
    ) -> tuple[dict[str, StoredCandidateIds], list[str]]:
        planned: list[tuple[ReleaseCandidate, dict[str, object]]] = []
        for candidate in candidate_batch:
            planned.append(
                (
                    candidate,
                    _candidate_values(
                        candidate,
                        query,
                        scope,
                        visibility_partition,
                        observed_at_ms,
                    ),
                )
            )

        deduplicated: dict[tuple[str, str], dict[str, object]] = {}
        for _candidate, values in planned:
            deduplicated[(values["transport"], values["release_key"])] = values
        candidate_ids = await self._upsert_candidates(list(deduplicated.values()))

        canonical_torrent_keys = {
            (values["transport"], values["release_key"])
            for candidate, values in planned
            if _has_canonical_torrent_identity(candidate, values["release_key"])
        }
        await self._upsert_canonical_torrent_identities(
            (
                candidate_ids[candidate_key],
                visibility_partition,
                candidate_key[1][5:],
                observed_at_ms,
            )
            for candidate_key in canonical_torrent_keys
        )

        for candidate, values in planned:
            candidate_key = (values["transport"], values["release_key"])
            if candidate_key in canonical_torrent_keys:
                continue
            canonical_id = candidate_ids[candidate_key]
            for identity in _candidate_identity_evidence(
                candidate,
                include_artifact_identity=True,
            ):
                scheme, value = _candidate_identity(
                    identity,
                    trusted=candidate.transport is TransportKind.BITTORRENT,
                )
                canonical_id = await self._attach_identity(
                    canonical_id,
                    scheme,
                    value,
                    observed_at_ms,
                )
            candidate_ids[candidate_key] = canonical_id

        resolved_candidate_ids = await self._resolve_candidate_ids(
            tuple(candidate_ids.values())
        )
        for candidate_key, candidate_id in tuple(candidate_ids.items()):
            candidate_ids[candidate_key] = resolved_candidate_ids[candidate_id]

        planned_locators: list[tuple[int, str, dict[str, object]]] = []
        for index, (candidate, values) in enumerate(planned):
            candidate_id = candidate_ids[(values["transport"], values["release_key"])]
            for locator in candidate.locators:
                locator_policy_owner = locator.policy.owner_configuration_partition
                if public_visibility and locator_policy_owner is not None:
                    raise ValueError("public locator must not have an owner policy")
                if (
                    not public_visibility
                    and locator_policy_owner is not None
                    and locator_policy_owner != owner_configuration_partition
                ):
                    raise ValueError(
                        "locator owner policy does not match discovery owner"
                    )
                encoded_locator = locator_json(locator)
                encoded_policy = policy_json(locator)
                content_key = _content_key(
                    content_namespace,
                    locator.kind.value,
                    encoded_locator,
                )
                planned_locators.append(
                    (
                        index,
                        locator.locator_id,
                        {
                            "locator_id": _uuid7(observed_at_ms),
                            "candidate_id": candidate_id,
                            "origin_kind": "discovery",
                            "discovery_configuration_id": (discovery_configuration_id),
                            "owner_configuration_partition": (stored_owner_partition),
                            "account_partition": account_partition,
                            "locator_kind": locator.kind.value,
                            "locator_json": encoded_locator,
                            "policy_json": encoded_policy,
                            "content_key": content_key,
                            "source_expires_at_ms": (
                                locator.policy.expires_at * 1_000
                                if locator.policy.expires_at is not None
                                else None
                            ),
                            "now_ms": observed_at_ms,
                        },
                    )
                )

        deduplicated_locators: dict[tuple[str, str, str], dict[str, object]] = {}
        for _external, _locator_id, values in planned_locators:
            deduplicated_locators[
                (
                    values["candidate_id"],
                    values["locator_kind"],
                    values["content_key"],
                )
            ] = values
        locator_rows = await self._upsert_locators(list(deduplicated_locators.values()))

        covered_locator_ids: list[str] = []
        resolved_locators: list[dict[str, str]] = [{} for _ in planned]
        for index, external_locator_id, values in planned_locators:
            locator_id = locator_rows[
                (
                    values["candidate_id"],
                    values["locator_kind"],
                    values["content_key"],
                )
            ]
            covered_locator_ids.append(locator_id)
            resolved_locators[index][external_locator_id] = locator_id
        stored = {}
        for index, (candidate, values) in enumerate(planned):
            stored[candidate.candidate_id] = StoredCandidateIds(
                candidate_ids[(values["transport"], values["release_key"])],
                resolved_locators[index],
            )
        return stored, covered_locator_ids

    async def _attach_identity(
        self,
        candidate_id: str,
        scheme: str,
        value: str,
        observed_at_ms: int,
    ) -> str:
        if self._is_postgres:
            await self._database.fetch_val(
                "SELECT pg_advisory_xact_lock(:lock_id)",
                {"lock_id": _identity_lock_id(scheme, value)},
                force_primary=True,
            )
        else:
            # SQLite transactions are deferred. This harmless write acquires the
            # single-writer lock before the identity lookup.
            await self._database.execute(
                """
                UPDATE release_candidates
                SET updated_at_ms = updated_at_ms
                WHERE candidate_id = :candidate_id
                """,
                {"candidate_id": candidate_id},
                force_primary=True,
            )

        candidate_id, _visited = await self._resolve_candidate_id(candidate_id)
        candidate = await self._candidate_row(candidate_id)
        existing = await self._database.fetch_one(
            """
            SELECT identity.candidate_id
            FROM candidate_identities AS identity
            JOIN release_candidates AS existing
              ON existing.candidate_id = identity.candidate_id
            WHERE identity.visibility_partition = :visibility_partition
              AND identity.transport = :transport
              AND identity.identity_scheme = :identity_scheme
              AND identity.identity_value = :identity_value
              AND existing.media_id = :media_id
              AND existing.scope = :scope
              AND existing.season_norm = :season_norm
              AND existing.episode_norm = :episode_norm
              AND COALESCE(existing.daily_date, '') =
                  COALESCE(CAST(:daily_date AS TEXT), '')
            """,
            {
                "visibility_partition": candidate["visibility_partition"],
                "transport": candidate["transport"],
                "identity_scheme": scheme,
                "identity_value": value,
                "media_id": candidate["media_id"],
                "scope": candidate["scope"],
                "season_norm": candidate["season_norm"],
                "episode_norm": candidate["episode_norm"],
                "daily_date": candidate["daily_date"],
            },
            force_primary=True,
        )
        if existing is None:
            await self._database.execute(
                """
                INSERT INTO candidate_identities (
                    candidate_id, visibility_partition, transport,
                    identity_scheme, identity_value, created_at_ms, updated_at_ms
                ) VALUES (
                    :candidate_id, :visibility_partition, :transport,
                    :identity_scheme, :identity_value, :now_ms, :now_ms
                )
                """,
                {
                    "candidate_id": candidate_id,
                    "visibility_partition": candidate["visibility_partition"],
                    "transport": candidate["transport"],
                    "identity_scheme": scheme,
                    "identity_value": value,
                    "now_ms": observed_at_ms,
                },
                force_primary=True,
            )
            return candidate_id

        existing_id, _visited = await self._resolve_candidate_id(
            existing["candidate_id"]
        )
        if existing_id == candidate_id:
            await self._database.execute(
                """
                UPDATE candidate_identities
                SET updated_at_ms = :now_ms
                WHERE candidate_id = :candidate_id
                  AND identity_scheme = :identity_scheme
                  AND identity_value = :identity_value
                """,
                {
                    "now_ms": observed_at_ms,
                    "candidate_id": candidate_id,
                    "identity_scheme": scheme,
                    "identity_value": value,
                },
                force_primary=True,
            )
            return candidate_id

        return await self._merge_candidates(
            candidate_id,
            existing_id,
            observed_at_ms=observed_at_ms,
        )

    async def _merge_candidates(
        self,
        left_id: str,
        right_id: str,
        *,
        observed_at_ms: int,
    ) -> str:
        locked_ids = sorted({left_id, right_id})
        for candidate_id in locked_ids:
            await self._database.execute(
                """
                UPDATE release_candidates
                SET updated_at_ms = updated_at_ms
                WHERE candidate_id = :candidate_id
                """,
                {"candidate_id": candidate_id},
                force_primary=True,
            )

        left_id, _left_path = await self._resolve_candidate_id(left_id)
        right_id, _right_path = await self._resolve_candidate_id(right_id)
        if left_id == right_id:
            return left_id
        winner_id, loser_id = sorted((left_id, right_id))
        loser = await self._candidate_row(loser_id)

        await self._merge_candidate_locators(
            winner_id,
            loser_id,
            observed_at_ms=observed_at_ms,
        )
        await self._merge_candidate_identities(winner_id, loser_id)
        from comet.playback.resolution_cache import (
            ProviderResolutionCacheRepository,
        )

        await ProviderResolutionCacheRepository(self._database).reassign_candidate(
            loser_id,
            winner_id,
        )
        await self._database.execute(
            """
            UPDATE candidate_redirects
            SET canonical_candidate_id = :winner_id,
                updated_at_ms = :now_ms
            WHERE canonical_candidate_id = :loser_id
            """,
            {
                "winner_id": winner_id,
                "loser_id": loser_id,
                "now_ms": observed_at_ms,
            },
            force_primary=True,
        )
        await self._database.execute(
            """
            INSERT INTO candidate_redirects (
                redirected_candidate_id, canonical_candidate_id,
                created_at_ms, updated_at_ms
            ) VALUES (:loser_id, :winner_id, :now_ms, :now_ms)
            ON CONFLICT (redirected_candidate_id) DO UPDATE SET
                canonical_candidate_id = excluded.canonical_candidate_id,
                updated_at_ms = excluded.updated_at_ms
            """,
            {
                "loser_id": loser_id,
                "winner_id": winner_id,
                "now_ms": observed_at_ms,
            },
            force_primary=True,
        )
        await self._database.execute(
            """
            UPDATE release_candidates
            SET last_seen_at_ms = CASE
                    WHEN last_seen_at_ms < :loser_last_seen_at_ms
                    THEN :loser_last_seen_at_ms
                    ELSE last_seen_at_ms
                END,
                updated_at_ms = :now_ms
            WHERE candidate_id = :winner_id
            """,
            {
                "winner_id": winner_id,
                "loser_last_seen_at_ms": loser["last_seen_at_ms"],
                "now_ms": observed_at_ms,
            },
            force_primary=True,
        )
        return winner_id

    async def _merge_candidate_locators(
        self,
        winner_id: str,
        loser_id: str,
        *,
        observed_at_ms: int,
    ) -> None:
        locators = await self._database.fetch_all(
            """
            SELECT locator_id, locator_kind, content_key,
                   owner_configuration_partition
            FROM release_locators
            WHERE candidate_id = :candidate_id
            ORDER BY locator_id
            """,
            {"candidate_id": loser_id},
            force_primary=True,
        )
        for locator in locators:
            existing = await self._database.fetch_one(
                """
                SELECT locator_id
                FROM release_locators
                WHERE candidate_id = :candidate_id
                  AND locator_kind = :locator_kind
                  AND content_key = :content_key
                  AND owner_configuration_partition =
                      :owner_configuration_partition
                """,
                {
                    "candidate_id": winner_id,
                    "locator_kind": locator["locator_kind"],
                    "content_key": locator["content_key"],
                    "owner_configuration_partition": locator[
                        "owner_configuration_partition"
                    ],
                },
                force_primary=True,
            )
            if existing is None:
                await self._database.execute(
                    """
                    UPDATE release_locators
                    SET candidate_id = :winner_id,
                        updated_at_ms = :now_ms
                    WHERE locator_id = :locator_id
                    """,
                    {
                        "winner_id": winner_id,
                        "locator_id": locator["locator_id"],
                        "now_ms": observed_at_ms,
                    },
                    force_primary=True,
                )
                continue

            coverage_rows = await self._database.fetch_all(
                """
                SELECT query_fingerprint, branch_fingerprint,
                       last_seen_at_ms, tombstoned_at_ms
                FROM release_locator_coverage
                WHERE locator_id = :locator_id
                """,
                {"locator_id": locator["locator_id"]},
                force_primary=True,
            )
            for coverage in coverage_rows:
                await self._database.execute(
                    """
                    INSERT INTO release_locator_coverage (
                        query_fingerprint, branch_fingerprint, locator_id,
                        last_seen_at_ms, tombstoned_at_ms
                    ) VALUES (
                        :query_fingerprint, :branch_fingerprint, :locator_id,
                        :last_seen_at_ms, :tombstoned_at_ms
                    )
                    ON CONFLICT (
                        query_fingerprint, branch_fingerprint, locator_id
                    ) DO UPDATE SET
                        last_seen_at_ms = CASE
                            WHEN release_locator_coverage.last_seen_at_ms <
                                 excluded.last_seen_at_ms
                            THEN excluded.last_seen_at_ms
                            ELSE release_locator_coverage.last_seen_at_ms
                        END,
                        tombstoned_at_ms = CASE
                            WHEN release_locator_coverage.tombstoned_at_ms IS NULL
                              OR excluded.tombstoned_at_ms IS NULL
                            THEN NULL
                            WHEN release_locator_coverage.tombstoned_at_ms >
                                 excluded.tombstoned_at_ms
                            THEN release_locator_coverage.tombstoned_at_ms
                            ELSE excluded.tombstoned_at_ms
                        END
                    """,
                    {
                        "query_fingerprint": coverage["query_fingerprint"],
                        "branch_fingerprint": coverage["branch_fingerprint"],
                        "locator_id": existing["locator_id"],
                        "last_seen_at_ms": coverage["last_seen_at_ms"],
                        "tombstoned_at_ms": coverage["tombstoned_at_ms"],
                    },
                    force_primary=True,
                )
            await self._database.execute(
                "DELETE FROM release_locators WHERE locator_id = :locator_id",
                {"locator_id": locator["locator_id"]},
                force_primary=True,
            )

    async def _merge_candidate_identities(
        self,
        winner_id: str,
        loser_id: str,
    ) -> None:
        identities = await self._database.fetch_all(
            """
            SELECT identity_scheme, identity_value
            FROM candidate_identities
            WHERE candidate_id = :candidate_id
            ORDER BY identity_scheme, identity_value
            """,
            {"candidate_id": loser_id},
            force_primary=True,
        )
        for identity in identities:
            duplicate = await self._database.fetch_one(
                """
                SELECT 1
                FROM candidate_identities
                WHERE candidate_id = :candidate_id
                  AND identity_scheme = :identity_scheme
                  AND identity_value = :identity_value
                """,
                {
                    "candidate_id": winner_id,
                    "identity_scheme": identity["identity_scheme"],
                    "identity_value": identity["identity_value"],
                },
                force_primary=True,
            )
            if duplicate is not None:
                await self._database.execute(
                    """
                    DELETE FROM candidate_identities
                    WHERE candidate_id = :candidate_id
                      AND identity_scheme = :identity_scheme
                      AND identity_value = :identity_value
                    """,
                    {
                        "candidate_id": loser_id,
                        "identity_scheme": identity["identity_scheme"],
                        "identity_value": identity["identity_value"],
                    },
                    force_primary=True,
                )
            else:
                await self._database.execute(
                    """
                    UPDATE candidate_identities
                    SET candidate_id = :winner_id
                    WHERE candidate_id = :loser_id
                      AND identity_scheme = :identity_scheme
                      AND identity_value = :identity_value
                    """,
                    {
                        "winner_id": winner_id,
                        "loser_id": loser_id,
                        "identity_scheme": identity["identity_scheme"],
                        "identity_value": identity["identity_value"],
                    },
                    force_primary=True,
                )

    async def _resolve_candidate_id(
        self,
        candidate_id: str,
    ) -> tuple[str, tuple[str, ...]]:
        row = await self._database.fetch_one(
            """
            SELECT
                origin.visibility_partition,
                origin.transport,
                redirect.canonical_candidate_id,
                target.visibility_partition AS target_visibility_partition,
                target.transport AS target_transport,
                chained.redirected_candidate_id AS chained_candidate_id
            FROM release_candidates AS origin
            LEFT JOIN candidate_redirects AS redirect
              ON redirect.redirected_candidate_id = origin.candidate_id
            LEFT JOIN release_candidates AS target
              ON target.candidate_id = redirect.canonical_candidate_id
            LEFT JOIN candidate_redirects AS chained
              ON chained.redirected_candidate_id =
                 redirect.canonical_candidate_id
            WHERE origin.candidate_id = :candidate_id
            """,
            {"candidate_id": candidate_id},
            force_primary=True,
        )
        if row is None:
            raise ValueError("candidate is unavailable")
        target_id = row["canonical_candidate_id"]
        if target_id is None:
            return candidate_id, ()
        if target_id == candidate_id or row["chained_candidate_id"] is not None:
            raise RuntimeError("candidate redirect cycle or chain detected")
        if (
            row["target_visibility_partition"] != row["visibility_partition"]
            or row["target_transport"] != row["transport"]
        ):
            raise RuntimeError("candidate redirect crosses candidate family")
        return target_id, (candidate_id,)

    async def _resolve_candidate_ids(
        self,
        candidate_ids: tuple[str, ...],
    ) -> dict[str, str]:
        resolved = {candidate_id: candidate_id for candidate_id in candidate_ids}
        for offset in range(0, len(candidate_ids), _COVERAGE_CHUNK):
            chunk = candidate_ids[offset : offset + _COVERAGE_CHUNK]
            rows = await self._database.fetch_all(
                f"""
                SELECT
                    redirect.redirected_candidate_id,
                    redirect.canonical_candidate_id,
                    origin.visibility_partition,
                    origin.transport,
                    target.visibility_partition AS target_visibility_partition,
                    target.transport AS target_transport,
                    chained.redirected_candidate_id AS chained_candidate_id
                FROM candidate_redirects AS redirect
                JOIN release_candidates AS origin
                  ON origin.candidate_id = redirect.redirected_candidate_id
                JOIN release_candidates AS target
                  ON target.candidate_id = redirect.canonical_candidate_id
                LEFT JOIN candidate_redirects AS chained
                  ON chained.redirected_candidate_id =
                     redirect.canonical_candidate_id
                WHERE {_CANDIDATE_ID_MEMBERSHIP_SQL}
                """,
                {"candidate_ids": encode_json_param(chunk)},
                force_primary=True,
            )
            for row in rows:
                source_id = row["redirected_candidate_id"]
                target_id = row["canonical_candidate_id"]
                if target_id == source_id or row["chained_candidate_id"] is not None:
                    raise RuntimeError("candidate redirect cycle or chain detected")
                if (
                    row["target_visibility_partition"] != row["visibility_partition"]
                    or row["target_transport"] != row["transport"]
                ):
                    raise RuntimeError("candidate redirect crosses candidate family")
                resolved[source_id] = target_id
        return resolved

    async def _candidate_row(self, candidate_id: str):
        row = await self._database.fetch_one(
            """
            SELECT candidate_id, visibility_partition, media_id, transport,
                   scope, season_norm, episode_norm, daily_date,
                   last_seen_at_ms
            FROM release_candidates
            WHERE candidate_id = :candidate_id
            """,
            {"candidate_id": candidate_id},
            force_primary=True,
        )
        if row is None:
            raise ValueError("candidate is unavailable")
        return row

    async def _upsert_candidates(
        self, rows: list[dict[str, object]]
    ) -> dict[tuple[str, str], str]:
        if not rows:
            return {}
        shared = rows[0]
        now_ms = shared["now_ms"]
        timestamps = {
            "created_at_ms": now_ms,
            "updated_at_ms": now_ms,
            "last_seen_at_ms": now_ms,
            "refresh_before": _refresh_before_ms(now_ms),
        }
        await self._database.execute_many(
            _UPSERT_CANDIDATE_QUERY,
            [
                {key: value for key, value in row.items() if key != "now_ms"}
                | timestamps
                for row in rows
            ],
        )
        stored = await self._database.fetch_all(
            _CANDIDATE_IDS_QUERY,
            {
                "visibility_partition": shared["visibility_partition"],
                "media_id": shared["media_id"],
                "scope": shared["scope"],
                "season_norm": shared["season_norm"],
                "episode_norm": shared["episode_norm"],
                "release_keys": encode_json_param([row["release_key"] for row in rows]),
            },
            force_primary=True,
        )
        return {
            (row["transport"], row["release_key"]): row["candidate_id"]
            for row in stored
        }

    async def _upsert_canonical_torrent_identities(
        self,
        rows: Iterable[tuple[str, str, str, int]],
    ) -> None:
        await self._database.execute_many(
            _UPSERT_CANONICAL_IDENTITY_QUERY,
            [
                {
                    "candidate_id": candidate_id,
                    "visibility_partition": visibility,
                    "transport": "bittorrent",
                    "identity_scheme": "btih",
                    "identity_value": info_hash,
                    "created_at_ms": now_ms,
                    "updated_at_ms": now_ms,
                    "refresh_before": _refresh_before_ms(now_ms),
                }
                for candidate_id, visibility, info_hash, now_ms in rows
            ],
        )

    async def _upsert_locators(
        self, rows: list[dict[str, object]]
    ) -> dict[tuple[str, str, str], str]:
        if not rows:
            return {}
        now_ms = rows[0]["now_ms"]
        timestamps = {
            "updated_at_ms": now_ms,
            "tombstoned_at_ms": None,
            "refresh_before": _refresh_before_ms(now_ms),
        }
        await self._database.execute_many(
            _UPSERT_LOCATOR_QUERY,
            [
                {key: value for key, value in row.items() if key != "now_ms"}
                | timestamps
                for row in rows
            ],
        )
        stored = await self._database.fetch_all(
            _LOCATOR_IDS_QUERY,
            {
                "owner_configuration_partition": rows[0][
                    "owner_configuration_partition"
                ],
                "candidate_ids": encode_json_param(
                    list({row["candidate_id"] for row in rows})
                ),
            },
            force_primary=True,
        )
        return {
            (row["candidate_id"], row["locator_kind"], row["content_key"]): row[
                "locator_id"
            ]
            for row in stored
        }

    async def load_active(
        self,
        query: MediaQuery,
        branch_fingerprint: str,
        *,
        owner_configuration_partition: bytes,
        account_partition: bytes | None = None,
        visibility_partition: bytes | None = None,
        public_visibility: bool = False,
        now: float | None = None,
    ) -> tuple[ReleaseCandidate, ...]:
        owner = owner_configuration_partition.hex()
        account = account_partition.hex() if account_partition is not None else None
        if public_visibility:
            if visibility_partition not in (None, _PUBLIC_PARTITION):
                raise ValueError("public visibility must use the public partition")
            visibility = _PUBLIC_PARTITION.hex()
        else:
            visibility = (visibility_partition or owner_configuration_partition).hex()
            if visibility == _PUBLIC_PARTITION.hex():
                raise ValueError("private visibility partition must not be public")
        branch = branch_fingerprint
        query_key = query_fingerprint(query)
        scope = search_scope(query)
        observed_at = time.time() if now is None else now
        observed_at_ms = int(observed_at * 1_000)
        rows = await self._database.fetch_all(
            """
            SELECT
                candidate.candidate_id, candidate.media_id,
                candidate.scope, candidate.transport, candidate.title,
                candidate.byte_size,
                candidate.published_at_ms, candidate.parsed_json,
                candidate.attributes_json,
                candidate.is_private,
                locator.locator_id, locator.locator_kind,
                locator.locator_json, locator.policy_json
            FROM release_locator_coverage coverage
            JOIN release_locators locator
              ON locator.locator_id = coverage.locator_id
            JOIN release_candidates candidate
              ON candidate.candidate_id = locator.candidate_id
            WHERE coverage.query_fingerprint = :query_fingerprint
              AND coverage.branch_fingerprint = :branch_fingerprint
              AND coverage.tombstoned_at_ms IS NULL
              AND locator.tombstoned_at_ms IS NULL
              AND (
                  locator.source_expires_at_ms IS NULL
                  OR locator.source_expires_at_ms > :now_ms
              )
              AND (
                  (
                      :public_visibility = 1
                      AND locator.owner_configuration_partition =
                          :public_partition
                      AND locator.account_partition IS NULL
                      AND candidate.visibility_partition = :public_partition
                  )
                  OR
                  (
                      :public_visibility = 0
                      AND locator.owner_configuration_partition =
                          :owner_partition
                      AND candidate.visibility_partition =
                          :visibility_partition
                  )
              )
              AND (
                  (CAST(:account_partition AS TEXT) IS NULL
                   AND locator.account_partition IS NULL)
                  OR
                  (CAST(:account_partition AS TEXT) IS NOT NULL
                   AND (
                       locator.account_partition IS NULL
                       OR locator.account_partition = :account_partition
                   ))
              )
              AND candidate.media_id = :media_id
              AND candidate.scope = :scope
            ORDER BY candidate.candidate_id, locator.locator_id
            """,
            {
                "query_fingerprint": query_key,
                "branch_fingerprint": branch,
                "now_ms": observed_at_ms,
                "owner_partition": owner,
                "account_partition": account,
                "media_id": query.media_id,
                "scope": scope,
                "visibility_partition": visibility,
                "public_visibility": int(public_visibility),
                "public_partition": _PUBLIC_PARTITION.hex(),
            },
            force_primary=True,
        )
        grouped: dict[str, tuple[object, list]] = {}
        for row in rows:
            locator = locator_from_json(
                row["locator_id"],
                row["locator_kind"],
                row["locator_json"],
                row["policy_json"],
            )
            candidate_id = row["candidate_id"]
            entry = grouped.get(candidate_id)
            if entry is None:
                entry = (row, [])
                grouped[candidate_id] = entry
            entry[1].append(locator)
        result = []
        for candidate_id, (row, locators) in grouped.items():
            transport = TransportKind(row["transport"])
            attributes = _decode_attributes(
                row["attributes_json"],
                trusted=transport is TransportKind.BITTORRENT,
            )
            candidate = ReleaseCandidate(
                candidate_id=candidate_id,
                media_id=row["media_id"],
                scope=ReleaseScope(row["scope"]),
                transport=transport,
                title=row["title"],
                locators=tuple(locators),
                size=row["byte_size"],
                published_at_ms=row["published_at_ms"],
                source=attributes["source"],
                parsed=load_cached_parsed(row["parsed_json"]),
                transport_stats=attributes["transport_stats"],
                is_private=bool(row["is_private"]),
            )
            result.append(candidate)
        return tuple(result)


def _candidate_values(
    candidate: ReleaseCandidate,
    query: MediaQuery,
    scope: ReleaseScope,
    visibility: str,
    observed_at_ms: int,
) -> dict[str, object]:
    if candidate.transport is not TransportKind.BITTORRENT:
        if (
            not isinstance(candidate.candidate_id, str)
            or not 1 <= len(candidate.candidate_id) <= 128
        ):
            raise ValueError("candidate release key is invalid")
        if (
            not isinstance(candidate.title, str)
            or not 1 <= len(candidate.title) <= 1_024
        ):
            raise ValueError("candidate title is invalid")
    return {
        "candidate_id": _uuid7(observed_at_ms),
        "visibility_partition": visibility,
        "media_id": query.media_id,
        "transport": candidate.transport.value,
        "release_key": candidate.candidate_id,
        "scope": scope,
        "season_norm": -1 if query.season is None else query.season,
        "episode_norm": -1 if query.episode is None else query.episode,
        "daily_date": query.air_date if scope == "daily_episode" else None,
        "title": candidate.title,
        "byte_size": candidate.size,
        "published_at_ms": candidate.published_at_ms,
        "parsed_json": parsed_json(
            candidate.parsed,
            trusted=candidate.transport is TransportKind.BITTORRENT,
        ),
        "attributes_json": _attributes_json(candidate),
        "is_private": candidate.is_private,
        "now_ms": observed_at_ms,
    }


def _attributes_json(candidate: ReleaseCandidate) -> str:
    if candidate.transport is TransportKind.BITTORRENT:
        return orjson.dumps(
            {
                "source": candidate.source,
                "transport_stats": candidate.transport_stats,
            },
            option=orjson.OPT_SORT_KEYS,
        ).decode()
    source = candidate.source
    if not isinstance(source, str):
        raise ValueError("candidate source is invalid")
    stats = candidate.transport_stats
    if not isinstance(stats, dict):
        raise ValueError("candidate transport stats must be an object")
    try:
        payload = orjson.dumps(
            {
                "source": source,
                "transport_stats": stats,
            },
            option=orjson.OPT_SORT_KEYS,
        )
    except TypeError as exc:
        raise ValueError("candidate transport stats are not JSON-serializable") from exc
    if len(payload) > 65_536:
        raise ValueError("candidate attributes JSON is too large")
    return payload.decode()


def _decode_attributes(
    payload: str,
    *,
    trusted: bool = False,
) -> dict[str, object]:
    if trusted:
        return orjson.loads(payload)
    try:
        value = orjson.loads(payload)
    except (orjson.JSONDecodeError, TypeError) as exc:
        raise ValueError("persisted candidate attributes are invalid") from exc
    if (
        not isinstance(value, dict)
        or not {"source", "transport_stats"} <= value.keys()
        or not isinstance(value["source"], str)
    ):
        raise ValueError("persisted candidate attributes are invalid")
    stats = value["transport_stats"]
    if not isinstance(stats, dict):
        raise ValueError("persisted candidate attributes are invalid")
    return {
        "source": value["source"],
        "transport_stats": stats,
    }


def decode_candidate_attributes(payload: str) -> dict[str, object]:
    """Decode trusted server-side torrent attributes without filtering them."""
    return _decode_attributes(payload, trusted=True)


def _visibility_partition(
    owner_partition: bytes,
    visibility_partition: bytes | None,
    public_visibility: bool,
) -> str:
    if public_visibility:
        if visibility_partition not in (None, _PUBLIC_PARTITION):
            raise ValueError("public visibility must use the public partition")
        return _PUBLIC_PARTITION.hex()
    value = visibility_partition or owner_partition
    encoded = value.hex()
    if encoded == _PUBLIC_PARTITION.hex():
        raise ValueError("public visibility requires explicit authorization")
    return encoded


def _candidate_identity(
    identity: str,
    *,
    trusted: bool = False,
) -> tuple[str, str]:
    if trusted:
        scheme, value = identity.split(":", 1)
        return scheme, value
    if not isinstance(identity, str) or ":" not in identity:
        raise ValueError("candidate identity is invalid")
    scheme, value = identity.split(":", 1)
    if scheme not in _IDENTITY_SCHEMES:
        raise ValueError("candidate identity scheme is invalid")
    expected_lengths = {
        "btih": 40,
        "nm1": 64,
    }
    if len(value) != expected_lengths[scheme] or any(
        character not in _FINGERPRINT_CHARS for character in value
    ):
        raise ValueError("candidate identity value is invalid")
    return scheme, value


def _candidate_identity_evidence(
    candidate: ReleaseCandidate,
    *,
    include_artifact_identity: bool,
) -> tuple[str, ...]:
    identities = {}
    if candidate.candidate_id.startswith(("btih:", "nm1:")):
        identities[candidate.candidate_id] = None
    for identity in candidate.identities:
        identities[identity] = None
    for locator in candidate.locators:
        if isinstance(locator, TorrentLocator):
            identities[f"btih:{locator.info_hash.lower()}"] = None
        elif include_artifact_identity and isinstance(locator, NzbArtifactRef):
            identities[locator.manifest_identity] = None
    return tuple(identities)


def _has_canonical_torrent_identity(
    candidate: ReleaseCandidate,
    release_key: object,
) -> bool:
    if (
        candidate.transport is not TransportKind.BITTORRENT
        or release_key != candidate.candidate_id
        or not release_key.startswith("btih:")
    ):
        return False
    return _candidate_identity_evidence(
        candidate,
        include_artifact_identity=True,
    ) == (release_key,)


def _identity_lock_id(scheme: str, value: str) -> int:
    digest = hashlib.sha256(
        b"comet-candidate-identity-lock-v1\0" + scheme.encode() + b"\0" + value.encode()
    ).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _content_key(
    source_configuration_id: str,
    locator_kind: str,
    encoded_locator: str,
) -> str:
    return hashlib.sha256(
        b"comet-release-locator-v1\0"
        + source_configuration_id.encode()
        + b"\0"
        + locator_kind.encode()
        + b"\0"
        + encoded_locator.encode()
    ).hexdigest()


def _uuid7(unix_ms: int) -> str:
    value = (
        (unix_ms << 80)
        | (0x7 << 76)
        | (secrets.randbits(12) << 64)
        | (0b10 << 62)
        | secrets.randbits(62)
    )
    return str(uuid.UUID(int=value))
