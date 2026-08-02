"""Immutable real-NZB brokerage shared by every compatible playback provider."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import os
import stat
import time
import uuid
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import orjson

from comet.usenet.artifact_leases import RUNTIME_OWNER, ArtifactReaderLease
from comet.usenet.engine_client import (
    EngineClient,
    EngineNntpError,
    EngineParseError,
)
from comet.usenet.engine_transport import EngineUnavailable
from comet.usenet.file_selection import (
    FileSelectionError,
    UsenetAsset,
    catalog_engine_source_assets,
)
from comet.usenet.identity import partition_hex
from comet.usenet.limits import (
    MAX_NZB_DOCUMENT_BYTES,
    MAX_NZB_FILES,
    MAX_NZB_METADATA_BYTES,
)

_GRANT_TTL_SECONDS = 6 * 60 * 60
_READER_LEASE_SECONDS = 60
_READER_HEARTBEAT_SECONDS = 20
_NZB_PARSER_VERSION = 2
_DATABASE_BATCH_SIZE = 256


class NzbBrokerError(RuntimeError):
    """A bounded ingestion failure safe to expose as a local error category."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class NzbArtifact:
    artifact_sha256: str
    grant_id: str
    nh1: str
    nm1: str
    manifest: list
    byte_size: int
    expires_at: float
    metadata: dict[str, str] = field(default_factory=dict)


class NzbArtifactReader(ArtifactReaderLease):
    """One renewable database-visible lease over an immutable artifact."""

    def __init__(
        self,
        database,
        *,
        lease_id: str,
        artifact_sha256: str,
        path: Path,
        byte_size: int,
    ):
        super().__init__(
            database,
            lease_id=lease_id,
            artifact_sha256=artifact_sha256,
            lease_seconds=_READER_LEASE_SECONDS,
            heartbeat_seconds=_READER_HEARTBEAT_SECONDS,
        )
        self.path = path
        self.byte_size = byte_size


def normalize_nzb_document(
    document: bytes,
    *,
    maximum_bytes: int | None = None,
) -> bytes:
    """Apply the broker's independent gzip and decompressed byte bounds."""
    limit = MAX_NZB_DOCUMENT_BYTES if maximum_bytes is None else maximum_bytes
    if not isinstance(document, bytes) or not document:
        raise NzbBrokerError("nzb_document_too_large")
    if len(document) > limit:
        raise NzbBrokerError("nzb_document_too_large")
    if not document.startswith(b"\x1f\x8b"):
        return document
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(document)) as compressed:
            decoded = compressed.read(limit + 1)
    except (OSError, EOFError, zlib.error) as exc:
        raise NzbBrokerError("nzb_gzip_invalid") from exc
    if not decoded or len(decoded) > limit:
        raise NzbBrokerError("nzb_document_too_large")
    return decoded


