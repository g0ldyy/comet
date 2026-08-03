"""Credential-free, account-scoped provider representation cache."""

import hashlib
import time
import uuid

from comet.core.capability_states import deterministic_cbor

PROVIDER_RESOLUTION_VERSION = 1
_MAX_GC_BATCH = 256
_REPRESENTATION_IDENTITY_FIELDS = (
    "selected_asset_id",
    "exact_logical_length",
    "strong_asset_revision",
)


def _milliseconds(value: float) -> int:
    return int(value * 1000)


def _scope(
    *,
    provider_kind: str,
    provider_configuration_id: str,
    account_partition: str,
    candidate_id: str,
    selection_intent_json: str,
    client: str,
) -> dict[str, str]:
    key = hashlib.sha256(
        b"comet-provider-resolution-v1\0"
        + deterministic_cbor(
            [
                provider_kind,
                provider_configuration_id,
                account_partition,
                candidate_id,
                selection_intent_json,
                client,
            ]
        )
    ).hexdigest()
    return {
        "resolution_key": key,
        "provider_kind": provider_kind,
        "provider_configuration_id": provider_configuration_id,
        "account_partition": account_partition,
        "candidate_id": candidate_id,
        "selection_intent_json": selection_intent_json,
        "client": client,
    }


def _metadata(
    *,
    target_kind: str,
    representation: dict[str, object],
) -> dict[str, object]:
    return {
        "selected_asset_id": representation["selected_asset_id"],
        "target_kind": target_kind,
        "exact_logical_length": representation["exact_logical_length"],
        "strong_asset_revision": representation["strong_asset_revision"],
        "etag_strength": representation["etag_strength"],
    }


def _has_representation_metadata(representation: dict[str, object]) -> bool:
    return any(
        representation[field] is not None for field in _REPRESENTATION_IDENTITY_FIELDS
    )


