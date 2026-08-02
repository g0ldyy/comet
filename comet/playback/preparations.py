"""Durable, owner-scoped preparation records for signed playback intents."""

import time
import uuid
from dataclasses import dataclass

import orjson

from comet.playback.resolution_cache import ProviderResolutionCacheRepository
from comet.playback.tokens import PlaybackIntent

_MAX_GC_BATCH = 256
ASSET_PREPARATION_PLAN_VERSIONS = (1, 2, 2, 1)


def _is_brokered_locator_lineage(
    external_locator_id: object,
    source_locator_ids: tuple[str, ...],
) -> bool:
    if not isinstance(external_locator_id, str):
        return False
    for source_locator_id in source_locator_ids:
        prefix = f"brokered-nzb-v1:{source_locator_id}:"
        if external_locator_id.startswith(prefix):
            digest = external_locator_id[len(prefix) :]
            return len(digest) == 64 and all(
                character in "0123456789abcdef" for character in digest
            )
    return False


@dataclass(frozen=True, slots=True)
class PlaybackPreparation:
    preparation_id: str
    candidate_id: str
    provider_configuration_id: str
    provider_kind: str
    locator_ids: tuple[str, ...]
    selection_intent: tuple[object, ...]
    client: str
    state: str
    target_kind: str | None
    target_ref: dict | None
    expires_at: float


