"""Durable remote-work ledger shared by mutating playback providers."""

import math
import time
import uuid
from dataclasses import dataclass

import orjson

from comet.core.sources import MAX_SIGNED_BIGINT

_MAX_GC_BATCH = 256
_TERMINAL_RETENTION_SECONDS = 6 * 60 * 60
_CLEANUP_RETRY_SECONDS = 60
_ALTMOUNT_RETRY_SECONDS = 20
_SAB_ABSENCE_CONFIRMATION_SECONDS = 5
_OWNERSHIP_STATES = {"created", "adopted", "unknown"}


def provider_selection_json(selection: tuple[object, ...]) -> str:
    return orjson.dumps(list(selection), option=orjson.OPT_SORT_KEYS).decode()


def _provider_status(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 512:
        raise ValueError("provider preparation is corrupt")
    return value


@dataclass(frozen=True, slots=True)
class ProviderPreparation:
    preparation_id: str
    state: str
    payload: dict
    created_at: float


@dataclass(frozen=True, slots=True)
class TorBoxCleanupTarget:
    preparation_id: str
    usenet_id: int


class ProviderPreparationRepository:
    def __init__(self, database):
        self._database = database

    @staticmethod
    def _partition(partition: bytes) -> str:
        if not isinstance(partition, bytes) or len(partition) != 32:
            raise ValueError("owner configuration partition must contain 32 bytes")
        return partition.hex()

    @staticmethod
    def _binding(
        *,
        provider_configuration_id: str,
        credential_fingerprint: str,
        candidate_id: str,
        locator_id: str,
        artifact_grant_id: str | None,
        selection_json: str,
        mutation_idempotency_key: str,
        provider_kind: str,
    ) -> tuple[dict[str, str | None], str]:
        try:
            values = {
                "provider_configuration_id": str(uuid.UUID(provider_configuration_id)),
                "candidate_id": str(uuid.UUID(candidate_id)),
                "locator_id": str(uuid.UUID(locator_id)),
                "artifact_grant_id": (
                    str(uuid.UUID(artifact_grant_id))
                    if artifact_grant_id is not None
                    else None
                ),
            }
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("invalid provider preparation") from exc
        if (
            not isinstance(credential_fingerprint, str)
            or len(credential_fingerprint) != 64
            or not isinstance(mutation_idempotency_key, str)
            or len(mutation_idempotency_key) != 64
            or provider_kind
            not in {
                "stremthru_newz",
                "torbox_usenet",
                "nzbdav",
                "altmount",
            }
        ):
            raise ValueError("invalid provider preparation")
        return values, selection_json

    @staticmethod
    def _binding_conflicts(
        row,
        values: dict[str, str | None],
        *,
        credential_fingerprint: str,
        provider_kind: str,
        selection_json: str,
    ) -> bool:
        return (
            row["provider_configuration_id"] != values["provider_configuration_id"]
            or row["credential_fingerprint"] != credential_fingerprint
            or row["provider_kind"] != provider_kind
            or row["candidate_id"] != values["candidate_id"]
            or row["locator_id"] != values["locator_id"]
            or row["artifact_grant_id"] != values["artifact_grant_id"]
            or row["selection_json"] != selection_json
        )

    async def garbage_collect(
        self,
        *,
        now: float | None = None,
        limit: int = _MAX_GC_BATCH,
    ) -> int:
        """Delete terminal ledgers only after every playback capability expires."""
        current_time = time.time() if now is None else now
        params = {"now": current_time, "limit": limit}
        rows = await self._database.fetch_all(
            """
            DELETE FROM provider_preparations
            WHERE preparation_id IN (
                SELECT provider.preparation_id
                FROM provider_preparations AS provider
                WHERE provider.state = 'terminal'
                  AND provider.refcount = 0
                  AND provider.gc_after_at IS NOT NULL
                  AND provider.gc_after_at < :now
                  AND (
                      provider.provider_kind <> 'torbox_usenet'
                      OR provider.cleanup_state IN ('not_required', 'complete')
                  )
                ORDER BY provider.gc_after_at, provider.preparation_id
                LIMIT :limit
            )
              AND state = 'terminal'
              AND refcount = 0
              AND gc_after_at IS NOT NULL
              AND gc_after_at < :now
              AND (
                  provider_kind <> 'torbox_usenet'
                  OR cleanup_state IN ('not_required', 'complete')
              )
            RETURNING preparation_id
            """,
            params,
            force_primary=True,
        )
        return len(rows)

    async def get_existing(
        self,
        *,
        owner_configuration_partition: bytes,
        provider_configuration_id: str,
        credential_fingerprint: str,
        candidate_id: str,
        locator_id: str,
        artifact_grant_id: str | None,
        selection_json: str,
        mutation_idempotency_key: str,
        provider_kind: str = "stremthru_newz",
    ) -> ProviderPreparation | None:
        """Read an exact durable mutation binding without creating one."""
        values, selection_json = self._binding(
            provider_configuration_id=provider_configuration_id,
            credential_fingerprint=credential_fingerprint,
            candidate_id=candidate_id,
            locator_id=locator_id,
            artifact_grant_id=artifact_grant_id,
            selection_json=selection_json,
            mutation_idempotency_key=mutation_idempotency_key,
            provider_kind=provider_kind,
        )
        row = await self._database.fetch_one(
            """
            SELECT preparation_id, state, provider_payload_json, created_at,
                   provider_configuration_id, credential_fingerprint,
                   provider_kind, candidate_id, locator_id,
                   artifact_grant_id, selection_json
            FROM provider_preparations
            WHERE mutation_idempotency_key = :mutation_idempotency_key
              AND owner_configuration_partition = :partition
            """,
            {
                "mutation_idempotency_key": mutation_idempotency_key,
                "partition": self._partition(owner_configuration_partition),
            },
            force_primary=True,
        )
        if row is None:
            return None
        if self._binding_conflicts(
            row,
            values,
            credential_fingerprint=credential_fingerprint,
            provider_kind=provider_kind,
            selection_json=selection_json,
        ):
            raise ValueError("provider preparation binding conflicts")
        return self._row(row)

    async def get_or_create(
        self,
        *,
        owner_configuration_partition: bytes,
        provider_configuration_id: str,
        credential_fingerprint: str,
        candidate_id: str,
        locator_id: str,
        artifact_grant_id: str | None,
        selection_json: str,
        mutation_idempotency_key: str,
        provider_kind: str = "stremthru_newz",
        now: float | None = None,
    ) -> tuple[ProviderPreparation, bool]:
        values, selection_json = self._binding(
            provider_configuration_id=provider_configuration_id,
            credential_fingerprint=credential_fingerprint,
            candidate_id=candidate_id,
            locator_id=locator_id,
            artifact_grant_id=artifact_grant_id,
            selection_json=selection_json,
            mutation_idempotency_key=mutation_idempotency_key,
            provider_kind=provider_kind,
        )
        current_time = time.time() if now is None else now
        record_id = str(uuid.uuid4())
        await self._database.execute(
            """
            INSERT INTO provider_preparations (
                preparation_id, owner_configuration_partition, provider_configuration_id,
                credential_fingerprint, provider_kind, candidate_id, locator_id,
                artifact_grant_id, selection_json, mutation_idempotency_key,
                provider_payload_json, state, cleanup_state, created_at, updated_at
            ) VALUES (
                :preparation_id, :partition, :provider_configuration_id,
                :credential_fingerprint, :provider_kind, :candidate_id, :locator_id,
                :artifact_grant_id, :selection_json, :mutation_idempotency_key,
                '{}', 'mutation_pending',
                CASE
                    WHEN :provider_kind = 'torbox_usenet'
                    THEN 'ownership_pending'
                    ELSE 'not_required'
                END,
                :now, :now
            ) ON CONFLICT (mutation_idempotency_key) DO NOTHING
            """,
            {
                **values,
                "preparation_id": record_id,
                "partition": self._partition(owner_configuration_partition),
                "credential_fingerprint": credential_fingerprint,
                "provider_kind": provider_kind,
                "selection_json": selection_json,
                "mutation_idempotency_key": mutation_idempotency_key,
                "now": current_time,
            },
            force_primary=True,
        )
        row = await self._database.fetch_one(
            """
            SELECT preparation_id, state, provider_payload_json, created_at,
                   provider_configuration_id, credential_fingerprint,
                   provider_kind, candidate_id, locator_id,
                   artifact_grant_id, selection_json
            FROM provider_preparations
            WHERE mutation_idempotency_key = :mutation_idempotency_key
              AND owner_configuration_partition = :partition
            """,
            {
                "mutation_idempotency_key": mutation_idempotency_key,
                "partition": self._partition(owner_configuration_partition),
            },
            force_primary=True,
        )
        if row is None:
            raise RuntimeError("provider preparation was not persisted")
        if self._binding_conflicts(
            row,
            values,
            credential_fingerprint=credential_fingerprint,
            provider_kind=provider_kind,
            selection_json=selection_json,
        ):
            raise ValueError("provider preparation binding conflicts")
        return self._row(row), row["preparation_id"] == record_id

    async def record_ambiguous_submission(
        self,
        preparation_id: str,
        *,
        owner_configuration_partition: bytes,
        provider_kind: str,
        now: float | None = None,
    ) -> None:
        """Retain a permanent tombstone when safe reconciliation is impossible."""
        if provider_kind not in {"stremthru_newz", "torbox_usenet"}:
            raise ValueError("invalid ambiguous provider submission")
        current_time = time.time() if now is None else now
        values = {
            "preparation_id": str(uuid.UUID(preparation_id)),
            "partition": self._partition(owner_configuration_partition),
            "provider_kind": provider_kind,
            "payload": '{"status":"ambiguous_submission"}',
            "now": current_time,
        }
        updated = await self._database.fetch_one(
            """
            UPDATE provider_preparations
            SET provider_payload_json = :payload, state = 'terminal',
                updated_at = :now, terminal_at = :now, gc_after_at = NULL
            WHERE preparation_id = :preparation_id
              AND owner_configuration_partition = :partition
              AND provider_kind = :provider_kind
              AND state = 'mutation_pending'
            RETURNING preparation_id
            """,
            values,
            force_primary=True,
        )
        if updated is not None:
            return
        current = await self._database.fetch_one(
            """
            SELECT state, provider_payload_json
            FROM provider_preparations
            WHERE preparation_id = :preparation_id
              AND owner_configuration_partition = :partition
              AND provider_kind = :provider_kind
            """,
            {
                "preparation_id": values["preparation_id"],
                "partition": values["partition"],
                "provider_kind": provider_kind,
            },
            force_primary=True,
        )
        if (
            current is None
            or current["state"] != "terminal"
            or current["provider_payload_json"] != values["payload"]
        ):
            raise ValueError("ambiguous provider submission conflicts")

    async def claim_altmount_retry(
        self,
        preparation_id: str,
        *,
        owner_configuration_partition: bytes,
        deadline_seconds: int,
        now: float | None = None,
    ) -> str | None:
        """Claim a byte-identical retry, including one final deadline attempt."""
        current_time = time.time() if now is None else now
        values = {
            "preparation_id": str(uuid.UUID(preparation_id)),
            "partition": self._partition(owner_configuration_partition),
        }
        row = await self._database.fetch_one(
            """
            SELECT provider_payload_json, created_at, updated_at
            FROM provider_preparations
            WHERE preparation_id = :preparation_id
              AND owner_configuration_partition = :partition
              AND provider_kind = 'altmount'
              AND state = 'mutation_pending'
            """,
            values,
            force_primary=True,
        )
        if row is None or current_time - row["updated_at"] < _ALTMOUNT_RETRY_SECONDS:
            return None
        payload = self._payload(row["provider_payload_json"])
        retry_count = payload.get("retry_count", 0)
        last_retry_at = payload.get("last_retry_at")
        final_retry = payload.get("final_retry", False)
        if (
            isinstance(retry_count, bool)
            or not isinstance(retry_count, int)
            or not 0 <= retry_count < MAX_SIGNED_BIGINT
            or (
                last_retry_at is not None
                and (
                    isinstance(last_retry_at, bool)
                    or not isinstance(last_retry_at, (int, float))
                    or not math.isfinite(last_retry_at)
                    or last_retry_at < 0
                )
            )
            or not isinstance(final_retry, bool)
            or (retry_count == 0) != (last_retry_at is None)
        ):
            raise ValueError("AltMount retry state is corrupt")
        if final_retry:
            return None
        phase = (
            "final" if current_time - row["created_at"] >= deadline_seconds else "retry"
        )
        replacement = orjson.dumps(
            {
                "retry_count": retry_count + 1,
                "last_retry_at": current_time,
                **({"final_retry": True} if phase == "final" else {}),
            }
        ).decode()
        updated = await self._database.fetch_one(
            """
            UPDATE provider_preparations
            SET provider_payload_json = :payload, updated_at = :now
            WHERE preparation_id = :preparation_id
              AND owner_configuration_partition = :partition
              AND provider_kind = 'altmount'
              AND state = 'mutation_pending'
              AND provider_payload_json = :previous_payload
              AND updated_at = :previous_updated_at
            RETURNING preparation_id
            """,
            {
                **values,
                "payload": replacement,
                "previous_payload": row["provider_payload_json"],
                "previous_updated_at": row["updated_at"],
                "now": current_time,
            },
            force_primary=True,
        )
        return phase if updated is not None else None

    async def record_altmount_selection(
        self,
        preparation_id: str,
        *,
        owner_configuration_partition: bytes,
        virtual_path: str,
        now: float | None = None,
    ) -> None:
        """Seal a validated native AltMount result without storing its download key."""
        current_time = time.time() if now is None else now
        payload = orjson.dumps(
            {
                "virtual_path": virtual_path,
                "status": "selected",
            }
        ).decode()
        values = {
            "preparation_id": str(uuid.UUID(preparation_id)),
            "partition": self._partition(owner_configuration_partition),
        }
        updated = await self._database.fetch_one(
            """
            UPDATE provider_preparations
            SET provider_payload_json = :payload, state = 'terminal',
                updated_at = :now, terminal_at = :now,
                gc_after_at = :gc_after_at
            WHERE preparation_id = :preparation_id
              AND owner_configuration_partition = :partition
              AND provider_kind = 'altmount'
              AND state = 'mutation_pending'
            RETURNING preparation_id
            """,
            {
                **values,
                "payload": payload,
                "now": current_time,
                "gc_after_at": current_time + _TERMINAL_RETENTION_SECONDS,
            },
            force_primary=True,
        )
        if updated is not None:
            return
        current = await self._database.fetch_one(
            """
            SELECT state, provider_payload_json
            FROM provider_preparations
            WHERE preparation_id = :preparation_id
              AND owner_configuration_partition = :partition
              AND provider_kind = 'altmount'
            """,
            values,
            force_primary=True,
        )
        if (
            current is None
            or current["state"] != "terminal"
            or current["provider_payload_json"] != payload
        ):
            raise ValueError("AltMount selection conflicts")

    async def record_altmount_failure(
        self,
        preparation_id: str,
        *,
        owner_configuration_partition: bytes,
        status: str,
        error_code: str,
        now: float | None = None,
    ) -> None:
        if (
            status not in {"failed", "invalid"}
            or not isinstance(error_code, str)
            or not 1 <= len(error_code) <= 128
            or any(ord(character) < 32 for character in error_code)
        ):
            raise ValueError("invalid AltMount failure")
        current_time = time.time() if now is None else now
        payload = orjson.dumps({"status": status, "error_code": error_code}).decode()
        values = {
            "preparation_id": str(uuid.UUID(preparation_id)),
            "partition": self._partition(owner_configuration_partition),
        }
        updated = await self._database.fetch_one(
            """
            UPDATE provider_preparations
            SET provider_payload_json = :payload, state = 'terminal',
                updated_at = :now, terminal_at = :now,
                gc_after_at = :gc_after_at
            WHERE preparation_id = :preparation_id
              AND owner_configuration_partition = :partition
              AND provider_kind = 'altmount'
              AND state = 'mutation_pending'
            RETURNING preparation_id
            """,
            {
                **values,
                "payload": payload,
                "now": current_time,
                "gc_after_at": current_time + _TERMINAL_RETENTION_SECONDS,
            },
            force_primary=True,
        )
        if updated is not None:
            return
        current = await self._database.fetch_one(
            """
            SELECT state, provider_payload_json
            FROM provider_preparations
            WHERE preparation_id = :preparation_id
              AND owner_configuration_partition = :partition
              AND provider_kind = 'altmount'
            """,
            values,
            force_primary=True,
        )
        if (
            current is None
            or current["state"] != "terminal"
            or current["provider_payload_json"] != payload
        ):
            raise ValueError("AltMount failure conflicts")

    async def begin_nzbdav_resubmission(
        self,
        preparation_id: str,
        *,
        owner_configuration_partition: bytes,
        now: float | None = None,
    ) -> bool:
        """Seal the sole post-reconciliation NzbDAV resubmission."""
        current_time = time.time() if now is None else now
        values = {
            "preparation_id": str(uuid.UUID(preparation_id)),
            "partition": self._partition(owner_configuration_partition),
        }
        row = await self._database.fetch_one(
            """
            SELECT provider_payload_json
            FROM provider_preparations
            WHERE preparation_id = :preparation_id
              AND owner_configuration_partition = :partition
              AND provider_kind = 'nzbdav'
              AND state = 'mutation_pending'
            """,
            values,
            force_primary=True,
        )
        if row is None:
            return False
        payload = self._payload(row["provider_payload_json"])
        if payload:
            return False
        updated = await self._database.fetch_one(
            """
            UPDATE provider_preparations
            SET provider_payload_json =
                    '{"status":"resubmit_mutation_pending"}',
                updated_at = :now
            WHERE preparation_id = :preparation_id
              AND owner_configuration_partition = :partition
              AND provider_kind = 'nzbdav'
              AND state = 'mutation_pending'
              AND provider_payload_json = '{}'
            RETURNING preparation_id
            """,
            {**values, "now": current_time},
            force_primary=True,
        )
        return updated is not None

    async def discard_rejected_nzbdav_submission(
        self,
        preparation_id: str,
        *,
        owner_configuration_partition: bytes,
    ) -> None:
        """Forget only an unattached initial intent rejected before mutation."""
        removed = await self._database.fetch_one(
            """
            DELETE FROM provider_preparations
            WHERE preparation_id = :preparation_id
              AND owner_configuration_partition = :partition
              AND provider_kind = 'nzbdav'
              AND state = 'mutation_pending'
              AND provider_payload_json = '{}'
              AND refcount = 0
            RETURNING preparation_id
            """,
            {
                "preparation_id": str(uuid.UUID(preparation_id)),
                "partition": self._partition(owner_configuration_partition),
            },
            force_primary=True,
        )
        if removed is None:
            raise ValueError("NzbDAV rejected submission conflicts")

    async def restore_rejected_nzbdav_resubmission(
        self,
        preparation_id: str,
        *,
        owner_configuration_partition: bytes,
        now: float | None = None,
    ) -> None:
        """Restore exact-absence reconciliation after a rejected re-add."""
        updated = await self._database.fetch_one(
            """
            UPDATE provider_preparations
            SET provider_payload_json = '{}', updated_at = :now
            WHERE preparation_id = :preparation_id
              AND owner_configuration_partition = :partition
              AND provider_kind = 'nzbdav'
              AND state = 'mutation_pending'
              AND provider_payload_json =
                    '{"status":"resubmit_mutation_pending"}'
            RETURNING preparation_id
            """,
            {
                "preparation_id": str(uuid.UUID(preparation_id)),
                "partition": self._partition(owner_configuration_partition),
                "now": time.time() if now is None else now,
            },
            force_primary=True,
        )
        if updated is None:
            raise ValueError("NzbDAV rejected resubmission conflicts")

    async def record_submission(
        self,
        preparation_id: str,
        *,
        owner_configuration_partition: bytes,
        remote_id: str,
        remote_hash: str,
        status: str,
        ownership: str,
        now: float | None = None,
    ) -> None:
        if ownership not in _OWNERSHIP_STATES or not all(
            isinstance(value, str) and 1 <= len(value) <= 512
            for value in (remote_id, remote_hash, status)
        ):
            raise ValueError("invalid provider submission")
        current_time = time.time() if now is None else now
        updated = await self._database.fetch_one(
            """
            UPDATE provider_preparations
            SET provider_payload_json = :payload, state = 'submitted', updated_at = :now,
                cleanup_state = CASE
                    WHEN provider_kind = 'torbox_usenet' AND :ownership = 'created'
                    THEN 'required'
                    ELSE 'not_required'
                END
            WHERE preparation_id = :preparation_id AND owner_configuration_partition = :partition
              AND state = 'mutation_pending'
            RETURNING preparation_id
            """,
            {
                "preparation_id": str(uuid.UUID(preparation_id)),
                "partition": self._partition(owner_configuration_partition),
                "payload": orjson.dumps(
                    {
                        "remote_id": remote_id,
                        "remote_hash": remote_hash,
                        "status": status,
                        "ownership": ownership,
                        "missing_count": 0,
                    }
                ).decode(),
                "ownership": ownership,
                "now": current_time,
            },
            force_primary=True,
        )
        if updated is not None:
            return
        current = await self._database.fetch_one(
            """
            SELECT state, provider_payload_json
            FROM provider_preparations
            WHERE preparation_id = :preparation_id
              AND owner_configuration_partition = :partition
            """,
            {
                "preparation_id": str(uuid.UUID(preparation_id)),
                "partition": self._partition(owner_configuration_partition),
            },
            force_primary=True,
        )
        if current is None or current["state"] != "submitted":
            raise ValueError("provider submission is unavailable")
        payload = self._payload(current["provider_payload_json"])
        if (
            payload.get("remote_id") != remote_id
            or payload.get("remote_hash") != remote_hash
            or payload.get("ownership") not in _OWNERSHIP_STATES
        ):
            raise ValueError("provider submission conflicts")
        if payload["ownership"] == "unknown" and ownership == "created":
            replacement = {**payload, "ownership": "created"}
            await self._database.execute(
                """
                UPDATE provider_preparations
                SET provider_payload_json = :payload, updated_at = :now,
                    cleanup_state = CASE
                        WHEN provider_kind = 'torbox_usenet'
                        THEN 'required'
                        ELSE cleanup_state
                    END
                WHERE preparation_id = :preparation_id
                  AND owner_configuration_partition = :partition
                  AND state = 'submitted'
                  AND provider_payload_json = :previous_payload
                """,
                {
                    "preparation_id": str(uuid.UUID(preparation_id)),
                    "partition": self._partition(owner_configuration_partition),
                    "payload": orjson.dumps(replacement).decode(),
                    "previous_payload": current["provider_payload_json"],
                    "now": current_time,
                },
                force_primary=True,
            )

    async def claim_torbox_cleanup(
        self,
        *,
        owner_configuration_partition: bytes,
        provider_configuration_id: str,
        credential_fingerprint: str,
        now: float | None = None,
    ) -> TorBoxCleanupTarget | None:
        """Claim one expired owned job under its exact account binding."""
        try:
            provider_id = str(uuid.UUID(provider_configuration_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("invalid TorBox cleanup binding") from exc
        if (
            not isinstance(credential_fingerprint, str)
            or len(credential_fingerprint) != 64
        ):
            raise ValueError("invalid TorBox cleanup binding")
        current_time = time.time() if now is None else now
        values = {
            "partition": self._partition(owner_configuration_partition),
            "provider_configuration_id": provider_id,
            "credential_fingerprint": credential_fingerprint,
            "now": current_time,
            "retry_before": current_time - _CLEANUP_RETRY_SECONDS,
        }
        row = await self._database.fetch_one(
            """
            UPDATE provider_preparations
            SET cleanup_state = 'in_progress', updated_at = :now
            WHERE preparation_id = (
                SELECT provider.preparation_id
                FROM provider_preparations AS provider
                WHERE provider.owner_configuration_partition = :partition
                  AND provider.provider_configuration_id =
                      :provider_configuration_id
                  AND provider.credential_fingerprint = :credential_fingerprint
                  AND provider.provider_kind = 'torbox_usenet'
                  AND provider.state = 'terminal'
                  AND provider.refcount = 0
                  AND provider.gc_after_at IS NOT NULL
                  AND provider.gc_after_at < :now
                  AND (
                      provider.cleanup_state = 'required'
                      OR (
                          provider.cleanup_state = 'in_progress'
                          AND provider.updated_at < :retry_before
                      )
                  )
                ORDER BY provider.gc_after_at, provider.preparation_id
                LIMIT 1
            )
              AND refcount = 0
              AND (
                  cleanup_state = 'required'
                  OR (
                      cleanup_state = 'in_progress'
                      AND updated_at < :retry_before
                  )
              )
            RETURNING preparation_id, provider_payload_json
            """,
            values,
            force_primary=True,
        )
        if row is None:
            return None
        payload = self._payload(row["provider_payload_json"])
        remote_id = payload.get("remote_id")
        if (
            payload.get("ownership") != "created"
            or not isinstance(remote_id, str)
            or not remote_id.isascii()
            or not remote_id.isdigit()
            or str(int(remote_id)) != remote_id
            or int(remote_id) > MAX_SIGNED_BIGINT
        ):
            raise ValueError("TorBox cleanup authority is corrupt")
        return TorBoxCleanupTarget(
            str(uuid.UUID(row["preparation_id"])),
            int(remote_id),
        )

    async def record_torbox_cleanup_complete(
        self,
        preparation_id: str,
        *,
        owner_configuration_partition: bytes,
        usenet_id: int,
        now: float | None = None,
    ) -> None:
        """Retire cleanup authority only after the exact remote delete succeeds."""
        if (
            isinstance(usenet_id, bool)
            or not isinstance(usenet_id, int)
            or not 0 <= usenet_id <= MAX_SIGNED_BIGINT
        ):
            raise ValueError("invalid TorBox cleanup target")
        values = {
            "preparation_id": str(uuid.UUID(preparation_id)),
            "partition": self._partition(owner_configuration_partition),
        }
        row = await self._database.fetch_one(
            """
            SELECT provider_payload_json
            FROM provider_preparations
            WHERE preparation_id = :preparation_id
              AND owner_configuration_partition = :partition
              AND provider_kind = 'torbox_usenet'
              AND state = 'terminal'
              AND cleanup_state = 'in_progress'
            """,
            values,
            force_primary=True,
        )
        if row is None:
            raise ValueError("TorBox cleanup target is unavailable")
        payload = self._payload(row["provider_payload_json"])
        if payload.get("ownership") != "created" or payload.get("remote_id") != str(
            usenet_id
        ):
            raise ValueError("TorBox cleanup authority conflicts")
        current_time = time.time() if now is None else now
        updated = await self._database.fetch_one(
            """
            UPDATE provider_preparations
            SET cleanup_state = 'complete', updated_at = :now,
                gc_after_at = :now
            WHERE preparation_id = :preparation_id
              AND owner_configuration_partition = :partition
              AND provider_kind = 'torbox_usenet'
              AND state = 'terminal'
              AND cleanup_state = 'in_progress'
              AND provider_payload_json = :payload
            RETURNING preparation_id
            """,
            {
                **values,
                "payload": row["provider_payload_json"],
                "now": current_time,
            },
            force_primary=True,
        )
        if updated is None:
            raise ValueError("TorBox cleanup target is unavailable")

    async def begin_stremthru_resubmission(
        self,
        preparation_id: str,
        *,
        owner_configuration_partition: bytes,
        now: float | None = None,
    ) -> bool:
        """Seal the one permitted re-add before the second upstream mutation."""
        current_time = time.time() if now is None else now
        row = await self._database.fetch_one(
            """
            SELECT provider_payload_json
            FROM provider_preparations
            WHERE preparation_id = :preparation_id
              AND owner_configuration_partition = :partition
              AND provider_kind = 'stremthru_newz'
              AND state = 'submitted'
            """,
            {
                "preparation_id": str(uuid.UUID(preparation_id)),
                "partition": self._partition(owner_configuration_partition),
            },
            force_primary=True,
        )
        if row is None:
            return False
        payload = self._payload(row["provider_payload_json"])
        if (
            payload.get("status") != "remote_missing"
            or payload.get("missing_count") != 1
            or payload.get("ownership") not in _OWNERSHIP_STATES
        ):
            return False
        pending = {
            **payload,
            "status": "readd_mutation_pending",
        }
        updated = await self._database.fetch_one(
            """
            UPDATE provider_preparations
            SET provider_payload_json = :payload, updated_at = :now
            WHERE preparation_id = :preparation_id
              AND owner_configuration_partition = :partition
              AND provider_kind = 'stremthru_newz'
              AND state = 'submitted'
              AND provider_payload_json = :previous_payload
            RETURNING preparation_id
            """,
            {
                "preparation_id": str(uuid.UUID(preparation_id)),
                "partition": self._partition(owner_configuration_partition),
                "payload": orjson.dumps(pending).decode(),
                "previous_payload": row["provider_payload_json"],
                "now": current_time,
            },
            force_primary=True,
        )
        return updated is not None

    async def discard_rejected_stremthru_submission(
        self,
        preparation_id: str,
        *,
        owner_configuration_partition: bytes,
    ) -> None:
        """Forget an empty intent only when the bridge proved no request was accepted."""
        removed = await self._database.fetch_one(
            """
            DELETE FROM provider_preparations
            WHERE preparation_id = :preparation_id
              AND owner_configuration_partition = :partition
              AND provider_kind = 'stremthru_newz'
              AND state = 'mutation_pending'
              AND provider_payload_json = '{}'
            RETURNING preparation_id
            """,
            {
                "preparation_id": str(uuid.UUID(preparation_id)),
                "partition": self._partition(owner_configuration_partition),
            },
            force_primary=True,
        )
        if removed is None:
            raise ValueError("StremThru rejected submission conflicts")

    async def restore_rejected_stremthru_resubmission(
        self,
        preparation_id: str,
        *,
        owner_configuration_partition: bytes,
        now: float | None = None,
    ) -> None:
        """Restore the sole tombstone when a re-add was rejected before mutation."""
        values = {
            "preparation_id": str(uuid.UUID(preparation_id)),
            "partition": self._partition(owner_configuration_partition),
        }
        row = await self._database.fetch_one(
            """
            SELECT provider_payload_json
            FROM provider_preparations
            WHERE preparation_id = :preparation_id
              AND owner_configuration_partition = :partition
              AND provider_kind = 'stremthru_newz'
              AND state = 'submitted'
            """,
            values,
            force_primary=True,
        )
        if row is None:
            raise ValueError("StremThru rejected resubmission conflicts")
        previous = self._payload(row["provider_payload_json"])
        if (
            previous.get("status") != "readd_mutation_pending"
            or previous.get("missing_count") != 1
            or previous.get("ownership") not in _OWNERSHIP_STATES
        ):
            raise ValueError("StremThru rejected resubmission conflicts")
        restored = {**previous, "status": "remote_missing"}
        updated = await self._database.fetch_one(
            """
            UPDATE provider_preparations
            SET provider_payload_json = :payload, updated_at = :now
            WHERE preparation_id = :preparation_id
              AND owner_configuration_partition = :partition
              AND provider_kind = 'stremthru_newz'
              AND state = 'submitted'
              AND provider_payload_json = :previous_payload
            RETURNING preparation_id
            """,
            {
                **values,
                "payload": orjson.dumps(restored).decode(),
                "previous_payload": row["provider_payload_json"],
                "now": time.time() if now is None else now,
            },
            force_primary=True,
        )
        if updated is None:
            raise ValueError("StremThru rejected resubmission conflicts")

    async def record_stremthru_resubmission(
        self,
        preparation_id: str,
        *,
        owner_configuration_partition: bytes,
        remote_id: str,
        remote_hash: str,
        status: str,
        now: float | None = None,
    ) -> None:
        if not all(
            isinstance(value, str) and 1 <= len(value) <= 512
            for value in (remote_id, remote_hash, status)
        ):
            raise ValueError("invalid provider resubmission")
        current_time = time.time() if now is None else now
        values = {
            "preparation_id": str(uuid.UUID(preparation_id)),
            "partition": self._partition(owner_configuration_partition),
        }
        existing = await self._database.fetch_one(
            """
            SELECT provider_payload_json
            FROM provider_preparations
            WHERE preparation_id = :preparation_id
              AND owner_configuration_partition = :partition
              AND provider_kind = 'stremthru_newz'
              AND state = 'submitted'
            """,
            values,
            force_primary=True,
        )
        if existing is None:
            raise ValueError("provider resubmission is unavailable")
        previous = self._payload(existing["provider_payload_json"])
        if (
            previous.get("status") != "readd_mutation_pending"
            or previous.get("missing_count") != 1
        ):
            raise ValueError("provider resubmission is unavailable")
        updated = await self._database.fetch_one(
            """
            UPDATE provider_preparations
            SET provider_payload_json = :payload, updated_at = :now
            WHERE preparation_id = :preparation_id
              AND owner_configuration_partition = :partition
              AND provider_kind = 'stremthru_newz'
              AND state = 'submitted'
              AND provider_payload_json = :previous_payload
            RETURNING preparation_id
            """,
            {
                **values,
                "payload": orjson.dumps(
                    {
                        "remote_id": remote_id,
                        "remote_hash": remote_hash,
                        "status": status,
                        "ownership": "created",
                        "missing_count": 1,
                    }
                ).decode(),
                "previous_payload": existing["provider_payload_json"],
                "now": current_time,
            },
            force_primary=True,
        )
        if updated is None:
            raise ValueError("provider resubmission is unavailable")

    async def record_stremthru_missing(
        self,
        preparation_id: str,
        *,
        owner_configuration_partition: bytes,
        now: float | None = None,
    ) -> bool:
        """Tombstone one missing ID and return whether the sole re-add remains."""
        current_time = time.time() if now is None else now
        values = {
            "preparation_id": str(uuid.UUID(preparation_id)),
            "partition": self._partition(owner_configuration_partition),
        }
        row = await self._database.fetch_one(
            """
            SELECT provider_payload_json
            FROM provider_preparations
            WHERE preparation_id = :preparation_id
              AND owner_configuration_partition = :partition
              AND provider_kind = 'stremthru_newz'
              AND state = 'submitted'
            """,
            values,
            force_primary=True,
        )
        if row is None:
            return False
        payload = self._payload(row["provider_payload_json"])
        missing_count = payload.get("missing_count")
        if (
            isinstance(missing_count, bool)
            or not isinstance(missing_count, int)
            or missing_count not in {0, 1}
            or payload.get("ownership") not in _OWNERSHIP_STATES
        ):
            raise ValueError("provider preparation is corrupt")
        retry = missing_count == 0
        replacement = {
            "status": "remote_missing" if retry else "remote_item_missing",
            "ownership": payload["ownership"],
            "missing_count": missing_count + int(retry),
        }
        state = "submitted" if retry else "terminal"
        updated = await self._database.fetch_one(
            """
            UPDATE provider_preparations
            SET provider_payload_json = :payload, state = :state,
                updated_at = :now, last_polled_at = :now,
                terminal_at = CASE WHEN :state = 'terminal' THEN :now ELSE NULL END,
                gc_after_at = CASE
                    WHEN :state = 'terminal' THEN :gc_after_at
                    ELSE NULL
                END
            WHERE preparation_id = :preparation_id
              AND owner_configuration_partition = :partition
              AND provider_kind = 'stremthru_newz'
              AND state = 'submitted'
              AND provider_payload_json = :previous_payload
            RETURNING preparation_id
            """,
            {
                **values,
                "payload": orjson.dumps(replacement).decode(),
                "previous_payload": row["provider_payload_json"],
                "state": state,
                "now": current_time,
                "gc_after_at": current_time + _TERMINAL_RETENTION_SECONDS,
            },
            force_primary=True,
        )
        if updated is not None:
            return retry
        current = await self._database.fetch_one(
            """
            SELECT state, provider_payload_json
            FROM provider_preparations
            WHERE preparation_id = :preparation_id
              AND owner_configuration_partition = :partition
              AND provider_kind = 'stremthru_newz'
            """,
            values,
            force_primary=True,
        )
        if current is None:
            return False
        current_payload = self._payload(current["provider_payload_json"])
        return (
            current["state"] == "submitted"
            and current_payload.get("status") == "remote_missing"
            and current_payload.get("missing_count") == 1
        )

    async def record_sab_absence(
        self,
        preparation_id: str,
        *,
        owner_configuration_partition: bytes,
        now: float | None = None,
    ) -> str:
        """Require two consecutive exact SAB absences before terminalizing."""
        current_time = time.time() if now is None else now
        values = {
            "preparation_id": str(uuid.UUID(preparation_id)),
            "partition": self._partition(owner_configuration_partition),
        }
        row = await self._database.fetch_one(
            """
            SELECT state, provider_payload_json
            FROM provider_preparations
            WHERE preparation_id = :preparation_id
              AND owner_configuration_partition = :partition
              AND provider_kind = 'nzbdav'
            """,
            values,
            force_primary=True,
        )
        if row is None:
            raise ValueError("SAB preparation is unavailable")
        payload = self._payload(row["provider_payload_json"])
        status = _provider_status(payload.get("status"))
        if row["state"] == "terminal" and status == "remote_item_missing":
            return "terminal"
        missing_count = payload.get("missing_count")
        if (
            row["state"] != "submitted"
            or not isinstance(payload.get("remote_id"), str)
            or not isinstance(payload.get("remote_hash"), str)
            or payload.get("ownership") not in _OWNERSHIP_STATES
            or isinstance(missing_count, bool)
            or missing_count not in {0, 1}
        ):
            raise ValueError("SAB preparation is corrupt")
        if missing_count == 0:
            if (
                status in {"remote_missing", "remote_item_missing"}
                or "previous_status" in payload
            ):
                raise ValueError("SAB preparation is corrupt")
            replacement = {
                **payload,
                "status": "remote_missing",
                "missing_count": 1,
                "previous_status": status,
                "missing_observed_at": current_time,
            }
            state, outcome = "submitted", "pending"
        else:
            missing_observed_at = payload.get("missing_observed_at")
            if (
                status != "remote_missing"
                or isinstance(missing_observed_at, bool)
                or not isinstance(missing_observed_at, (int, float))
                or not math.isfinite(missing_observed_at)
                or missing_observed_at < 0
            ):
                raise ValueError("SAB preparation is corrupt")
            _provider_status(payload.get("previous_status"))
            if current_time - missing_observed_at < _SAB_ABSENCE_CONFIRMATION_SECONDS:
                return "pending"
            replacement = {
                key: value
                for key, value in payload.items()
                if key not in {"previous_status", "missing_observed_at"}
            }
            replacement["status"] = "remote_item_missing"
            state, outcome = "terminal", "terminal"
        updated = await self._database.fetch_one(
            """
            UPDATE provider_preparations
            SET provider_payload_json = :payload, state = :state,
                updated_at = :now, last_polled_at = :now,
                terminal_at = CASE
                    WHEN :state = 'terminal' THEN :now
                    ELSE NULL
                END,
                gc_after_at = CASE
                    WHEN :state = 'terminal' THEN :gc_after_at
                    ELSE NULL
                END
            WHERE preparation_id = :preparation_id
              AND owner_configuration_partition = :partition
              AND provider_kind = 'nzbdav'
              AND state = 'submitted'
              AND provider_payload_json = :previous_payload
            RETURNING preparation_id
            """,
            {
                **values,
                "payload": orjson.dumps(replacement).decode(),
                "previous_payload": row["provider_payload_json"],
                "state": state,
                "now": current_time,
                "gc_after_at": current_time + _TERMINAL_RETENTION_SECONDS,
            },
            force_primary=True,
        )
        if updated is not None:
            return outcome
        current = await self._database.fetch_one(
            """
            SELECT state, provider_payload_json
            FROM provider_preparations
            WHERE preparation_id = :preparation_id
              AND owner_configuration_partition = :partition
              AND provider_kind = 'nzbdav'
            """,
            values,
            force_primary=True,
        )
        if current is not None:
            current_payload = self._payload(current["provider_payload_json"])
            current_status = _provider_status(current_payload.get("status"))
            if (
                current["state"] == "submitted"
                and current_status not in {"remote_missing", "remote_item_missing"}
                and current_payload.get("missing_count") == 0
            ):
                return "pending"
            if (
                current["state"] == "submitted"
                and current_status == "remote_missing"
                and current_payload.get("missing_count") == 1
            ):
                return "pending"
            if (
                current["state"] == "terminal"
                and current_payload.get("status") == "remote_item_missing"
            ):
                return "terminal"
        raise ValueError("SAB preparation is unavailable")

    async def clear_sab_absence(
        self,
        preparation_id: str,
        *,
        owner_configuration_partition: bytes,
        now: float | None = None,
    ) -> bool:
        """Clear a single SAB absence after the exact bound job reappears."""
        current_time = time.time() if now is None else now
        values = {
            "preparation_id": str(uuid.UUID(preparation_id)),
            "partition": self._partition(owner_configuration_partition),
        }
        row = await self._database.fetch_one(
            """
            SELECT state, provider_payload_json
            FROM provider_preparations
            WHERE preparation_id = :preparation_id
              AND owner_configuration_partition = :partition
              AND provider_kind = 'nzbdav'
            """,
            values,
            force_primary=True,
        )
        if row is None or row["state"] != "submitted":
            return False
        payload = self._payload(row["provider_payload_json"])
        status = _provider_status(payload.get("status"))
        if (
            not isinstance(payload.get("remote_id"), str)
            or not isinstance(payload.get("remote_hash"), str)
            or payload.get("ownership") not in _OWNERSHIP_STATES
        ):
            raise ValueError("SAB preparation is corrupt")
        missing_count = payload.get("missing_count")
        if missing_count == 0:
            if status in {"remote_missing", "remote_item_missing"}:
                raise ValueError("SAB preparation is corrupt")
            return True
        previous_status = _provider_status(payload.get("previous_status"))
        if (
            missing_count != 1
            or status != "remote_missing"
            or isinstance(payload.get("missing_observed_at"), bool)
            or not isinstance(payload.get("missing_observed_at"), (int, float))
            or not math.isfinite(payload["missing_observed_at"])
            or payload["missing_observed_at"] < 0
        ):
            raise ValueError("SAB preparation is corrupt")
        replacement = {
            key: value
            for key, value in payload.items()
            if key not in {"previous_status", "missing_observed_at"}
        }
        replacement["status"] = previous_status
        replacement["missing_count"] = 0
        updated = await self._database.fetch_one(
            """
            UPDATE provider_preparations
            SET provider_payload_json = :payload, updated_at = :now,
                last_polled_at = :now
            WHERE preparation_id = :preparation_id
              AND owner_configuration_partition = :partition
              AND provider_kind = 'nzbdav'
              AND state = 'submitted'
              AND provider_payload_json = :previous_payload
            RETURNING preparation_id
            """,
            {
                **values,
                "payload": orjson.dumps(replacement).decode(),
                "previous_payload": row["provider_payload_json"],
                "now": current_time,
            },
            force_primary=True,
        )
        if updated is not None:
            return True
        current = await self._database.fetch_one(
            """
            SELECT state, provider_payload_json
            FROM provider_preparations
            WHERE preparation_id = :preparation_id
              AND owner_configuration_partition = :partition
              AND provider_kind = 'nzbdav'
            """,
            values,
            force_primary=True,
        )
        if current is None or current["state"] != "submitted":
            return False
        current_payload = self._payload(current["provider_payload_json"])
        current_status = _provider_status(current_payload.get("status"))
        return current_payload.get("missing_count") == 0 and current_status not in {
            "remote_missing",
            "remote_item_missing",
        }

    async def record_selected_file(
        self,
        preparation_id: str,
        *,
        owner_configuration_partition: bytes,
        remote_id: str,
        remote_hash: str,
        file_index: int,
        file_size: int,
        locked_link: str | None,
        now: float | None = None,
    ) -> None:
        if (
            not all(
                isinstance(value, str) and 1 <= len(value) <= 512
                for value in (remote_id, remote_hash)
            )
            or (
                locked_link is not None
                and (
                    not isinstance(locked_link, str)
                    or not 1 <= len(locked_link) <= 4096
                )
            )
            or isinstance(file_index, bool)
            or not isinstance(file_index, int)
            or not 0 <= file_index <= MAX_SIGNED_BIGINT
            or isinstance(file_size, bool)
            or not isinstance(file_size, int)
            or not 1 <= file_size <= MAX_SIGNED_BIGINT
        ):
            raise ValueError("invalid provider file")
        current_time = time.time() if now is None else now
        values = {
            "preparation_id": str(uuid.UUID(preparation_id)),
            "partition": self._partition(owner_configuration_partition),
        }
        existing = await self._database.fetch_one(
            """
            SELECT state, provider_payload_json
            FROM provider_preparations
            WHERE preparation_id = :preparation_id
              AND owner_configuration_partition = :partition
            """,
            values,
            force_primary=True,
        )
        if existing is None:
            raise ValueError("provider file is unavailable")
        previous = self._payload(existing["provider_payload_json"])
        selected = {
            "remote_id": remote_id,
            "remote_hash": remote_hash,
            "file_index": file_index,
            "file_size": file_size,
            "status": "selected",
            "ownership": previous.get("ownership"),
            "missing_count": previous.get("missing_count"),
        }
        if locked_link is not None:
            selected["locked_link"] = locked_link
        if existing["state"] == "terminal":
            if (
                previous == selected
                and previous.get("ownership") in _OWNERSHIP_STATES
                and previous.get("missing_count") in {0, 1}
            ):
                return
            raise ValueError("provider file is unavailable")
        if (
            existing["state"] != "submitted"
            or previous.get("remote_id") != remote_id
            or previous.get("remote_hash") != remote_hash
            or previous.get("ownership") not in _OWNERSHIP_STATES
            or isinstance(previous.get("missing_count"), bool)
            or previous.get("missing_count") not in {0, 1}
        ):
            raise ValueError("provider preparation is corrupt")
        selected_payload = orjson.dumps(selected).decode()
        updated = await self._database.fetch_one(
            """
            UPDATE provider_preparations
            SET provider_payload_json = :payload, state = 'terminal',
                updated_at = :now, last_polled_at = :now,
                terminal_at = :now, gc_after_at = :gc_after_at
            WHERE preparation_id = :preparation_id AND owner_configuration_partition = :partition
              AND state = 'submitted'
              AND provider_payload_json = :previous_payload
            RETURNING preparation_id
            """,
            {
                **values,
                "payload": selected_payload,
                "previous_payload": existing["provider_payload_json"],
                "now": current_time,
                "gc_after_at": current_time + _TERMINAL_RETENTION_SECONDS,
            },
            force_primary=True,
        )
        if updated is not None:
            return
        current = await self._database.fetch_one(
            """
            SELECT state, provider_payload_json
            FROM provider_preparations
            WHERE preparation_id = :preparation_id
              AND owner_configuration_partition = :partition
            """,
            values,
            force_primary=True,
        )
        if (
            current is None
            or current["state"] != "terminal"
            or current["provider_payload_json"] != selected_payload
        ):
            raise ValueError("provider file is unavailable")

    async def record_terminal_status(
        self,
        preparation_id: str,
        *,
        owner_configuration_partition: bytes,
        status: str,
        now: float | None = None,
    ) -> None:
        status = _provider_status(status)
        current_time = time.time() if now is None else now
        values = {
            "preparation_id": str(uuid.UUID(preparation_id)),
            "partition": self._partition(owner_configuration_partition),
        }
        row = await self._database.fetch_one(
            """
            SELECT provider_payload_json
            FROM provider_preparations
            WHERE preparation_id = :preparation_id
              AND owner_configuration_partition = :partition
              AND state = 'submitted'
            """,
            values,
            force_primary=True,
        )
        if row is None:
            raise ValueError("provider terminal status is unavailable")
        previous = self._payload(row["provider_payload_json"])
        if (
            previous.get("ownership") not in _OWNERSHIP_STATES
            or not isinstance(previous.get("remote_id"), str)
            or not isinstance(previous.get("remote_hash"), str)
        ):
            raise ValueError("provider preparation is corrupt")
        updated = await self._database.fetch_one(
            """
            UPDATE provider_preparations
            SET provider_payload_json = :payload, state = 'terminal',
                updated_at = :now, last_polled_at = :now,
                terminal_at = :now, gc_after_at = :gc_after_at
            WHERE preparation_id = :preparation_id
              AND owner_configuration_partition = :partition
              AND state = 'submitted'
              AND provider_payload_json = :previous_payload
            RETURNING preparation_id
            """,
            {
                **values,
                "payload": orjson.dumps(
                    {
                        **previous,
                        "status": status,
                    }
                ).decode(),
                "previous_payload": row["provider_payload_json"],
                "now": current_time,
                "gc_after_at": current_time + _TERMINAL_RETENTION_SECONDS,
            },
            force_primary=True,
        )
        if updated is None:
            raise ValueError("provider terminal status is unavailable")

    async def record_poll(
        self,
        preparation_id: str,
        *,
        owner_configuration_partition: bytes,
        now: float | None = None,
    ) -> ProviderPreparation | None:
        """Record and return the submitted provider binding observed by a poll."""
        current_time = time.time() if now is None else now
        row = await self._database.fetch_one(
            """
            UPDATE provider_preparations
            SET last_polled_at = :now, updated_at = :now
            WHERE preparation_id = :preparation_id AND owner_configuration_partition = :partition
              AND state = 'submitted'
            RETURNING preparation_id, state, provider_payload_json, created_at
            """,
            {
                "preparation_id": str(uuid.UUID(preparation_id)),
                "partition": self._partition(owner_configuration_partition),
                "now": current_time,
            },
            force_primary=True,
        )
        return self._row(row) if row is not None else None

    @staticmethod
    def _row(row) -> ProviderPreparation:
        try:
            payload = ProviderPreparationRepository._payload(
                row["provider_payload_json"]
            )
            if row["state"] not in {
                "mutation_pending",
                "submitted",
                "terminal",
            }:
                raise ValueError
            return ProviderPreparation(
                str(uuid.UUID(row["preparation_id"])),
                row["state"],
                payload,
                float(row["created_at"]),
            )
        except (TypeError, ValueError, orjson.JSONDecodeError) as exc:
            raise ValueError("provider preparation is corrupt") from exc

    @staticmethod
    def _payload(value: object) -> dict:
        try:
            payload = orjson.loads(value)
        except (TypeError, orjson.JSONDecodeError) as exc:
            raise ValueError("provider preparation is corrupt") from exc
        if not isinstance(payload, dict):
            raise ValueError("provider preparation is corrupt")
        return payload
