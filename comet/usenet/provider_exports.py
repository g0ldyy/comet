"""Stable, provider-scoped NZB exports for URL-only upstreams."""

import re
import secrets
import time
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from comet.core.models import settings
from comet.usenet.identity import partition_hex

_AUDIENCE = "stremthru-newz"
_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_EXPORT_IDLE_TTL_SECONDS = 30 * 24 * 60 * 60
_MAX_GC_BATCH = 256


class NzbProviderExportError(RuntimeError):
    """A safe failure while creating or resolving a provider export."""


@dataclass(frozen=True, slots=True)
class NzbProviderExport:
    export_token: str
    owner_configuration_partition: bytes
    grant_id: str
    artifact_sha256: str
    byte_size: int


def export_base_url() -> str:
    """Return the operator-controlled URL used in immutable provider exports."""
    value = settings.USENET_EXPORT_BASE_URL or settings.PUBLIC_BASE_URL
    if not isinstance(value, str) or not value:
        raise NzbProviderExportError("nzb_export_base_url_unavailable")
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise NzbProviderExportError("nzb_export_base_url_invalid") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or any(character.isspace() or ord(character) < 33 for character in value)
    ):
        raise NzbProviderExportError("nzb_export_base_url_invalid")
    return urlunsplit(
        (parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip("/"), "", "")
    )