class ProviderResolutionCacheRepository:
    def __init__(self, database):
        self._database = database

    async def _scope(
        self,
        *,
        rendered_candidate_id: str,
        provider_kind: str,
        provider_configuration_id: str,
        account_partition: bytes,
        selection_intent_json: str,
        client: str,
    ) -> dict[str, str]:
        return _scope(
            provider_kind=provider_kind,
            provider_configuration_id=provider_configuration_id,
            account_partition=account_partition.hex(),
            candidate_id=await self._generic_candidate_id(rendered_candidate_id),
            selection_intent_json=selection_intent_json,
            client=client,
        )

    async def record_ready(
        self,
        *,
        rendered_candidate_id: str,
        provider_kind: str,
        provider_configuration_id: str,
        account_partition: bytes,
        selection_intent_json: str,
        client: str,
        target_kind: str,
        representation: dict[str, object],
        observed_at: float,
        expires_at: float,
    ) -> bool:
        if not _has_representation_metadata(representation):
            return False
        return await self._record(
            scope=await self._scope(
                rendered_candidate_id=rendered_candidate_id,
                provider_kind=provider_kind,
                provider_configuration_id=provider_configuration_id,
                account_partition=account_partition,
                selection_intent_json=selection_intent_json,
                client=client,
            ),
            metadata=_metadata(
                target_kind=target_kind,
                representation=representation,
            ),
            observed_at=observed_at,
            last_used_at=observed_at,
            expires_at=expires_at,
        )

    async def validate_ready(
        self,
        *,
        rendered_candidate_id: str,
        provider_kind: str,
        provider_configuration_id: str,
        account_partition: bytes,
        selection_intent_json: str,
        client: str,
        target_kind: str,
        representation: dict[str, object],
        observed_at: float,
        expires_at: float,
    ) -> bool:
        if not _has_representation_metadata(representation):
            return False
        scope = await self._scope(
            rendered_candidate_id=rendered_candidate_id,
            provider_kind=provider_kind,
            provider_configuration_id=provider_configuration_id,
            account_partition=account_partition,
            selection_intent_json=selection_intent_json,
            client=client,
        )
        current = _metadata(
            target_kind=target_kind,
            representation=representation,
        )
        cached = await self._load(scope, observed_at)
        if cached is None:
            return await self._record(
                scope=scope,
                metadata=current,
                observed_at=observed_at,
                last_used_at=observed_at,
                expires_at=expires_at,
            )
        for cached_value, current_value in (
            (cached["selected_asset_id"], current["selected_asset_id"]),
            (cached["exact_logical_length"], current["exact_logical_length"]),
            (cached["strong_asset_revision"], current["strong_asset_revision"]),
        ):
            if cached_value is not None and current_value != cached_value:
                raise ValueError("provider media representation changed")
        if cached["target_kind"] != target_kind or (
            cached["etag_strength"] == "strong" and current["etag_strength"] != "strong"
        ):
            raise ValueError("provider media representation changed")
        return True

    async def _record(
        self,
        *,
        scope: dict[str, str],
        metadata: dict[str, object],
        observed_at: float,
        last_used_at: float,
        expires_at: float,
    ) -> bool:
        observed_at_ms = _milliseconds(observed_at)
        last_used_at_ms = _milliseconds(last_used_at)
        expires_at_ms = _milliseconds(expires_at)
        if expires_at_ms <= observed_at_ms:
            return False
        row = await self._database.fetch_one(
            """
            INSERT INTO provider_resolution_cache (
                resolution_id, resolution_key, provider_kind,
                provider_configuration_id, account_partition, candidate_id,
                selection_intent_json, selected_asset_id, client,
                target_kind, resolution_version, exact_logical_length,
                strong_asset_revision, etag_strength, observed_at_ms,
                last_used_at_ms, expires_at_ms
            ) VALUES (
                :resolution_id, :resolution_key, :provider_kind,
                :provider_configuration_id, :account_partition, :candidate_id,
                :selection_intent_json, :selected_asset_id, :client,
                :target_kind, :resolution_version, :exact_logical_length,
                :strong_asset_revision, :etag_strength, :observed_at_ms,
                :last_used_at_ms, :expires_at_ms
            )
            ON CONFLICT (resolution_key) DO UPDATE SET
                selected_asset_id = CASE
                    WHEN provider_resolution_cache.expires_at_ms <=
                         excluded.observed_at_ms
                    THEN excluded.selected_asset_id
                    ELSE COALESCE(
                        provider_resolution_cache.selected_asset_id,
                        excluded.selected_asset_id
                    )
                END,
                target_kind = excluded.target_kind,
                resolution_version = excluded.resolution_version,
                exact_logical_length = CASE
                    WHEN provider_resolution_cache.expires_at_ms <=
                         excluded.observed_at_ms
                    THEN excluded.exact_logical_length
                    ELSE COALESCE(
                        provider_resolution_cache.exact_logical_length,
                        excluded.exact_logical_length
                    )
                END,
                strong_asset_revision = CASE
                    WHEN provider_resolution_cache.expires_at_ms <=
                         excluded.observed_at_ms
                    THEN excluded.strong_asset_revision
                    ELSE COALESCE(
                        provider_resolution_cache.strong_asset_revision,
                        excluded.strong_asset_revision
                    )
                END,
                etag_strength = CASE
                    WHEN provider_resolution_cache.expires_at_ms <=
                         excluded.observed_at_ms
                    THEN excluded.etag_strength
                    WHEN provider_resolution_cache.etag_strength = 'strong'
                      OR excluded.etag_strength = 'strong'
                    THEN 'strong'
                    ELSE 'weak'
                END,
                observed_at_ms = CASE
                    WHEN provider_resolution_cache.observed_at_ms >
                         excluded.observed_at_ms
                    THEN provider_resolution_cache.observed_at_ms
                    ELSE excluded.observed_at_ms
                END,
                last_used_at_ms = CASE
                    WHEN provider_resolution_cache.last_used_at_ms >
                         excluded.last_used_at_ms
                    THEN provider_resolution_cache.last_used_at_ms
                    ELSE excluded.last_used_at_ms
                END,
                expires_at_ms = CASE
                    WHEN provider_resolution_cache.expires_at_ms >
                         excluded.expires_at_ms
                      AND provider_resolution_cache.expires_at_ms >
                          excluded.observed_at_ms
                    THEN provider_resolution_cache.expires_at_ms
                    ELSE excluded.expires_at_ms
                END
            WHERE provider_resolution_cache.expires_at_ms <=
                  excluded.observed_at_ms
               OR (
                    (
                        provider_resolution_cache.selected_asset_id IS NULL
                        OR provider_resolution_cache.selected_asset_id =
                           excluded.selected_asset_id
                    )
                    AND (
                        provider_resolution_cache.exact_logical_length IS NULL
                        OR provider_resolution_cache.exact_logical_length =
                           excluded.exact_logical_length
                    )
                    AND (
                        provider_resolution_cache.strong_asset_revision IS NULL
                        OR provider_resolution_cache.strong_asset_revision =
                           excluded.strong_asset_revision
                    )
                    AND (
                        provider_resolution_cache.etag_strength = 'weak'
                        OR excluded.etag_strength = 'strong'
                    )
                    AND provider_resolution_cache.target_kind =
                        excluded.target_kind
               )
            RETURNING resolution_id
            """,
            {
                **scope,
                **metadata,
                "resolution_id": str(uuid.uuid4()),
                "resolution_version": PROVIDER_RESOLUTION_VERSION,
                "observed_at_ms": observed_at_ms,
                "last_used_at_ms": last_used_at_ms,
                "expires_at_ms": expires_at_ms,
            },
            force_primary=True,
        )
        if row is None:
            raise ValueError("provider media representation changed")
        return True

    async def _load(
        self,
        scope: dict[str, str],
        now: float,
    ):
        now_ms = _milliseconds(now)
        row = await self._database.fetch_one(
            """
            UPDATE provider_resolution_cache
            SET last_used_at_ms = CASE
                    WHEN last_used_at_ms > :now_ms
                    THEN last_used_at_ms
                    ELSE :now_ms
                END
            WHERE resolution_key = :resolution_key
              AND expires_at_ms > :now_ms
            RETURNING selected_asset_id, target_kind,
                      exact_logical_length, strong_asset_revision, etag_strength,
                      observed_at_ms, last_used_at_ms, expires_at_ms
            """,
            {"resolution_key": scope["resolution_key"], "now_ms": now_ms},
            force_primary=True,
        )
        return row

    async def _generic_candidate_id(self, rendered_candidate_id: str) -> str:
        row = await self._database.fetch_one(
            """
            SELECT rendered.external_candidate_id AS candidate_id
            FROM rendered_release_candidates AS rendered
            JOIN release_candidates AS candidate
              ON candidate.candidate_id = rendered.external_candidate_id
            WHERE rendered.candidate_id = :rendered_candidate_id
            """,
            {"rendered_candidate_id": rendered_candidate_id},
            force_primary=True,
        )
        if row is None:
            raise ValueError("provider resolution candidate is unavailable")
        return row["candidate_id"]

    async def reassign_candidate(
        self,
        loser_candidate_id: str,
        winner_candidate_id: str,
    ) -> None:
        rows = await self._database.fetch_all(
            """
            SELECT *
            FROM provider_resolution_cache
            WHERE candidate_id = :candidate_id
            ORDER BY resolution_id
            """,
            {"candidate_id": loser_candidate_id},
            force_primary=True,
        )
        for row in rows:
            scope = _scope(
                provider_kind=row["provider_kind"],
                provider_configuration_id=row["provider_configuration_id"],
                account_partition=row["account_partition"],
                candidate_id=winner_candidate_id,
                selection_intent_json=row["selection_intent_json"],
                client=row["client"],
            )
            existing = await self._database.fetch_one(
                """
                SELECT resolution_id, observed_at_ms, last_used_at_ms
                FROM provider_resolution_cache
                WHERE resolution_key = :resolution_key
                """,
                {"resolution_key": scope["resolution_key"]},
                force_primary=True,
            )
            if existing is not None and (
                existing["observed_at_ms"],
                existing["last_used_at_ms"],
                existing["resolution_id"],
            ) >= (
                row["observed_at_ms"],
                row["last_used_at_ms"],
                row["resolution_id"],
            ):
                await self._delete(row["resolution_id"])
                continue
            if existing is not None:
                await self._delete(existing["resolution_id"])
            await self._database.execute(
                """
                UPDATE provider_resolution_cache
                SET candidate_id = :candidate_id,
                    resolution_key = :resolution_key
                WHERE resolution_id = :resolution_id
                """,
                {
                    "candidate_id": winner_candidate_id,
                    "resolution_key": scope["resolution_key"],
                    "resolution_id": row["resolution_id"],
                },
                force_primary=True,
            )

    async def _delete(self, resolution_id: str) -> None:
        await self._database.execute(
            """
            DELETE FROM provider_resolution_cache
            WHERE resolution_id = :resolution_id
            """,
            {"resolution_id": resolution_id},
            force_primary=True,
        )

    async def cleanup_expired(
        self,
        *,
        now: float | None = None,
        limit: int = _MAX_GC_BATCH,
    ) -> int:
        now_ms = _milliseconds(time.time() if now is None else now)
        rows = await self._database.fetch_all(
            """
            DELETE FROM provider_resolution_cache
            WHERE resolution_id IN (
                SELECT resolution_id
                FROM provider_resolution_cache
                WHERE expires_at_ms <= :now_ms
                ORDER BY expires_at_ms, resolution_id
                LIMIT :limit
            )
              AND expires_at_ms <= :now_ms
            RETURNING resolution_id
            """,
            {"now_ms": now_ms, "limit": limit},
            force_primary=True,
        )
        return len(rows)
