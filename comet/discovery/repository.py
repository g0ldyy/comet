"""Transport-neutral persistence for discovered release candidates."""

import hashlib
import math
import secrets
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import orjson
from sqlalchemy.engine.url import make_url

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
from comet.core.sql_batch import chunk_parameters
from comet.discovery.coverage import (
    SearchCoverageRepository,
    query_fingerprint,
    search_scope,
)
from comet.discovery.models import MediaQuery

_PUBLIC_PARTITION = bytes(32)
LEGACY_TORRENT_DISCOVERY_CONFIGURATION_ID = "4d92fc74-6f21-5e67-96e5-80bc4f38092c"
_FINGERPRINT_CHARS = frozenset("0123456789abcdef")
_CANDIDATE_CHUNK = 250
_LOCATOR_CHUNK = 300
_COVERAGE_CHUNK = 500
_IDENTITY_SCHEMES = frozenset({"btih", "nm1"})
_CANDIDATE_SHARED_COLUMNS = frozenset(
    {
        "visibility_partition",
        "media_id",
        "scope",
        "season_norm",
        "episode_norm",
        "daily_date",
        "now_ms",
    }
)
_LOCATOR_SHARED_COLUMNS = frozenset(
    {
        "origin_kind",
        "discovery_configuration_id",
        "owner_configuration_partition",
        "account_partition",
        "now_ms",
    }
)


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
        source_id = _canonical_uuid(
            discovery_configuration_id,
            "discovery configuration",
        )
        if not isinstance(public_visibility, bool):
            raise ValueError("public visibility flag is invalid")
        owner = _partition(owner_configuration_partition, "owner")
        stored_owner = _PUBLIC_PARTITION.hex() if public_visibility else owner
        account = (
            _partition(account_partition, "account")
            if account_partition is not None and not public_visibility
            else None
        )
        visibility = _visibility_partition(
            owner_configuration_partition,
            visibility_partition,
            public_visibility,
        )
        branch = _fingerprint(branch_fingerprint, "branch")
        query_key = query_fingerprint(query)
        scope = search_scope(query)
        candidate_batch = tuple(candidates)
        if len(candidate_batch) > 1_000:
            raise ValueError("discovery batch is too large")
        observed_at = time.time() if now is None else _timestamp(now)
        observed_at_ms = int(observed_at * 1_000)
        async with self._database.transaction():
            await self._database.execute(
                """
                UPDATE release_locator_coverage
                SET tombstoned_at_ms = :now_ms
                WHERE query_fingerprint = :query_fingerprint
                  AND branch_fingerprint = :branch_fingerprint
                  AND tombstoned_at_ms IS NULL
                """,
                {
                    "now_ms": observed_at_ms,
                    "query_fingerprint": query_key,
                    "branch_fingerprint": branch,
                },
                force_primary=True,
            )
            stored, covered_locator_ids = await self._persist_candidate_rows(
                query,
                scope,
                candidate_batch,
                origin_kind="discovery",
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

            await self._database.execute(
                """
                UPDATE release_locators
                SET tombstoned_at_ms = :now_ms
                WHERE discovery_configuration_id =
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
                },
                force_primary=True,
            )
            await SearchCoverageRepository(self._database).record_success(
                query,
                branch,
                next_refresh_at=next_refresh_at,
                now=observed_at,
            )
        return stored

    async def persist_manual(
        self,
        candidate: ReleaseCandidate,
        *,
        origin_kind: str,
        owner_configuration_partition: bytes,
        now: float | None = None,
    ) -> StoredCandidateIds:
        """Persist one owner-scoped manual import without search coverage."""
        if origin_kind not in {"manual_upload", "manual_url"}:
            raise ValueError("manual locator origin is invalid")
        _manual_candidate_id(candidate.candidate_id)
        if candidate.media_id != candidate.candidate_id:
            raise ValueError("manual candidate media identity is invalid")
        owner = _partition(owner_configuration_partition, "owner")
        observed_at = time.time() if now is None else _timestamp(now)
        observed_at_ms = int(observed_at * 1_000)
        query = MediaQuery(candidate.media_id, "movie")
        async with self._database.transaction():
            stored, _locator_ids = await self._persist_candidate_rows(
                query,
                query.scope,
                (candidate,),
                origin_kind=origin_kind,
                discovery_configuration_id=None,
                owner_configuration_partition=owner_configuration_partition,
                stored_owner_partition=owner,
                account_partition=None,
                visibility_partition=owner,
                public_visibility=False,
                content_namespace="manual-v1",
                observed_at_ms=observed_at_ms,
                require_owner_policy=True,
            )
        return stored[candidate.candidate_id]

    async def persist_legacy_torrent_batch(
        self,
        query: MediaQuery,
        candidates: Iterable[ReleaseCandidate],
        *,
        now: float | None = None,
    ) -> Mapping[str, StoredCandidateIds]:
        """Import credential-independent legacy cache rows without coverage.

        Account-derived legacy rows are intentionally ineligible because their
        modern HMAC partition cannot be reconstructed from the stored digest.
        """
        candidate_batch = tuple(candidates)
        if not candidate_batch:
            return {}
        if any(
            candidate.transport is not TransportKind.BITTORRENT
            for candidate in candidate_batch
        ):
            raise ValueError("legacy torrent batch contains another transport")
        scope = search_scope(query)
        observed_at = time.time() if now is None else _timestamp(now)
        observed_at_ms = int(observed_at * 1_000)
        async with self._database.transaction():
            stored, _locator_ids = await self._persist_candidate_rows(
                query,
                scope,
                candidate_batch,
                origin_kind="discovery",
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

    async def manual_artifact_origin(
        self,
        candidate_id: str,
        artifact_sha256: str,
        *,
        owner_configuration_partition: bytes,
        now: float | None = None,
    ) -> str:
        """Authorize an imported candidate/artifact pair for follow-up selection."""
        candidate_id = _manual_candidate_id(candidate_id)
        artifact_sha256 = _fingerprint(artifact_sha256, "artifact")
        owner = _partition(owner_configuration_partition, "owner")
        observed_at = time.time() if now is None else _timestamp(now)
        rows = await self._database.fetch_all(
            """
            SELECT
                locator.locator_id, locator.locator_json,
                locator.policy_json, locator.origin_kind
            FROM release_candidates candidate
            JOIN release_locators locator
              ON locator.candidate_id = candidate.candidate_id
            WHERE candidate.visibility_partition = :owner_partition
              AND candidate.media_id = :manual_candidate_id
              AND candidate.release_key = :manual_candidate_id
              AND candidate.transport = 'usenet'
              AND locator.owner_configuration_partition = :owner_partition
              AND locator.locator_kind = 'nzb_artifact'
              AND locator.origin_kind IN ('manual_upload', 'manual_url')
              AND locator.tombstoned_at_ms IS NULL
              AND (
                  locator.source_expires_at_ms IS NULL
                  OR locator.source_expires_at_ms > :now_ms
              )
            ORDER BY locator.locator_id
            """,
            {
                "owner_partition": owner,
                "manual_candidate_id": candidate_id,
                "now_ms": int(observed_at * 1_000),
            },
            force_primary=True,
        )
        for row in rows:
            locator = locator_from_json(
                row["locator_id"],
                "nzb_artifact",
                row["locator_json"],
                row["policy_json"],
            )
            if (
                locator.artifact_sha256 == artifact_sha256
                and locator.policy.owner_configuration_partition
                == owner_configuration_partition
            ):
                return row["origin_kind"]
        raise ValueError("manual artifact candidate is unavailable")

    async def resolve_candidate_id(self, candidate_id: str) -> str:
        """Resolve the permanent direct redirect for a merged candidate."""
        candidate_id = _canonical_uuid(candidate_id, "candidate")
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
        candidate_id = _canonical_uuid(candidate_id, "candidate")
        scheme, value = _candidate_identity(identity)
        observed_at = time.time() if now is None else _timestamp(now)
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
        origin_kind: str,
        discovery_configuration_id: str | None,
        owner_configuration_partition: bytes,
        stored_owner_partition: str,
        account_partition: str | None,
        visibility_partition: str,
        public_visibility: bool,
        content_namespace: str,
        observed_at_ms: int,
        require_owner_policy: bool = False,
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

        for candidate, values in planned:
            candidate_key = (values["transport"], values["release_key"])
            canonical_id = candidate_ids[candidate_key]
            for identity in _candidate_identity_evidence(
                candidate,
                include_artifact_identity=origin_kind
                not in {"manual_upload", "manual_url"},
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

        for candidate_key, candidate_id in tuple(candidate_ids.items()):
            candidate_ids[candidate_key], _visited = await self._resolve_candidate_id(
                candidate_id
            )

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
                if (
                    require_owner_policy
                    and locator_policy_owner != owner_configuration_partition
                ):
                    raise ValueError("manual locator must bind its owner")
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
                            "origin_kind": origin_kind,
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
        resolved: dict[tuple[str, str], str] = {}
        for offset in range(0, len(rows), _CANDIDATE_CHUNK):
            chunk = rows[offset : offset + _CANDIDATE_CHUNK]
            values = chunk_parameters(chunk, _CANDIDATE_SHARED_COLUMNS)
            tuples = [
                f"(:candidate_id_{index}, :visibility_partition,"
                f" :media_id, :transport_{index},"
                f" :release_key_{index}, :scope,"
                f" :season_norm, :episode_norm,"
                f" :daily_date, :title_{index},"
                f" :byte_size_{index}, :published_at_ms_{index},"
                f" :parsed_json_{index}, :attributes_json_{index},"
                f" :now_ms, :now_ms, :now_ms)"
                for index in range(len(chunk))
            ]
            returned = await self._database.fetch_all(
                f"""
                INSERT INTO release_candidates (
                    candidate_id, visibility_partition, media_id,
                    transport, release_key, scope, season_norm,
                    episode_norm, daily_date, title, byte_size,
                    published_at_ms, parsed_json, attributes_json,
                    created_at_ms, updated_at_ms, last_seen_at_ms
                ) VALUES {", ".join(tuples)}
                ON CONFLICT (
                    visibility_partition, media_id, transport,
                    release_key, scope, season_norm, episode_norm
                ) DO UPDATE SET
                    daily_date = excluded.daily_date,
                    title = excluded.title,
                    byte_size = excluded.byte_size,
                    published_at_ms = excluded.published_at_ms,
                    parsed_json = excluded.parsed_json,
                    attributes_json = excluded.attributes_json,
                    updated_at_ms = excluded.updated_at_ms,
                    last_seen_at_ms = excluded.last_seen_at_ms
                RETURNING candidate_id, transport, release_key
                """,
                values,
                force_primary=True,
            )
            for row in returned:
                resolved[(row["transport"], row["release_key"])] = row["candidate_id"]
        if len(resolved) != len(rows):  # pragma: no cover - database corruption
            raise RuntimeError("persisted discovery candidate disappeared")
        return resolved

    async def _upsert_locators(
        self, rows: list[dict[str, object]]
    ) -> dict[tuple[str, str, str], str]:
        resolved: dict[tuple[str, str, str], str] = {}
        for offset in range(0, len(rows), _LOCATOR_CHUNK):
            chunk = rows[offset : offset + _LOCATOR_CHUNK]
            values = chunk_parameters(chunk, _LOCATOR_SHARED_COLUMNS)
            tuples = [
                f"(:locator_id_{index}, :candidate_id_{index},"
                f" :origin_kind, :discovery_configuration_id,"
                f" :owner_configuration_partition,"
                f" :account_partition, :locator_kind_{index},"
                f" :locator_json_{index}, :policy_json_{index},"
                f" :content_key_{index}, :source_expires_at_ms_{index},"
                f" :now_ms, NULL)"
                for index in range(len(chunk))
            ]
            returned = await self._database.fetch_all(
                f"""
                INSERT INTO release_locators (
                    locator_id, candidate_id, origin_kind,
                    discovery_configuration_id,
                    owner_configuration_partition, account_partition,
                    locator_kind, locator_json, policy_json,
                    content_key, source_expires_at_ms,
                    updated_at_ms,
                    tombstoned_at_ms
                ) VALUES {", ".join(tuples)}
                ON CONFLICT (
                    candidate_id, locator_kind, content_key,
                    owner_configuration_partition
                ) DO UPDATE SET
                    account_partition = excluded.account_partition,
                    locator_json = excluded.locator_json,
                    policy_json = excluded.policy_json,
                    source_expires_at_ms = excluded.source_expires_at_ms,
                    updated_at_ms = excluded.updated_at_ms,
                    tombstoned_at_ms = NULL
                RETURNING locator_id, candidate_id, locator_kind, content_key
                """,
                values,
                force_primary=True,
            )
            for row in returned:
                resolved[
                    (row["candidate_id"], row["locator_kind"], row["content_key"])
                ] = row["locator_id"]
        if len(resolved) != len(rows):  # pragma: no cover - database corruption
            raise RuntimeError("persisted discovery locator disappeared")
        return resolved

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
        if not isinstance(public_visibility, bool):
            raise ValueError("public visibility flag is invalid")
        owner = _partition(owner_configuration_partition, "owner")
        account = (
            _partition(account_partition, "account")
            if account_partition is not None
            else None
        )
        if public_visibility:
            if visibility_partition not in (None, _PUBLIC_PARTITION):
                raise ValueError("public visibility must use the public partition")
            visibility = _PUBLIC_PARTITION.hex()
        else:
            visibility = _partition(
                visibility_partition or owner_configuration_partition,
                "visibility",
            )
            if visibility == _PUBLIC_PARTITION.hex():
                raise ValueError("private visibility partition must not be public")
        branch = _fingerprint(branch_fingerprint, "branch")
        query_key = query_fingerprint(query)
        scope = search_scope(query)
        observed_at = time.time() if now is None else _timestamp(now)
        observed_at_ms = int(observed_at * 1_000)
        rows = await self._database.fetch_all(
            """
            SELECT
                candidate.candidate_id, candidate.media_id,
                candidate.scope, candidate.transport, candidate.title,
                candidate.byte_size,
                candidate.published_at_ms, candidate.attributes_json,
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
            candidate_id = row["candidate_id"]
            entry = grouped.get(candidate_id)
            if entry is None:
                entry = (row, [])
                grouped[candidate_id] = entry
            entry[1].append(
                locator_from_json(
                    row["locator_id"],
                    row["locator_kind"],
                    row["locator_json"],
                    row["policy_json"],
                )
            )
        result = []
        for candidate_id, (row, locators) in grouped.items():
            transport = TransportKind(row["transport"])
            attributes = _decode_attributes(
                row["attributes_json"],
                trusted=transport is TransportKind.BITTORRENT,
            )
            result.append(
                ReleaseCandidate(
                    candidate_id=candidate_id,
                    media_id=row["media_id"],
                    scope=ReleaseScope(row["scope"]),
                    transport=transport,
                    title=row["title"],
                    locators=tuple(locators),
                    size=row["byte_size"],
                    published_at_ms=row["published_at_ms"],
                    source=attributes["source"],
                    transport_stats=attributes["transport_stats"],
                )
            )
        return tuple(result)


def _candidate_values(
    candidate: ReleaseCandidate,
    query: MediaQuery,
    scope: ReleaseScope,
    visibility: str,
    observed_at_ms: int,
) -> dict[str, object]:
    if not isinstance(candidate, ReleaseCandidate):
        raise ValueError("invalid release candidate")
    if candidate.media_id != query.media_id:
        raise ValueError("candidate media does not match discovery query")
    if candidate.scope is not scope:
        raise ValueError("candidate scope does not match discovery query")
    if candidate.transport is not TransportKind.BITTORRENT:
        if (
            not isinstance(candidate.candidate_id, str)
            or not 1 <= len(candidate.candidate_id) <= 128
            or any(ord(character) < 32 for character in candidate.candidate_id)
        ):
            raise ValueError("candidate release key is invalid")
        if (
            not isinstance(candidate.title, str)
            or not 1 <= len(candidate.title) <= 1_024
            or any(ord(character) < 32 for character in candidate.title)
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
    stats = _safe_attribute_value(candidate.transport_stats, 0)
    if not isinstance(stats, dict):
        raise ValueError("candidate transport stats must be an object")
    payload = orjson.dumps(
        {
            "source": source,
            "transport_stats": stats,
        },
        option=orjson.OPT_SORT_KEYS,
    ).decode()
    if len(payload.encode()) > 65_536:
        raise ValueError("candidate attributes JSON is too large")
    return payload


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
    stats = _safe_attribute_value(value["transport_stats"], 0)
    if not isinstance(stats, dict):
        raise ValueError("persisted candidate attributes are invalid")
    return {
        "source": value["source"],
        "transport_stats": stats,
    }


def decode_candidate_attributes(payload: str) -> dict[str, object]:
    """Decode trusted server-side torrent attributes without filtering them."""
    return _decode_attributes(payload, trusted=True)


def _safe_attribute_value(
    value: object,
    depth: int,
    *,
    unbounded: bool = False,
) -> object:
    if depth > 6:
        raise ValueError("candidate attributes are too deeply nested")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if not -(2**63) <= value < 2**63:
            raise ValueError("candidate attribute integer is out of range")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("candidate attribute number is invalid")
        return value
    if isinstance(value, str):
        if not unbounded and len(value) > 4_096:
            raise ValueError("candidate attribute text is invalid")
        return value
    if isinstance(value, (list, tuple)):
        if not unbounded and len(value) > 128:
            raise ValueError("candidate attribute list is too large")
        return [
            _safe_attribute_value(item, depth + 1, unbounded=unbounded)
            for item in value
        ]
    if isinstance(value, Mapping):
        if len(value) > 128:
            raise ValueError("candidate attribute object is too large")
        result = {}
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 128:
                raise ValueError("candidate attribute key is invalid")
            result[key] = _safe_attribute_value(
                item,
                depth + 1,
                unbounded=key == "tracker_sources",
            )
        return result
    raise ValueError("candidate attribute value is unsupported")


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
    encoded = _partition(value, "visibility")
    if encoded == _PUBLIC_PARTITION.hex():
        raise ValueError("public visibility requires explicit authorization")
    return encoded


def _partition(value: bytes, field: str) -> str:
    if not isinstance(value, bytes) or len(value) != 32:
        raise ValueError(f"{field} partition must contain 32 bytes")
    return value.hex()


def _fingerprint(value: str, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _FINGERPRINT_CHARS for character in value)
    ):
        raise ValueError(f"{field} fingerprint is invalid")
    return value


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


def _identity_lock_id(scheme: str, value: str) -> int:
    digest = hashlib.sha256(
        b"comet-candidate-identity-lock-v1\0" + scheme.encode() + b"\0" + value.encode()
    ).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _canonical_uuid(value: str, field: str) -> str:
    try:
        canonical = str(uuid.UUID(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{field} identifier is invalid") from exc
    if canonical != value:
        raise ValueError(f"{field} identifier is invalid")
    return canonical


def _manual_candidate_id(value: str) -> str:
    if not isinstance(value, str) or not value.startswith("manual:"):
        raise ValueError("manual candidate identity is invalid")
    identifier = value.removeprefix("manual:")
    try:
        _canonical_uuid(identifier, "manual candidate")
    except ValueError as exc:
        raise ValueError("manual candidate identity is invalid") from exc
    if f"manual:{identifier}" != value:
        raise ValueError("manual candidate identity is invalid")
    return value


def _timestamp(value: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError("discovery timestamp is invalid")
    return float(value)


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
    if not 0 <= unix_ms < 2**48:
        raise ValueError("UUIDv7 timestamp is out of range")
    value = (
        (unix_ms << 80)
        | (0x7 << 76)
        | (secrets.randbits(12) << 64)
        | (0b10 << 62)
        | secrets.randbits(62)
    )
    return str(uuid.UUID(int=value))
