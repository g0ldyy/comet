"""Credential-free persistence for capabilities rendered to a profile."""

import hmac
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import orjson

from comet.core.locator_codec import (
    locator_from_json,
    locator_json,
    parsed_json,
    policy_json,
)
from comet.core.sources import (
    EasynewsHttpRef,
    LocatorKind,
    LocatorPolicy,
    NzbArtifactRef,
    ReleaseCandidate,
    TransportKind,
)
from comet.core.database import (
    build_changed_rows_upsert,
    build_json_list_membership_predicate,
    encode_json_param,
)
from comet.discovery.repository import ReleaseDiscoveryRepository
from comet.usenet.archive_paths import normalize_archive_relative_path

_MAX_GC_BATCH = 256
_RENDERED_RELEASE_RETENTION_SECONDS = 24 * 60 * 60
# Re-rendering an unchanged release rewrites it at most this often; GC adds the
# same slack so every render still stays resolvable for the full retention.
_RENDERED_RELEASE_REFRESH_SECONDS = 60 * 60
_UPSERT_RENDERED_CANDIDATE_QUERY = build_changed_rows_upsert(
    "rendered_release_candidates",
    columns={
        "candidate_id": "CHAR(36)",
        "owner_configuration_partition": "CHAR(64)",
        "external_candidate_id": "TEXT",
        "media_id": "TEXT",
        "transport": "TEXT",
        "title": "TEXT",
        "byte_size": "BIGINT",
        "parsed_json": "TEXT",
        "created_at": "DOUBLE PRECISION",
        "updated_at": "DOUBLE PRECISION",
        "last_rendered_at": "DOUBLE PRECISION",
    },
    conflict_columns=("owner_configuration_partition", "external_candidate_id"),
    compared_columns=("media_id", "transport", "title", "byte_size", "parsed_json"),
    refreshed_column="last_rendered_at",
    update_expressions={"updated_at": "EXCLUDED.updated_at"},
)
_EXTERNAL_CANDIDATE_MEMBERSHIP_SQL = build_json_list_membership_predicate(
    "external_candidate_id", "external_candidate_ids"
)
_RENDERED_CANDIDATE_IDS_QUERY = f"""
    SELECT candidate_id, external_candidate_id
    FROM rendered_release_candidates
    WHERE owner_configuration_partition = :owner_configuration_partition
      AND {_EXTERNAL_CANDIDATE_MEMBERSHIP_SQL}
"""
_UPSERT_RENDERED_LOCATOR_QUERY = build_changed_rows_upsert(
    "rendered_release_locators",
    columns={
        "locator_id": "CHAR(36)",
        "candidate_id": "CHAR(36)",
        "external_locator_id": "TEXT",
        "locator_kind": "TEXT",
        "locator_json": "TEXT",
        "policy_json": "TEXT",
        "created_at": "DOUBLE PRECISION",
        "updated_at": "DOUBLE PRECISION",
        "last_rendered_at": "DOUBLE PRECISION",
    },
    conflict_columns=("candidate_id", "external_locator_id"),
    compared_columns=("locator_kind", "locator_json", "policy_json"),
    refreshed_column="last_rendered_at",
    update_expressions={"updated_at": "EXCLUDED.updated_at"},
)
_RENDERED_LOCATOR_IDS_QUERY = f"""
    SELECT locator_id, candidate_id, external_locator_id
    FROM rendered_release_locators
    WHERE {build_json_list_membership_predicate("candidate_id", "candidate_ids")}
"""


_BROKERED_LOCATOR_PREFIX = "brokered-nzb-v1:"


@dataclass(frozen=True, slots=True)
class RenderedCandidateIds:
    candidate_id: str
    locator_ids: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class ResolvedPlaybackIntent:
    candidate_id: str
    transport: str
    title: str
    byte_size: int | None
    locators: tuple[dict, ...]
    media_id: str