def _valid_artifact_identity(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _artifact_from_row(row) -> NzbArtifact:
    try:
        document = orjson.loads(row["manifest_json"])
    except orjson.JSONDecodeError as exc:
        raise NzbBrokerError("artifact_metadata_corrupt") from exc
    if (
        not isinstance(document, dict)
        or not isinstance(document.get("metadata"), dict)
        or not isinstance(document.get("files"), list)
    ):
        raise NzbBrokerError("artifact_metadata_corrupt")
    return NzbArtifact(
        row["artifact_sha256"],
        row["grant_id"],
        row["nh1"],
        row["nm1"],
        document["files"],
        row["byte_size"],
        row["expires_at"],
        document["metadata"],
    )


class NzbBroker:
    """Publishes immutable NZB bytes and grants them to one configuration partition."""

    def __init__(self, artifact_dir: str | Path, database, engine: EngineClient):
        self._artifact_dir = Path(artifact_dir)
        self._database = database
        self._engine = engine

    def _artifact_path(self, artifact_sha256: str) -> Path:
        return self._artifact_dir / "nzb" / f"{artifact_sha256}.nzb"

    @staticmethod
    def _publish(path: Path, document: bytes) -> bool:
        """Atomically publish once without replacing another immutable artifact."""
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        created = False
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as output:
                output.write(document)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary, path)
                created = True
            except FileExistsError:
                if path.stat().st_size != len(document):
                    raise NzbBrokerError(
                        "artifact identity conflicts with stored bytes"
                    )
                digest = hashlib.sha256()
                with path.open("rb") as existing:
                    for chunk in iter(lambda: existing.read(1024 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest() != path.stem.removesuffix(".nzb"):
                    raise NzbBrokerError(
                        "artifact identity conflicts with stored bytes"
                    )
            finally:
                temporary.unlink(missing_ok=True)
            directory = os.open(
                path.parent,
                os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY,
            )
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return created
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    async def ingest_bytes(
        self,
        document: bytes,
        *,
        owner_configuration_partition: bytes,
        now: float | None = None,
    ) -> NzbArtifact:
        document = normalize_nzb_document(document)
        partition = partition_hex(owner_configuration_partition)
        artifact_sha256 = hashlib.sha256(document).hexdigest()
        path = self._artifact_path(artifact_sha256)
        try:
            parsed = await self._engine.parse_nzb(artifact_sha256, document)
        except EngineParseError as exc:
            raise NzbBrokerError("nzb_parse_failed") from exc

        current_time = time.time() if now is None else now
        expires_at = current_time + _GRANT_TTL_SECONDS
        if parsed.get("version") != _NZB_PARSER_VERSION:
            raise NzbBrokerError("nzb_parser_version_unsupported")
        manifest_document = orjson.dumps(
            {
                "metadata": parsed["metadata"],
                "files": parsed["manifest"],
            }
        )
        if len(manifest_document) > MAX_NZB_METADATA_BYTES:
            raise NzbBrokerError("nzb_metadata_too_large")
        manifest_json = manifest_document.decode("utf-8")
        grant_id = str(uuid.uuid4())
        # Publishing under the artifact-row transaction serializes resurrection
        # against GC before either side mutates the shared POSIX object.
        async with self._database.transaction():
            await self._database.execute(
                """
                INSERT INTO nzb_artifacts (
                    artifact_sha256, byte_size, storage_kind, relative_path,
                    publication_state, refcount, source_manifest_identity,
                    created_at, last_used_at, tombstoned_at
                ) VALUES (
                    :artifact_sha256, :byte_size, 'nzb', :relative_path,
                    'publishing', 0, :nm1,
                    :created_at, :last_used_at, NULL
                )
                ON CONFLICT (artifact_sha256) DO UPDATE SET
                    byte_size = EXCLUDED.byte_size,
                    storage_kind = 'nzb',
                    relative_path = EXCLUDED.relative_path,
                    publication_state = 'publishing',
                    source_manifest_identity =
                        EXCLUDED.source_manifest_identity,
                    last_used_at = EXCLUDED.last_used_at,
                    tombstoned_at = NULL
                """,
                {
                    "artifact_sha256": artifact_sha256,
                    "byte_size": len(document),
                    "relative_path": f"nzb/{artifact_sha256}.nzb",
                    "nm1": parsed["nm1"],
                    "created_at": current_time,
                    "last_used_at": current_time,
                },
                force_primary=True,
            )
            await self._database.execute(
                """
                INSERT INTO nzb_contents (
                    manifest_identity, parser_version,
                    posting_set_identity, artifact_sha256,
                    manifest_json, inspection_state,
                    created_at, last_used_at
                ) VALUES (
                    :nm1, :parser_version, :nh1, :artifact_sha256,
                    :manifest_json, 'parsed', :created_at, :last_used_at
                )
                ON CONFLICT (manifest_identity, parser_version) DO UPDATE SET
                    posting_set_identity = EXCLUDED.posting_set_identity,
                    artifact_sha256 = EXCLUDED.artifact_sha256,
                    manifest_json = EXCLUDED.manifest_json,
                    last_used_at = EXCLUDED.last_used_at
                """,
                {
                    "nm1": parsed["nm1"],
                    "parser_version": _NZB_PARSER_VERSION,
                    "nh1": parsed["nh1"],
                    "artifact_sha256": artifact_sha256,
                    "manifest_json": manifest_json,
                    "created_at": current_time,
                    "last_used_at": current_time,
                },
                force_primary=True,
            )
            try:
                await asyncio.to_thread(self._publish, path, document)
            except OSError as exc:
                raise NzbBrokerError("artifact_publication_failed") from exc
            await self._database.execute(
                """
                UPDATE nzb_artifacts
                SET publication_state = 'published'
                WHERE artifact_sha256 = :artifact_sha256
                  AND publication_state = 'publishing'
                """,
                {"artifact_sha256": artifact_sha256},
                force_primary=True,
            )
            grant = await self._database.fetch_one(
                """
                INSERT INTO nzb_artifact_grants (
                    grant_id, artifact_sha256, owner_configuration_partition,
                    created_at, last_used_at, expires_at
                ) VALUES (
                    :grant_id, :artifact_sha256, :owner_configuration_partition,
                    :created_at, :last_used_at, :expires_at
                )
                ON CONFLICT (artifact_sha256, owner_configuration_partition) DO UPDATE SET
                    last_used_at = EXCLUDED.last_used_at,
                    expires_at = EXCLUDED.expires_at
                RETURNING grant_id
                """,
                {
                    "grant_id": grant_id,
                    "artifact_sha256": artifact_sha256,
                    "owner_configuration_partition": partition,
                    "created_at": current_time,
                    "last_used_at": current_time,
                    "expires_at": expires_at,
                },
                force_primary=True,
            )
            if grant is None:
                raise NzbBrokerError("artifact_grant_persistence_failed")
            await self._database.execute(
                """
                UPDATE nzb_artifacts
                SET refcount = (
                    SELECT COUNT(*)
                    FROM nzb_artifact_grants AS grants
                    WHERE grants.artifact_sha256 = :artifact_sha256
                )
                WHERE artifact_sha256 = :artifact_sha256
                """,
                {"artifact_sha256": artifact_sha256},
                force_primary=True,
            )
        return NzbArtifact(
            artifact_sha256=artifact_sha256,
            grant_id=grant["grant_id"],
            nh1=parsed["nh1"],
            nm1=parsed["nm1"],
            manifest=parsed["manifest"],
            byte_size=len(document),
            expires_at=expires_at,
            metadata=parsed["metadata"],
        )

    async def catalog_artifact(
        self,
        artifact: NzbArtifact,
    ) -> tuple[UsenetAsset, ...]:
        """Return Rust-derived, digest-bound source assets without NNTP work."""
        if not isinstance(artifact, NzbArtifact):
            raise NzbBrokerError("asset_catalog_invalid")
        try:
            engine_assets = await self._engine.catalog_nntp_artifact(
                artifact.artifact_sha256,
                artifact.nm1,
                artifact.metadata,
                artifact.manifest,
            )
            return catalog_engine_source_assets(
                artifact.artifact_sha256,
                engine_assets,
            )
        except (
            EngineNntpError,
            EngineUnavailable,
            FileSelectionError,
            ValueError,
        ) as exc:
            raise NzbBrokerError("asset_catalog_failed") from exc

    async def acquire_granted_artifact(
        self,
        grant_id: str,
        *,
        owner_configuration_partition: bytes,
        now: float | None = None,
    ) -> NzbArtifactReader:
        """Lease an active owner-scoped artifact without accepting an HTTP path."""
        try:
            parsed_grant = uuid.UUID(grant_id)
        except (TypeError, ValueError, AttributeError) as exc:
            raise NzbBrokerError("invalid_artifact_grant") from exc
        if str(parsed_grant) != grant_id:
            raise NzbBrokerError("invalid_artifact_grant")
        partition = partition_hex(owner_configuration_partition)
        current_time = time.time() if now is None else now
        lease_id = str(uuid.uuid4())
        async with self._database.transaction():
            # The conditional UPDATE locks the artifact before lease insertion.
            # GC either observes this lease or wins first and makes this fail.
            row = await self._database.fetch_one(
                """
                UPDATE nzb_artifacts
                SET last_used_at = :now
                WHERE storage_kind = 'nzb'
                  AND publication_state = 'published'
                  AND EXISTS (
                      SELECT 1
                      FROM nzb_artifact_grants AS grants
                      WHERE grants.artifact_sha256 =
                            nzb_artifacts.artifact_sha256
                        AND grants.grant_id = :grant_id
                        AND grants.owner_configuration_partition =
                            :owner_configuration_partition
                        AND grants.expires_at >= :now
                  )
                RETURNING artifact_sha256, byte_size, relative_path
                """,
                {
                    "grant_id": grant_id,
                    "owner_configuration_partition": partition,
                    "now": current_time,
                },
                force_primary=True,
            )
            if row is None:
                raise NzbBrokerError("artifact_grant_unavailable")
            artifact_sha256 = row["artifact_sha256"]
            relative_path = row["relative_path"]
            if relative_path != f"nzb/{artifact_sha256}.nzb":
                raise NzbBrokerError("artifact_storage_corrupt")
            await self._database.execute(
                """
                INSERT INTO artifact_reader_leases (
                    lease_id, artifact_sha256, runtime_owner,
                    acquired_at, heartbeat_at, expires_at
                ) VALUES (
                    :lease_id, :artifact_sha256, :runtime_owner,
                    :now, :now, :expires_at
                )
                """,
                {
                    "lease_id": lease_id,
                    "artifact_sha256": artifact_sha256,
                    "runtime_owner": RUNTIME_OWNER,
                    "now": current_time,
                    "expires_at": current_time + _READER_LEASE_SECONDS,
                },
                force_primary=True,
            )
            await self._database.execute(
                """
                UPDATE nzb_artifact_grants
                SET last_used_at = :now
                WHERE grant_id = :grant_id
                """,
                {"now": current_time, "grant_id": grant_id},
                force_primary=True,
            )
        path = self._artifact_path(artifact_sha256)
        try:
            artifact_stat = await asyncio.to_thread(path.lstat)
            if (
                not stat.S_ISREG(artifact_stat.st_mode)
                or artifact_stat.st_size != row["byte_size"]
            ):
                raise NzbBrokerError("artifact_storage_corrupt")
        except OSError as exc:
            await self._database.execute(
                "DELETE FROM artifact_reader_leases WHERE lease_id = :lease_id",
                {"lease_id": lease_id},
                force_primary=True,
            )
            raise NzbBrokerError("artifact_storage_unavailable") from exc
        except NzbBrokerError:
            await self._database.execute(
                "DELETE FROM artifact_reader_leases WHERE lease_id = :lease_id",
                {"lease_id": lease_id},
                force_primary=True,
            )
            raise
        return NzbArtifactReader(
            self._database,
            lease_id=lease_id,
            artifact_sha256=artifact_sha256,
            path=path,
            byte_size=row["byte_size"],
        )

    async def acquire_owned_artifact(
        self,
        artifact_sha256: str,
        *,
        owner_configuration_partition: bytes,
        now: float | None = None,
    ) -> NzbArtifactReader:
        """Lease an active owner grant selected by immutable artifact identity."""
        if not _valid_artifact_identity(artifact_sha256):
            raise NzbBrokerError("invalid_artifact_identity")
        partition = partition_hex(owner_configuration_partition)
        current_time = time.time() if now is None else now
        grant = await self._database.fetch_one(
            """
            SELECT grant_id
            FROM nzb_artifact_grants
            WHERE artifact_sha256 = :artifact_sha256
              AND owner_configuration_partition = :owner_configuration_partition
              AND expires_at >= :now
            """,
            {
                "artifact_sha256": artifact_sha256,
                "owner_configuration_partition": partition,
                "now": current_time,
            },
            force_primary=True,
        )
        if grant is None:
            raise NzbBrokerError("artifact_grant_unavailable")
        return await self.acquire_granted_artifact(
            grant["grant_id"],
            owner_configuration_partition=owner_configuration_partition,
            now=current_time,
        )

    async def read_owned_artifact(
        self,
        artifact_sha256: str,
        *,
        owner_configuration_partition: bytes,
        now: float | None = None,
    ) -> bytes:
        """Read immutable bytes while holding and finally releasing a lease."""
        reader = await self.acquire_owned_artifact(
            artifact_sha256,
            owner_configuration_partition=owner_configuration_partition,
            now=now,
        )
        async with reader:
            try:
                return await asyncio.to_thread(reader.path.read_bytes)
            except OSError as exc:
                raise NzbBrokerError("artifact_storage_unavailable") from exc

    async def resolve_owned_artifact(
        self,
        artifact_sha256: str,
        *,
        owner_configuration_partition: bytes,
        now: float | None = None,
    ) -> NzbArtifact:
        """Load the active owner grant and safe manifest for one immutable artifact."""
        artifacts = await self.resolve_owned_artifacts(
            (artifact_sha256,),
            owner_configuration_partition=owner_configuration_partition,
            now=now,
        )
        artifact = artifacts.get(artifact_sha256)
        if artifact is None:
            raise NzbBrokerError("artifact_grant_unavailable")
        return artifact

    async def resolve_owned_artifacts(
        self,
        artifact_sha256s,
        *,
        owner_configuration_partition: bytes,
        now: float | None = None,
    ) -> dict[str, NzbArtifact]:
        """Resolve a bounded owner-scoped artifact set in database-safe batches."""
        if not isinstance(artifact_sha256s, (list, tuple)):
            raise NzbBrokerError("invalid_artifact_identity")
        if len(artifact_sha256s) > MAX_NZB_FILES or any(
            not _valid_artifact_identity(identity) for identity in artifact_sha256s
        ):
            raise NzbBrokerError("invalid_artifact_identity")
        identities = tuple(dict.fromkeys(artifact_sha256s))
        if not identities:
            return {}
        current_time = time.time() if now is None else now
        rows = []
        for offset in range(0, len(identities), _DATABASE_BATCH_SIZE):
            batch = identities[offset : offset + _DATABASE_BATCH_SIZE]
            identity_params = {
                f"artifact_sha256_{index}": identity
                for index, identity in enumerate(batch)
            }
            identity_placeholders = ", ".join(
                f":artifact_sha256_{index}" for index in range(len(batch))
            )
            rows.extend(
                await self._database.fetch_all(
                    f"""
            SELECT grants.grant_id, grants.expires_at,
                   artifacts.artifact_sha256,
                   contents.posting_set_identity AS nh1,
                   contents.manifest_identity AS nm1,
                   contents.manifest_json, artifacts.byte_size
            FROM nzb_artifact_grants AS grants
            JOIN nzb_artifacts AS artifacts
              ON artifacts.artifact_sha256 = grants.artifact_sha256
            JOIN nzb_contents AS contents
              ON contents.manifest_identity =
                 artifacts.source_manifest_identity
             AND contents.parser_version = :parser_version
            WHERE grants.artifact_sha256 IN ({identity_placeholders})
              AND grants.owner_configuration_partition =
                  :owner_configuration_partition
              AND grants.expires_at >= :now
              AND artifacts.storage_kind = 'nzb'
              AND artifacts.publication_state = 'published'
            """,
                    {
                        **identity_params,
                        "parser_version": _NZB_PARSER_VERSION,
                        "owner_configuration_partition": partition_hex(
                            owner_configuration_partition
                        ),
                        "now": current_time,
                    },
                    force_primary=True,
                )
            )
        if not rows:
            return {}
        for offset in range(0, len(rows), _DATABASE_BATCH_SIZE):
            batch = rows[offset : offset + _DATABASE_BATCH_SIZE]
            grant_params = {
                f"grant_id_{index}": row["grant_id"] for index, row in enumerate(batch)
            }
            grant_placeholders = ", ".join(
                f":grant_id_{index}" for index in range(len(batch))
            )
            await self._database.execute(
                f"""
            UPDATE nzb_artifact_grants
            SET last_used_at = :now
            WHERE grant_id IN ({grant_placeholders})
            """,
                {"now": current_time, **grant_params},
                force_primary=True,
            )
            returned_identity_params = {
                f"returned_sha256_{index}": row["artifact_sha256"]
                for index, row in enumerate(batch)
            }
            returned_identity_placeholders = ", ".join(
                f":returned_sha256_{index}" for index in range(len(batch))
            )
            returned_manifest_params = {
                f"returned_nm1_{index}": row["nm1"] for index, row in enumerate(batch)
            }
            returned_manifest_placeholders = ", ".join(
                f":returned_nm1_{index}" for index in range(len(batch))
            )
            await self._database.execute(
                f"""
            UPDATE nzb_artifacts
            SET last_used_at = :now
            WHERE artifact_sha256 IN ({returned_identity_placeholders})
            """,
                {"now": current_time, **returned_identity_params},
                force_primary=True,
            )
            await self._database.execute(
                f"""
            UPDATE nzb_contents
            SET last_used_at = :now
            WHERE parser_version = :parser_version
              AND manifest_identity IN ({returned_manifest_placeholders})
            """,
                {
                    "now": current_time,
                    "parser_version": _NZB_PARSER_VERSION,
                    **returned_manifest_params,
                },
                force_primary=True,
            )
        resolved = {}
        for row in rows:
            resolved[row["artifact_sha256"]] = _artifact_from_row(row)
        return resolved

    async def resolve_granted_artifact(
        self,
        grant_id: str,
        *,
        owner_configuration_partition: bytes,
        now: float | None = None,
    ) -> NzbArtifact:
        """Load parsed metadata through an exact owner-bound grant."""
        try:
            parsed_grant = uuid.UUID(grant_id)
        except (TypeError, ValueError, AttributeError) as exc:
            raise NzbBrokerError("invalid_artifact_grant") from exc
        if str(parsed_grant) != grant_id:
            raise NzbBrokerError("invalid_artifact_grant")
        current_time = time.time() if now is None else now
        row = await self._database.fetch_one(
            """
            SELECT grants.grant_id, grants.expires_at,
                   artifacts.artifact_sha256,
                   contents.posting_set_identity AS nh1,
                   contents.manifest_identity AS nm1,
                   contents.manifest_json, artifacts.byte_size
            FROM nzb_artifact_grants AS grants
            JOIN nzb_artifacts AS artifacts
              ON artifacts.artifact_sha256 = grants.artifact_sha256
            JOIN nzb_contents AS contents
              ON contents.manifest_identity =
                 artifacts.source_manifest_identity
             AND contents.parser_version = :parser_version
            WHERE grants.grant_id = :grant_id
              AND grants.owner_configuration_partition =
                  :owner_configuration_partition
              AND grants.expires_at >= :now
              AND artifacts.storage_kind = 'nzb'
              AND artifacts.publication_state = 'published'
            """,
            {
                "grant_id": grant_id,
                "parser_version": _NZB_PARSER_VERSION,
                "owner_configuration_partition": partition_hex(
                    owner_configuration_partition
                ),
                "now": current_time,
            },
            force_primary=True,
        )
        if row is None:
            raise NzbBrokerError("artifact_grant_unavailable")
        return await self._resolved_artifact(row, current_time)

    async def _resolved_artifact(self, row, current_time: float) -> NzbArtifact:
        artifact = _artifact_from_row(row)
        await self._database.execute(
            """
            UPDATE nzb_artifact_grants
            SET last_used_at = :now
            WHERE grant_id = :grant_id
            """,
            {"now": current_time, "grant_id": row["grant_id"]},
            force_primary=True,
        )
        await self._database.execute(
            """
            UPDATE nzb_contents
            SET last_used_at = :now
            WHERE manifest_identity = :manifest_identity
              AND parser_version = :parser_version
            """,
            {
                "now": current_time,
                "manifest_identity": row["nm1"],
                "parser_version": _NZB_PARSER_VERSION,
            },
            force_primary=True,
        )
        await self._database.execute(
            """
            UPDATE nzb_artifacts
            SET last_used_at = :now
            WHERE artifact_sha256 = :artifact_sha256
            """,
            {
                "now": current_time,
                "artifact_sha256": row["artifact_sha256"],
            },
            force_primary=True,
        )
        return artifact