class NzbProviderExportRepository:
    """Persists the opaque bearer independently of a request configuration URL."""

    def __init__(self, database):
        self._database = database

    @staticmethod
    def _binding(
        partition: bytes,
        grant_id: str,
        provider_configuration_id: str,
        credential_fingerprint: str,
    ) -> dict[str, str]:
        try:
            parsed_grant = uuid.UUID(grant_id)
            parsed_provider = uuid.UUID(provider_configuration_id)
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("invalid provider export binding") from exc
        if (
            str(parsed_grant) != grant_id
            or str(parsed_provider) != provider_configuration_id
        ):
            raise ValueError("invalid provider export binding")
        if not isinstance(credential_fingerprint, str) or not _FINGERPRINT_RE.fullmatch(
            credential_fingerprint
        ):
            raise ValueError("invalid provider export binding")
        return {
            "owner_configuration_partition": partition_hex(partition),
            "grant_id": grant_id,
            "provider_configuration_id": provider_configuration_id,
            "credential_fingerprint": credential_fingerprint,
            "audience": _AUDIENCE,
        }

    async def get_or_create(
        self,
        *,
        owner_configuration_partition: bytes,
        grant_id: str,
        provider_configuration_id: str,
        credential_fingerprint: str,
        now: float | None = None,
    ) -> str:
        binding = self._binding(
            owner_configuration_partition,
            grant_id,
            provider_configuration_id,
            credential_fingerprint,
        )
        current_time = time.time() if now is None else now
        existing = await self._database.fetch_one(
            """
            UPDATE nzb_provider_exports
            SET last_used_at = CASE
                WHEN last_used_at < :last_used_at THEN :last_used_at
                ELSE last_used_at
            END
            WHERE owner_configuration_partition = :owner_configuration_partition
              AND grant_id = :grant_id AND provider_configuration_id = :provider_configuration_id
              AND credential_fingerprint = :credential_fingerprint AND audience = :audience
              AND active = TRUE
            RETURNING export_token
            """,
            {**binding, "last_used_at": current_time},
            force_primary=True,
        )
        if existing is not None:
            return f"nx1.{existing['export_token']}"
        for _ in range(3):
            token = secrets.token_hex(16)
            await self._database.execute(
                """
                INSERT INTO nzb_provider_exports (
                    export_token, owner_configuration_partition, grant_id,
                    provider_configuration_id, credential_fingerprint, audience,
                    active, created_at, last_used_at
                ) VALUES (
                    :export_token, :owner_configuration_partition, :grant_id,
                    :provider_configuration_id, :credential_fingerprint, :audience,
                    TRUE, :created_at, :last_used_at
                ) ON CONFLICT DO NOTHING
                """,
                {
                    **binding,
                    "export_token": token,
                    "created_at": current_time,
                    "last_used_at": current_time,
                },
                force_primary=True,
            )
            existing = await self._database.fetch_one(
                """
                UPDATE nzb_provider_exports
                SET last_used_at = CASE
                    WHEN last_used_at < :last_used_at THEN :last_used_at
                    ELSE last_used_at
                END
                WHERE owner_configuration_partition = :owner_configuration_partition
                  AND grant_id = :grant_id AND provider_configuration_id = :provider_configuration_id
                  AND credential_fingerprint = :credential_fingerprint AND audience = :audience
                  AND active = TRUE
                RETURNING export_token
                """,
                {**binding, "last_used_at": current_time},
                force_primary=True,
            )
            if existing is not None:
                return f"nx1.{existing['export_token']}"
        raise NzbProviderExportError("nzb_provider_export_persistence_failed")

    async def resolve(
        self, token: str, *, now: float | None = None
    ) -> NzbProviderExport:
        if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
            raise NzbProviderExportError("nzb_provider_export_unavailable")
        current_time = time.time() if now is None else now
        claimed = await self._database.fetch_one(
            """
            UPDATE nzb_provider_exports
            SET last_used_at = CASE
                    WHEN last_used_at < :now THEN :now
                    ELSE last_used_at
                END,
                request_count = request_count + 1
            WHERE export_token = :export_token
              AND audience = :audience
              AND active = TRUE
              AND EXISTS (
                  SELECT 1
                  FROM nzb_artifact_grants AS grants
                  JOIN nzb_artifacts AS artifacts
                    ON artifacts.artifact_sha256 = grants.artifact_sha256
                  WHERE grants.grant_id = nzb_provider_exports.grant_id
                    AND grants.owner_configuration_partition =
                        nzb_provider_exports.owner_configuration_partition
                    AND grants.expires_at >= :now
                    AND artifacts.storage_kind = 'nzb'
                    AND artifacts.publication_state = 'published'
              )
            RETURNING owner_configuration_partition, grant_id
            """,
            {"export_token": token, "audience": _AUDIENCE, "now": current_time},
            force_primary=True,
        )
        if claimed is None:
            raise NzbProviderExportError("nzb_provider_export_unavailable")
        row = await self._database.fetch_one(
            """
            SELECT artifacts.artifact_sha256, artifacts.byte_size
            FROM nzb_artifact_grants AS grants
            JOIN nzb_artifacts AS artifacts
              ON artifacts.artifact_sha256 = grants.artifact_sha256
            WHERE grants.grant_id = :grant_id
              AND grants.owner_configuration_partition =
                  :owner_configuration_partition
              AND grants.expires_at >= :now
              AND artifacts.storage_kind = 'nzb'
              AND artifacts.publication_state = 'published'
            """,
            {
                "grant_id": claimed["grant_id"],
                "owner_configuration_partition": claimed[
                    "owner_configuration_partition"
                ],
                "now": current_time,
            },
            force_primary=True,
        )
        if row is None:
            raise NzbProviderExportError("nzb_provider_export_unavailable")
        return NzbProviderExport(
            token,
            bytes.fromhex(claimed["owner_configuration_partition"]),
            claimed["grant_id"],
            row["artifact_sha256"],
            row["byte_size"],
        )

    async def garbage_collect(
        self,
        *,
        now: float | None = None,
        limit: int = _MAX_GC_BATCH,
    ) -> tuple[int, int]:
        """Revoke idle exports and delete only those with no live preparation."""
        current_time = time.time() if now is None else now
        cutoff = current_time - _EXPORT_IDLE_TTL_SECONDS
        stale = await self._database.fetch_all(
            """
            SELECT exports.export_token
            FROM nzb_provider_exports AS exports
            WHERE exports.active = TRUE
              AND exports.last_used_at < :cutoff
              AND NOT EXISTS (
                  SELECT 1
                  FROM provider_preparations AS preparations
                  WHERE preparations.artifact_grant_id = exports.grant_id
                    AND (
                        preparations.state != 'terminal'
                        OR preparations.gc_after_at IS NULL
                        OR preparations.gc_after_at > :now
                    )
              )
            ORDER BY exports.last_used_at, exports.export_token
            LIMIT :limit
            """,
            {"cutoff": cutoff, "now": current_time, "limit": limit},
            force_primary=True,
        )
        revoked = 0
        for row in stale:
            updated = await self._database.fetch_one(
                """
                UPDATE nzb_provider_exports
                SET active = FALSE,
                    revoked_at = :now,
                    revocation_reason = 'idle'
                WHERE export_token = :export_token
                  AND active = TRUE
                  AND last_used_at < :cutoff
                  AND NOT EXISTS (
                      SELECT 1
                      FROM provider_preparations AS preparations
                      WHERE preparations.artifact_grant_id =
                            nzb_provider_exports.grant_id
                        AND (
                            preparations.state != 'terminal'
                            OR preparations.gc_after_at IS NULL
                            OR preparations.gc_after_at > :now
                        )
                  )
                RETURNING export_token
                """,
                {
                    "now": current_time,
                    "cutoff": cutoff,
                    "export_token": row["export_token"],
                },
                force_primary=True,
            )
            revoked += updated is not None

        removable = await self._database.fetch_all(
            """
            SELECT exports.export_token
            FROM nzb_provider_exports AS exports
            WHERE exports.active = FALSE
              AND NOT EXISTS (
                  SELECT 1
                  FROM provider_preparations AS preparations
                  WHERE preparations.artifact_grant_id = exports.grant_id
                    AND (
                        preparations.state != 'terminal'
                        OR preparations.gc_after_at IS NULL
                        OR preparations.gc_after_at > :now
                    )
              )
            ORDER BY exports.revoked_at, exports.export_token
            LIMIT :limit
            """,
            {"now": current_time, "limit": limit},
            force_primary=True,
        )
        deleted = 0
        for row in removable:
            removed = await self._database.fetch_one(
                """
                DELETE FROM nzb_provider_exports
                WHERE export_token = :export_token
                  AND active = FALSE
                  AND NOT EXISTS (
                      SELECT 1
                      FROM provider_preparations AS preparations
                      WHERE preparations.artifact_grant_id =
                            nzb_provider_exports.grant_id
                        AND (
                            preparations.state != 'terminal'
                            OR preparations.gc_after_at IS NULL
                            OR preparations.gc_after_at > :now
                        )
                  )
                RETURNING export_token
                """,
                {
                    "export_token": row["export_token"],
                    "now": current_time,
                },
                force_primary=True,
            )
            deleted += removed is not None
        return revoked, deleted