class RenderedReleaseRepository:
    def __init__(self, database):
        self._database = database

    async def garbage_collect(
        self,
        *,
        now: float | None = None,
        limit: int = _MAX_GC_BATCH,
    ) -> tuple[int, int]:
        """Remove expired rendered IDs without erasing mutation authority."""
        current_time = time.time() if now is None else now
        cutoff = (
            current_time
            - _RENDERED_RELEASE_RETENTION_SECONDS
            - _RENDERED_RELEASE_REFRESH_SECONDS
        )
        locators = await self._database.fetch_all(
            """
            DELETE FROM rendered_release_locators
            WHERE locator_id IN (
                SELECT locator.locator_id
                FROM rendered_release_locators AS locator
                WHERE locator.last_rendered_at < :cutoff
                  AND NOT EXISTS (
                      SELECT 1
                      FROM provider_preparations AS provider
                      WHERE provider.locator_id = locator.locator_id
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM asset_preparations AS playback
                      WHERE playback.candidate_id = locator.candidate_id
                        AND playback.absolute_expires_at >= :now
                  )
                ORDER BY locator.last_rendered_at, locator.locator_id
                LIMIT :limit
            )
              AND last_rendered_at < :cutoff
              AND NOT EXISTS (
                  SELECT 1
                  FROM provider_preparations AS provider
                  WHERE provider.locator_id =
                        rendered_release_locators.locator_id
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM asset_preparations AS playback
                  WHERE playback.candidate_id =
                        rendered_release_locators.candidate_id
                    AND playback.absolute_expires_at >= :now
              )
            RETURNING locator_id
            """,
            {"cutoff": cutoff, "now": current_time, "limit": limit},
            force_primary=True,
        )
        candidates = await self._database.fetch_all(
            """
            DELETE FROM rendered_release_candidates
            WHERE candidate_id IN (
                SELECT candidate.candidate_id
                FROM rendered_release_candidates AS candidate
                WHERE candidate.last_rendered_at < :cutoff
                  AND NOT EXISTS (
                      SELECT 1
                      FROM provider_preparations AS provider
                      WHERE provider.candidate_id = candidate.candidate_id
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM asset_preparations AS playback
                      WHERE playback.candidate_id = candidate.candidate_id
                        AND playback.absolute_expires_at >= :now
                  )
                ORDER BY candidate.last_rendered_at, candidate.candidate_id
                LIMIT :limit
            )
              AND last_rendered_at < :cutoff
              AND NOT EXISTS (
                  SELECT 1
                  FROM provider_preparations AS provider
                  WHERE provider.candidate_id =
                        rendered_release_candidates.candidate_id
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM asset_preparations AS playback
                  WHERE playback.candidate_id =
                        rendered_release_candidates.candidate_id
                    AND playback.absolute_expires_at >= :now
              )
            RETURNING candidate_id
            """,
            {"cutoff": cutoff, "now": current_time, "limit": limit},
            force_primary=True,
        )
        return len(locators), len(candidates)

    @staticmethod
    def _parsed_json(candidate: ReleaseCandidate) -> str:
        return parsed_json(
            candidate.parsed,
            trusted=candidate.transport is TransportKind.BITTORRENT,
        )

    async def persist(
        self,
        candidates: Iterable[ReleaseCandidate],
        *,
        owner_configuration_partition: bytes,
        now: float | None = None,
    ) -> Mapping[str, RenderedCandidateIds]:
        partition = owner_configuration_partition.hex()
        current_time = time.time() if now is None else now
        planned = [
            (
                candidate,
                {
                    "candidate_id": str(uuid.uuid4()),
                    "owner_configuration_partition": partition,
                    "external_candidate_id": candidate.candidate_id,
                    "media_id": candidate.media_id,
                    "transport": candidate.transport.value,
                    "title": candidate.title,
                    "byte_size": candidate.size,
                    "parsed_json": self._parsed_json(candidate),
                    "created_at": current_time,
                    "updated_at": current_time,
                    "last_rendered_at": current_time,
                },
            )
            for candidate in candidates
        ]
        deduplicated = {
            values["external_candidate_id"]: values for _candidate, values in planned
        }
        candidate_ids = await self._upsert_candidates(list(deduplicated.values()))

        planned_locators: list[tuple[int, str, dict[str, object]]] = []
        for index, (candidate, values) in enumerate(planned):
            candidate_id = candidate_ids[values["external_candidate_id"]]
            for locator in candidate.locators:
                planned_locators.append(
                    (
                        index,
                        locator.locator_id,
                        {
                            "locator_id": str(uuid.uuid4()),
                            "candidate_id": candidate_id,
                            "external_locator_id": locator.locator_id,
                            "locator_kind": locator.kind.value,
                            "locator_json": locator_json(locator),
                            "policy_json": policy_json(locator),
                            "created_at": current_time,
                            "updated_at": current_time,
                            "last_rendered_at": current_time,
                        },
                    )
                )
        deduplicated_locators = {
            (values["candidate_id"], values["external_locator_id"]): values
            for _index, _external, values in planned_locators
        }
        locator_ids = await self._upsert_locators(list(deduplicated_locators.values()))

        resolved: list[dict[str, str]] = [{} for _ in planned]
        for index, external_locator_id, values in planned_locators:
            resolved[index][external_locator_id] = locator_ids[
                (values["candidate_id"], values["external_locator_id"])
            ]
        result = {}
        for index, (candidate, values) in enumerate(planned):
            result[candidate.candidate_id] = RenderedCandidateIds(
                candidate_ids[values["external_candidate_id"]],
                resolved[index],
            )
        return result

    async def _upsert_candidates(self, rows: list[dict[str, object]]) -> dict[str, str]:
        if not rows:
            return {}
        refresh_before = rows[0]["last_rendered_at"] - _RENDERED_RELEASE_REFRESH_SECONDS
        await self._database.execute_many(
            _UPSERT_RENDERED_CANDIDATE_QUERY,
            [row | {"refresh_before": refresh_before} for row in rows],
        )
        stored = await self._database.fetch_all(
            _RENDERED_CANDIDATE_IDS_QUERY,
            {
                "owner_configuration_partition": rows[0][
                    "owner_configuration_partition"
                ],
                "external_candidate_ids": encode_json_param(
                    [row["external_candidate_id"] for row in rows]
                ),
            },
            force_primary=True,
        )
        return {row["external_candidate_id"]: row["candidate_id"] for row in stored}

    async def _upsert_locators(
        self, rows: list[dict[str, object]]
    ) -> dict[tuple[str, str], str]:
        if not rows:
            return {}
        refresh_before = rows[0]["last_rendered_at"] - _RENDERED_RELEASE_REFRESH_SECONDS
        await self._database.execute_many(
            _UPSERT_RENDERED_LOCATOR_QUERY,
            [row | {"refresh_before": refresh_before} for row in rows],
        )
        stored = await self._database.fetch_all(
            _RENDERED_LOCATOR_IDS_QUERY,
            {
                "candidate_ids": encode_json_param(
                    list({row["candidate_id"] for row in rows})
                )
            },
            force_primary=True,
        )
        return {
            (row["candidate_id"], row["external_locator_id"]): row["locator_id"]
            for row in stored
        }

    async def resolve_intent(
        self,
        candidate_id: str,
        locator_ids: list[str],
        *,
        owner_configuration_partition: bytes,
    ) -> ResolvedPlaybackIntent:
        """Resolve only the candidate/locator IDs signed into one capability."""
        partition = owner_configuration_partition.hex()
        candidate = await self._database.fetch_one(
            """
            SELECT candidate_id, media_id, transport, title, byte_size
            FROM rendered_release_candidates
            WHERE candidate_id = :candidate_id
              AND owner_configuration_partition = :partition
            """,
            {"candidate_id": candidate_id, "partition": partition},
            force_primary=True,
        )
        if candidate is None:
            raise ValueError("playback candidate is unavailable")
        placeholders = ", ".join(
            f":locator_{index}" for index in range(len(locator_ids))
        )
        rows = await self._database.fetch_all(
            f"""
            SELECT locator_id, locator_kind, locator_json, policy_json
            FROM rendered_release_locators
            WHERE candidate_id = :candidate_id
              AND locator_id IN ({placeholders})
            """,
            {
                "candidate_id": candidate_id,
                **{
                    f"locator_{index}": value for index, value in enumerate(locator_ids)
                },
            },
            force_primary=True,
        )
        by_id = {row["locator_id"]: row for row in rows}
        locators = []
        for locator_id in locator_ids:
            row = by_id.get(locator_id)
            if row is None:
                raise ValueError("playback locator is unavailable")
            locators.append(self._resolved_locator(row))
        return ResolvedPlaybackIntent(
            candidate_id,
            candidate["transport"],
            candidate["title"],
            candidate["byte_size"],
            tuple(locators),
            candidate["media_id"],
        )

    async def brokered_artifacts(
        self,
        candidate_id: str,
        source_locator_id: str,
        *,
        owner_configuration_partition: bytes,
    ) -> tuple[dict, ...]:
        """Load immutable artifacts derived from one exact committed source locator."""
        prefix = self._brokered_external_prefix(source_locator_id)
        rows = await self._database.fetch_all(
            """
            SELECT artifact.locator_id, artifact.external_locator_id,
                   artifact.locator_kind, artifact.locator_json,
                   artifact.policy_json
            FROM rendered_release_candidates AS candidate
            JOIN rendered_release_locators AS source
              ON source.candidate_id = candidate.candidate_id
            JOIN rendered_release_locators AS artifact
              ON artifact.candidate_id = candidate.candidate_id
            WHERE candidate.candidate_id = :candidate_id
              AND candidate.owner_configuration_partition = :partition
              AND source.locator_id = :source_locator_id
              AND source.locator_kind IN ('real_nzb', 'easynews_http')
              AND artifact.locator_kind = 'nzb_artifact'
              AND artifact.external_locator_id LIKE :external_prefix
            ORDER BY artifact.updated_at DESC, artifact.locator_id ASC
            """,
            {
                "candidate_id": candidate_id,
                "partition": owner_configuration_partition.hex(),
                "source_locator_id": source_locator_id,
                "external_prefix": prefix + "%",
            },
            force_primary=True,
        )
        artifacts = []
        for row in rows:
            try:
                locator = self._resolved_locator(row)
                artifact_sha256 = locator["payload"]["artifact_sha256"]
                if row["external_locator_id"] != prefix + artifact_sha256:
                    raise ValueError
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("brokered playback locator is corrupt") from exc
            artifacts.append(locator)
        return tuple(artifacts)

    async def attach_brokered_artifact(
        self,
        candidate_id: str,
        source_locator_id: str,
        artifact_sha256: str,
        manifest_identity: str,
        *,
        owner_configuration_partition: bytes,
        now: float | None = None,
    ) -> dict:
        async with self._database.transaction():
            return await self._attach_brokered_artifact(
                candidate_id,
                source_locator_id,
                artifact_sha256,
                manifest_identity,
                owner_configuration_partition=owner_configuration_partition,
                now=now,
            )

    async def _attach_brokered_artifact(
        self,
        candidate_id: str,
        source_locator_id: str,
        artifact_sha256: str,
        manifest_identity: str,
        *,
        owner_configuration_partition: bytes,
        now: float | None,
    ) -> dict:
        """Attach one immutable broker result without widening its source policy."""
        partition = owner_configuration_partition.hex()
        source = await self._database.fetch_one(
            """
            SELECT source.locator_kind, source.locator_json, source.policy_json,
                   candidate.external_candidate_id AS release_candidate_id,
                   source.external_locator_id AS release_locator_id
            FROM rendered_release_candidates AS candidate
            JOIN rendered_release_locators AS source
              ON source.candidate_id = candidate.candidate_id
            WHERE candidate.candidate_id = :candidate_id
              AND candidate.owner_configuration_partition = :partition
              AND source.locator_id = :source_locator_id
              AND source.locator_kind IN ('real_nzb', 'easynews_http')
            """,
            {
                "candidate_id": candidate_id,
                "partition": partition,
                "source_locator_id": source_locator_id,
            },
            force_primary=True,
        )
        if source is None:
            raise ValueError("broker source locator is unavailable")
        source_locator = locator_from_json(
            source_locator_id,
            source["locator_kind"],
            source["locator_json"],
            source["policy_json"],
        )
        selection_hint_name = None
        selection_hint_size = None
        if (
            isinstance(source_locator, EasynewsHttpRef)
            and source_locator.byte_size is not None
        ):
            candidate_hint = f"{source_locator.filename}.{source_locator.extension}"
            if normalize_archive_relative_path(candidate_hint) == candidate_hint:
                selection_hint_name = candidate_hint
                selection_hint_size = source_locator.byte_size
        artifact = NzbArtifactRef(
            locator_id=(
                self._brokered_external_prefix(source_locator_id) + artifact_sha256
            ),
            kind=LocatorKind.NZB_ARTIFACT,
            policy=LocatorPolicy(
                source_locator.policy.allowed_provider_kinds,
                owner_configuration_partition=owner_configuration_partition,
                exact_provider_configuration_id=(
                    source_locator.policy.exact_provider_configuration_id
                ),
                expires_at=source_locator.policy.expires_at,
            ),
            artifact_sha256=artifact_sha256,
            manifest_identity=manifest_identity,
            selection_hint_name=selection_hint_name,
            selection_hint_size=selection_hint_size,
        )
        encoded_locator = locator_json(artifact)
        encoded_policy = policy_json(artifact)
        current_time = time.time() if now is None else now
        locator_id = str(uuid.uuid4())
        row = await self._database.fetch_one(
            """
            INSERT INTO rendered_release_locators (
                locator_id, candidate_id, external_locator_id, locator_kind,
                locator_json, policy_json, created_at, updated_at,
                last_rendered_at
            ) VALUES (
                :locator_id, :candidate_id, :external_locator_id, 'nzb_artifact',
                :locator_json, :policy_json, :now, :now, :now
            ) ON CONFLICT (candidate_id, external_locator_id) DO UPDATE SET
                locator_json = EXCLUDED.locator_json,
                policy_json = EXCLUDED.policy_json,
                updated_at = EXCLUDED.updated_at,
                last_rendered_at = EXCLUDED.last_rendered_at
            RETURNING locator_id, locator_kind, locator_json, policy_json
            """,
            {
                "locator_id": locator_id,
                "candidate_id": candidate_id,
                "external_locator_id": artifact.locator_id,
                "locator_json": encoded_locator,
                "policy_json": encoded_policy,
                "now": current_time,
            },
            force_primary=True,
        )
        if row is None:
            raise RuntimeError("brokered playback locator was not persisted")
        await ReleaseDiscoveryRepository(self._database).attach_identity(
            source["release_candidate_id"],
            manifest_identity,
            now=current_time,
        )
        return self._resolved_locator(row)

    @staticmethod
    def _brokered_external_prefix(source_locator_id: str) -> str:
        return f"{_BROKERED_LOCATOR_PREFIX}{source_locator_id}:"

    @staticmethod
    def _resolved_locator(row) -> dict:
        locator = locator_from_json(
            row["locator_id"],
            row["locator_kind"],
            row["locator_json"],
            row["policy_json"],
        )
        return {
            "locator_id": locator.locator_id,
            "kind": locator.kind.value,
            "payload": orjson.loads(locator_json(locator)),
            "policy": orjson.loads(policy_json(locator)),
        }

    @staticmethod
    def authorize_intent(
        intent: ResolvedPlaybackIntent,
        *,
        provider_configuration_id: str,
        provider_kind: str,
        owner_configuration_partition: bytes,
        now: int | None = None,
    ) -> None:
        """Check persisted locator policy before a provider receives a reference."""
        partition = owner_configuration_partition.hex()
        current_time = int(time.time() if now is None else now)
        for locator in intent.locators:
            policy = locator["policy"]
            allowed_kinds = policy["allowed_provider_kinds"]
            exact_configuration_id = policy.get("exact_provider_configuration_id")
            policy_partition = policy.get("owner_configuration_partition")
            expires_at = policy.get("expires_at")
            if (
                provider_kind not in allowed_kinds
                or (
                    exact_configuration_id is not None
                    and exact_configuration_id != provider_configuration_id
                )
                or (
                    policy_partition is not None
                    and not hmac.compare_digest(policy_partition, partition)
                )
                or (expires_at is not None and expires_at <= current_time)
            ):
                raise ValueError("playback locator is no longer authorized")