class PlaybackPreparationRepository:
    def __init__(self, database):
        self._database = database

    @staticmethod
    def _canonical_ids(locator_ids: tuple[str, ...]) -> tuple[str, ...]:
        try:
            normalized = tuple(str(uuid.UUID(locator_id)) for locator_id in locator_ids)
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("invalid playback locators") from exc
        if (
            normalized != locator_ids
            or not 1 <= len(normalized) <= 16
            or len(set(normalized)) != len(normalized)
        ):
            raise ValueError("invalid playback locators")
        return normalized

    @staticmethod
    def _selection(value: tuple[object, ...]) -> str:
        return orjson.dumps(list(value), option=orjson.OPT_SORT_KEYS).decode()

    @staticmethod
    def _provider_reference(target_ref: dict | None) -> str | None:
        return None if target_ref is None else target_ref.get("provider_preparation_id")

    @staticmethod
    def representation_metadata(
        target_ref: dict | None,
        *,
        state: str | None,
    ) -> dict[str, object]:
        if target_ref is None or state == "failed":
            return {
                "exact_logical_length": None,
                "strong_asset_revision": None,
                "etag_strength": None,
                "selected_asset_id": None,
            }
        strong_revision = target_ref.get("strong_asset_revision")
        return {
            "exact_logical_length": target_ref.get("byte_size"),
            "strong_asset_revision": strong_revision,
            "etag_strength": (
                "strong"
                if strong_revision is not None
                else "weak"
                if state == "ready"
                else None
            ),
            "selected_asset_id": target_ref.get("selected_asset_id"),
        }

    async def _decrement_provider_reference(
        self,
        provider_preparation_id: str,
        *,
        count: int = 1,
    ) -> None:
        decremented = await self._database.fetch_one(
            """
            UPDATE provider_preparations
            SET refcount = refcount - :count
            WHERE preparation_id = :provider_preparation_id
              AND refcount >= :count
            RETURNING preparation_id
            """,
            {
                "provider_preparation_id": provider_preparation_id,
                "count": count,
            },
            force_primary=True,
        )
        if decremented is None:
            raise RuntimeError("provider preparation refcount is corrupt")

    async def _delete_expired(
        self,
        preparation_id: str,
        *,
        partition: str,
        now: float,
    ) -> bool:
        async with self._database.transaction():
            deleted = await self._database.fetch_one(
                """
                DELETE FROM asset_preparations
                WHERE preparation_id = :preparation_id
                  AND owner_configuration_partition = :partition
                  AND absolute_expires_at <= :now
                RETURNING provider_preparation_id
                """,
                {
                    "preparation_id": preparation_id,
                    "partition": partition,
                    "now": now,
                },
                force_primary=True,
            )
            if deleted is None:
                return False
            provider_preparation_id = deleted["provider_preparation_id"]
            if provider_preparation_id is not None:
                await self._decrement_provider_reference(
                    provider_preparation_id,
                )
        return True

    async def _replace_target(
        self,
        preparation_id: str,
        *,
        owner_configuration_partition: bytes,
        provider_account_partition: bytes,
        state: str | None,
        target_kind: str | None,
        target_ref: dict | None,
        target_json: str | None,
        pending_only: bool,
        now: float,
    ) -> None:
        """Replace one target and its exact remote-job reference atomically."""
        partition = owner_configuration_partition.hex()
        new_provider_id = self._provider_reference(target_ref)
        metadata = self.representation_metadata(
            target_ref,
            state=state,
        )
        async with self._database.transaction():
            playback = await self._database.fetch_one(
                f"""
                SELECT candidate_id, provider_configuration_id, provider_kind,
                       locator_ids_json, selection_intent_json,
                       provider_preparation_id, client, state,
                       absolute_expires_at
                FROM asset_preparations
                WHERE preparation_id = :preparation_id
                  AND owner_configuration_partition = :partition
                  AND absolute_expires_at > :now
                  {"AND state = 'pending'" if pending_only else ""}
                """,
                {
                    "preparation_id": preparation_id,
                    "partition": partition,
                    "now": now,
                },
                force_primary=True,
            )
            if playback is None:
                return
            old_provider_id = playback["provider_preparation_id"]
            if new_provider_id != old_provider_id and new_provider_id is not None:
                try:
                    locator_ids = self._canonical_ids(
                        tuple(orjson.loads(playback["locator_ids_json"]))
                    )
                except (TypeError, ValueError, orjson.JSONDecodeError) as exc:
                    raise ValueError("playback preparation is corrupt") from exc
                provider = await self._database.fetch_one(
                    """
                    SELECT provider.locator_id, derived.external_locator_id
                    FROM provider_preparations AS provider
                    LEFT JOIN rendered_release_locators AS derived
                      ON derived.locator_id = provider.locator_id
                     AND derived.candidate_id = provider.candidate_id
                    WHERE provider.preparation_id =
                          :provider_preparation_id
                      AND provider.owner_configuration_partition = :partition
                      AND provider.candidate_id = :candidate_id
                      AND provider.provider_configuration_id =
                          :provider_configuration_id
                      AND provider.provider_kind = :provider_kind
                      AND provider.selection_json = :selection_intent_json
                      AND (
                          provider.provider_kind <> 'torbox_usenet'
                          OR provider.cleanup_state NOT IN (
                              'in_progress', 'complete'
                          )
                      )
                    """,
                    {
                        "provider_preparation_id": new_provider_id,
                        "partition": partition,
                        "candidate_id": playback["candidate_id"],
                        "provider_configuration_id": playback[
                            "provider_configuration_id"
                        ],
                        "provider_kind": playback["provider_kind"],
                        "selection_intent_json": playback["selection_intent_json"],
                    },
                    force_primary=True,
                )
                if provider is None or (
                    provider["locator_id"] not in locator_ids
                    and not _is_brokered_locator_lineage(
                        provider["external_locator_id"],
                        locator_ids,
                    )
                ):
                    raise ValueError("provider preparation reference is unavailable")
                incremented = await self._database.fetch_one(
                    """
                    UPDATE provider_preparations
                    SET refcount = refcount + 1
                    WHERE preparation_id = :provider_preparation_id
                      AND owner_configuration_partition = :partition
                      AND candidate_id = :candidate_id
                      AND provider_configuration_id =
                          :provider_configuration_id
                      AND provider_kind = :provider_kind
                      AND selection_json = :selection_intent_json
                      AND locator_id = :locator_id
                      AND (
                          provider_kind <> 'torbox_usenet'
                          OR cleanup_state NOT IN ('in_progress', 'complete')
                      )
                    RETURNING preparation_id
                    """,
                    {
                        "provider_preparation_id": new_provider_id,
                        "partition": partition,
                        "candidate_id": playback["candidate_id"],
                        "provider_configuration_id": playback[
                            "provider_configuration_id"
                        ],
                        "provider_kind": playback["provider_kind"],
                        "selection_intent_json": playback["selection_intent_json"],
                        "locator_id": provider["locator_id"],
                    },
                    force_primary=True,
                )
                if incremented is None:
                    raise ValueError("provider preparation reference is unavailable")
            updated = await self._database.fetch_one(
                f"""
                UPDATE asset_preparations
                SET state = COALESCE(:state, state),
                    target_kind = :target_kind,
                    reconstruction_blueprint_json =
                        :reconstruction_blueprint_json,
                    provider_preparation_id = :new_provider_preparation_id,
                    last_used_at = :last_used_at
                WHERE preparation_id = :preparation_id
                  AND owner_configuration_partition = :partition
                  AND absolute_expires_at > :now
                  {"AND state = 'pending'" if pending_only else ""}
                  AND (
                      provider_preparation_id =
                          :old_provider_preparation_id
                      OR (
                          provider_preparation_id IS NULL
                          AND CAST(
                              :old_provider_preparation_id AS TEXT
                          ) IS NULL
                      )
                  )
                RETURNING preparation_id
                """,
                {
                    "preparation_id": preparation_id,
                    "partition": partition,
                    "state": state,
                    "target_kind": target_kind,
                    "reconstruction_blueprint_json": target_json,
                    "new_provider_preparation_id": new_provider_id,
                    "old_provider_preparation_id": old_provider_id,
                    "last_used_at": now,
                    "now": now,
                },
                force_primary=True,
            )
            if updated is None:
                raise RuntimeError("playback target changed concurrently")
            if new_provider_id != old_provider_id and old_provider_id is not None:
                await self._decrement_provider_reference(
                    old_provider_id,
                )
            if state == "ready" and target_kind is not None:
                await ProviderResolutionCacheRepository(self._database).record_ready(
                    rendered_candidate_id=playback["candidate_id"],
                    provider_kind=playback["provider_kind"],
                    provider_configuration_id=playback["provider_configuration_id"],
                    account_partition=provider_account_partition,
                    selection_intent_json=playback["selection_intent_json"],
                    client=playback["client"],
                    target_kind=target_kind,
                    representation=metadata,
                    observed_at=now,
                    expires_at=playback["absolute_expires_at"],
                )

    async def bind_artifact(
        self,
        preparation_id: str,
        *,
        owner_configuration_partition: bytes,
        artifact_grant_id: str,
        artifact_sha256: str,
        manifest_identity: str,
        now: float | None = None,
    ) -> None:
        """Pin one owner-granted exact manifest into the restart blueprint."""
        current_time = time.time() if now is None else now
        partition = owner_configuration_partition.hex()
        async with self._database.transaction():
            grant = await self._database.fetch_one(
                """
                SELECT grants.grant_id
                FROM nzb_artifact_grants AS grants
                JOIN nzb_artifacts AS artifact
                  ON artifact.artifact_sha256 = grants.artifact_sha256
                JOIN nzb_contents AS content
                  ON content.artifact_sha256 = artifact.artifact_sha256
                WHERE grants.grant_id = :artifact_grant_id
                  AND grants.owner_configuration_partition = :partition
                  AND grants.artifact_sha256 = :artifact_sha256
                  AND grants.expires_at > :now
                  AND artifact.publication_state = 'published'
                  AND content.manifest_identity = :manifest_identity
                  AND content.parser_version = :parser_version
                """,
                {
                    "artifact_grant_id": artifact_grant_id,
                    "partition": partition,
                    "artifact_sha256": artifact_sha256,
                    "manifest_identity": manifest_identity,
                    "parser_version": ASSET_PREPARATION_PLAN_VERSIONS[1],
                    "now": current_time,
                },
                force_primary=True,
            )
            if grant is None:
                raise ValueError("asset preparation artifact is unavailable")
            updated = await self._database.fetch_one(
                """
                UPDATE asset_preparations
                SET artifact_grant_id = :artifact_grant_id,
                    manifest_identity = :manifest_identity,
                    last_used_at = :now
                WHERE preparation_id = :preparation_id
                  AND owner_configuration_partition = :partition
                  AND absolute_expires_at > :now
                  AND (
                      artifact_grant_id IS NULL
                      OR artifact_grant_id = :artifact_grant_id
                  )
                  AND (
                      manifest_identity IS NULL
                      OR manifest_identity = :manifest_identity
                  )
                RETURNING preparation_id
                """,
                {
                    "preparation_id": preparation_id,
                    "artifact_grant_id": artifact_grant_id,
                    "manifest_identity": manifest_identity,
                    "partition": partition,
                    "now": current_time,
                },
                force_primary=True,
            )
            if updated is None:
                raise ValueError("asset preparation artifact changed")

    async def garbage_collect(
        self,
        *,
        now: float | None = None,
        limit: int = _MAX_GC_BATCH,
    ) -> int:
        """Delete only expired capabilities in one bounded primary query."""
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= _MAX_GC_BATCH
        ):
            raise ValueError("invalid playback preparation GC limit")
        current_time = time.time() if now is None else now
        async with self._database.transaction():
            rows = await self._database.fetch_all(
                """
                DELETE FROM asset_preparations
                WHERE preparation_id IN (
                    SELECT preparation_id
                    FROM asset_preparations
                    WHERE absolute_expires_at < :now
                    ORDER BY absolute_expires_at, preparation_id
                    LIMIT :limit
                )
                  AND absolute_expires_at < :now
                RETURNING preparation_id, provider_preparation_id
                """,
                {"now": current_time, "limit": limit},
                force_primary=True,
            )
            references: dict[str, int] = {}
            for row in rows:
                provider_preparation_id = row["provider_preparation_id"]
                if provider_preparation_id is not None:
                    references[provider_preparation_id] = (
                        references.get(provider_preparation_id, 0) + 1
                    )
            for provider_preparation_id, count in references.items():
                await self._decrement_provider_reference(
                    provider_preparation_id,
                    count=count,
                )
        return len(rows)

    async def get_or_create(
        self,
        intent: PlaybackIntent,
        *,
        provider_kind: str,
        owner_configuration_partition: bytes,
        preparation_intent_key: str,
        ttl: int = 6 * 60 * 60,
        now: float | None = None,
    ) -> PlaybackPreparation:
        candidate_id = intent.candidate_id
        provider_id = intent.provider_configuration_id
        locator_ids = intent.locator_ids
        selection_intent_json = self._selection(intent.selection_intent)
        locator_ids_json = orjson.dumps(locator_ids).decode()
        partition = owner_configuration_partition.hex()
        current_time = time.time() if now is None else now
        params = {
            "partition": partition,
            "candidate_id": candidate_id,
            "provider_id": provider_id,
            "preparation_intent_key": preparation_intent_key,
            "selection_intent_json": selection_intent_json,
            "client": intent.client,
        }
        existing = await self._database.fetch_one(
            """
            UPDATE asset_preparations
            SET last_used_at = :now
            WHERE preparation_intent_key = :preparation_intent_key
              AND owner_configuration_partition = :partition
              AND absolute_expires_at > :now
            RETURNING preparation_id, candidate_id, provider_configuration_id,
                      provider_kind, locator_ids_json,
                      selection_intent_json AS selection_json,
                      client, state, target_kind,
                      reconstruction_blueprint_json AS target_ref_json,
                      provider_preparation_id,
                      absolute_expires_at AS expires_at
            """,
            {
                "preparation_intent_key": preparation_intent_key,
                "partition": partition,
                "now": current_time,
            },
            force_primary=True,
        )
        if existing is not None:
            preparation = self._row(existing)
            if (
                preparation.candidate_id != candidate_id
                or preparation.provider_configuration_id != provider_id
                or preparation.provider_kind != provider_kind
                or preparation.locator_ids != locator_ids
                or preparation.selection_intent != intent.selection_intent
            ):
                raise RuntimeError("asset preparation intent key collision")
            return preparation
        expired = await self._database.fetch_one(
            """
            SELECT preparation_id
            FROM asset_preparations
            WHERE preparation_intent_key = :preparation_intent_key
              AND owner_configuration_partition = :partition
              AND absolute_expires_at <= :now
            """,
            {
                "preparation_intent_key": preparation_intent_key,
                "partition": partition,
                "now": current_time,
            },
            force_primary=True,
        )
        if expired is not None:
            await self._delete_expired(
                expired["preparation_id"],
                partition=partition,
                now=current_time,
            )
        preparation_id = str(uuid.uuid4())
        absolute_expires_at = current_time + ttl
        await self._database.execute(
            """
            INSERT INTO asset_preparations (
                preparation_id, owner_configuration_partition,
                preparation_intent_key, candidate_id,
                provider_configuration_id, provider_kind,
                locator_ids_json, selection_intent_json,
                selection_intent_version, parser_version, selector_version,
                archive_plan_version, client, state, created_at, last_used_at,
                idle_expires_at, absolute_expires_at
            ) VALUES (
                :preparation_id, :partition, :preparation_intent_key,
                :candidate_id, :provider_id, :provider_kind,
                :locator_ids_json, :selection_intent_json,
                :selection_intent_version, :parser_version,
                :selector_version, :archive_plan_version, :client, 'pending',
                :created_at, :last_used_at, :idle_expires_at,
                :absolute_expires_at
            ) ON CONFLICT (preparation_intent_key) DO NOTHING
            """,
            {
                **params,
                "preparation_id": preparation_id,
                "provider_kind": provider_kind,
                "locator_ids_json": locator_ids_json,
                "selection_intent_version": ASSET_PREPARATION_PLAN_VERSIONS[0],
                "parser_version": ASSET_PREPARATION_PLAN_VERSIONS[1],
                "selector_version": ASSET_PREPARATION_PLAN_VERSIONS[2],
                "archive_plan_version": ASSET_PREPARATION_PLAN_VERSIONS[3],
                "created_at": current_time,
                "last_used_at": current_time,
                "idle_expires_at": absolute_expires_at,
                "absolute_expires_at": absolute_expires_at,
            },
            force_primary=True,
        )
        created = await self._database.fetch_one(
            """
            SELECT preparation_id, candidate_id, provider_configuration_id,
                   provider_kind, locator_ids_json,
                   selection_intent_json AS selection_json,
                   client, state, target_kind,
                   reconstruction_blueprint_json AS target_ref_json,
                   provider_preparation_id,
                   absolute_expires_at AS expires_at
            FROM asset_preparations
            WHERE preparation_intent_key = :preparation_intent_key
              AND owner_configuration_partition = :partition
              AND absolute_expires_at > :now
            """,
            {
                "preparation_intent_key": preparation_intent_key,
                "partition": partition,
                "now": current_time,
            },
            force_primary=True,
        )
        if created is None:
            raise RuntimeError("playback preparation was not persisted")
        return self._row(created)

    async def resolve(
        self,
        preparation_id: str,
        *,
        owner_configuration_partition: bytes,
        now: float | None = None,
    ) -> PlaybackPreparation:
        current_time = time.time() if now is None else now
        row = await self._database.fetch_one(
            """
            UPDATE asset_preparations
            SET last_used_at = :now
            WHERE preparation_id = :preparation_id
              AND owner_configuration_partition = :partition
              AND absolute_expires_at > :now
            RETURNING preparation_id, candidate_id,
                      provider_configuration_id, provider_kind,
                      locator_ids_json,
                      selection_intent_json AS selection_json,
                      client, state, target_kind,
                      reconstruction_blueprint_json AS target_ref_json,
                      provider_preparation_id,
                      absolute_expires_at AS expires_at
            """,
            {
                "preparation_id": preparation_id,
                "partition": owner_configuration_partition.hex(),
                "now": current_time,
            },
            force_primary=True,
        )
        if row is None:
            raise ValueError("playback preparation is unavailable")
        return self._row(row)

    async def mark_ready(
        self,
        preparation_id: str,
        *,
        owner_configuration_partition: bytes,
        provider_account_partition: bytes,
        target_kind: str,
        target_ref: dict,
        now: float | None = None,
    ) -> None:
        """Commit an opaque provider target after a successful preparation."""
        target_json = orjson.dumps(
            target_ref,
            option=orjson.OPT_SORT_KEYS,
        ).decode()
        current_time = time.time() if now is None else now
        await self._replace_target(
            preparation_id,
            owner_configuration_partition=owner_configuration_partition,
            provider_account_partition=provider_account_partition,
            state="ready",
            target_kind=target_kind,
            target_ref=target_ref,
            target_json=target_json,
            pending_only=False,
            now=current_time,
        )

    async def record_pending_target(
        self,
        preparation_id: str,
        *,
        owner_configuration_partition: bytes,
        provider_account_partition: bytes,
        target_kind: str,
        target_ref: dict,
        now: float | None = None,
    ) -> None:
        """Record an upstream job that is still preparing without marking it ready."""
        target_json = orjson.dumps(target_ref, option=orjson.OPT_SORT_KEYS).decode()
        current_time = time.time() if now is None else now
        await self._replace_target(
            preparation_id,
            owner_configuration_partition=owner_configuration_partition,
            provider_account_partition=provider_account_partition,
            state=None,
            target_kind=target_kind,
            target_ref=target_ref,
            target_json=target_json,
            pending_only=True,
            now=current_time,
        )

    async def clear_pending_target(
        self,
        preparation_id: str,
        *,
        owner_configuration_partition: bytes,
        provider_account_partition: bytes,
        now: float | None = None,
    ) -> None:
        """Forget a tombstoned remote target while retaining the same capability."""
        current_time = time.time() if now is None else now
        await self._replace_target(
            preparation_id,
            owner_configuration_partition=owner_configuration_partition,
            provider_account_partition=provider_account_partition,
            state=None,
            target_kind=None,
            target_ref=None,
            target_json=None,
            pending_only=True,
            now=current_time,
        )

    async def mark_failed(
        self,
        preparation_id: str,
        *,
        owner_configuration_partition: bytes,
        provider_account_partition: bytes,
        code: str,
        now: float | None = None,
    ) -> None:
        """Record a bounded safe code without retaining an upstream response."""
        if (
            not isinstance(code, str)
            or not code
            or len(code.encode()) > 128
            or any(ord(character) < 32 or ord(character) == 127 for character in code)
        ):
            raise ValueError("invalid playback failure")
        current_time = time.time() if now is None else now
        failure = {"failure_code": code}
        await self._replace_target(
            preparation_id,
            owner_configuration_partition=owner_configuration_partition,
            provider_account_partition=provider_account_partition,
            state="failed",
            target_kind=None,
            target_ref=failure,
            target_json=orjson.dumps(failure).decode(),
            pending_only=False,
            now=current_time,
        )

    @classmethod
    def _row(cls, row) -> PlaybackPreparation:
        try:
            candidate_id = str(uuid.UUID(row["candidate_id"]))
            provider_configuration_id = str(uuid.UUID(row["provider_configuration_id"]))
            locator_ids = cls._canonical_ids(
                tuple(orjson.loads(row["locator_ids_json"]))
            )
            selection = tuple(orjson.loads(row["selection_json"]))
            target_ref = (
                orjson.loads(row["target_ref_json"])
                if row["target_ref_json"] is not None
                else None
            )
            if target_ref is not None and not isinstance(target_ref, dict):
                raise ValueError
            if cls._provider_reference(target_ref) != row["provider_preparation_id"]:
                raise ValueError
            if row["state"] not in {"pending", "ready", "failed"}:
                raise ValueError
            if row["state"] == "ready" and (
                row["target_kind"] not in {"cloud", "relay", "native"}
                or target_ref is None
            ):
                raise ValueError
            if row["state"] == "failed" and (
                row["target_kind"] is not None or target_ref is None
            ):
                raise ValueError
        except (TypeError, ValueError, orjson.JSONDecodeError) as exc:
            raise ValueError("playback preparation is corrupt") from exc
        return PlaybackPreparation(
            str(uuid.UUID(row["preparation_id"])),
            candidate_id,
            provider_configuration_id,
            row["provider_kind"],
            locator_ids,
            selection,
            row["client"],
            row["state"],
            row["target_kind"],
            target_ref,
            row["expires_at"],
        )
