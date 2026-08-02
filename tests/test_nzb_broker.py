import asyncio
import gzip
import hashlib
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

from databases import Database

from comet.core.db_router import ReplicaAwareDatabase
from comet.core.schema_migrations import (
    MigrationContext,
    _ensure_usenet_schema,
)
from comet.usenet import nzb_broker
from comet.usenet.nzb_broker import (
    NzbBroker,
    NzbBrokerError,
    normalize_nzb_document,
)


class FakeEngine:
    async def parse_nzb(self, artifact_sha256, document):
        return {
            "version": 2,
            "files": 1,
            "segments": 1,
            "nh1": "nh1:" + "a" * 40,
            "nm1": "nm1:" + "b" * 64,
            "metadata": {"password": "archive-secret"},
            "manifest": [{"subject": "release", "postings": []}],
        }

    async def catalog_nntp_artifact(
        self,
        artifact_sha256,
        _manifest_identity,
        _metadata,
        _manifest,
    ):
        path = "Movie.mkv"
        digest = hashlib.sha256()
        digest.update(b"comet-nzb-asset-v1\0")
        digest.update(bytes.fromhex(artifact_sha256))
        digest.update((0).to_bytes(4, "big"))
        encoded_path = path.encode()
        digest.update(len(encoded_path).to_bytes(4, "big"))
        digest.update(encoded_path)
        return [
            {
                "asset_id": digest.hexdigest(),
                "file_index": 0,
                "relative_path": path,
                "declared_bytes": 42,
                "kind": "video",
            }
        ]


class NzbBrokerTests(unittest.IsolatedAsyncioTestCase):
    def test_broker_normalizes_bounded_gzip_before_identity_and_parsing(self):
        document = b"<nzb>" + b"x" * 512 + b"</nzb>"

        self.assertEqual(
            normalize_nzb_document(gzip.compress(document, compresslevel=1)),
            document,
        )
        with self.assertRaisesRegex(NzbBrokerError, "nzb_gzip_invalid"):
            normalize_nzb_document(b"\x1f\x8bnot-gzip")

    def test_broker_accepts_valid_high_ratio_and_concatenated_gzip(self):
        highly_compressible = b"<nzb>" + b"x" * 1_000_000 + b"</nzb>"
        compressed = gzip.compress(highly_compressible)
        concatenated = gzip.compress(b"<nzb>") + gzip.compress(b"</nzb>")

        self.assertGreater(
            len(highly_compressible) / len(compressed),
            20,
        )
        self.assertEqual(
            normalize_nzb_document(compressed),
            highly_compressible,
        )
        self.assertEqual(normalize_nzb_document(concatenated), b"<nzb></nzb>")

    async def test_broker_publishes_an_immutable_artifact_and_owner_grant(self):
        with TemporaryDirectory() as temporary:
            database = ReplicaAwareDatabase(
                Database(f"sqlite+aiosqlite:///{temporary}/broker.db")
            )
            await database.connect()
            try:
                context = MigrationContext(database, is_sqlite=True, is_postgres=False)
                await _ensure_usenet_schema(context)
                broker = NzbBroker(f"{temporary}/artifacts", database, FakeEngine())
                partition = b"a" * 32

                first = await broker.ingest_bytes(
                    b"<nzb>one</nzb>", owner_configuration_partition=partition, now=100
                )
                second = await broker.ingest_bytes(
                    b"<nzb>one</nzb>", owner_configuration_partition=partition, now=200
                )
                other = await broker.ingest_bytes(
                    b"<nzb>two</nzb>", owner_configuration_partition=partition, now=200
                )
                self.assertEqual(first.artifact_sha256, second.artifact_sha256)
                self.assertEqual(first.grant_id, second.grant_id)
                resolved_batch = await broker.resolve_owned_artifacts(
                    [
                        first.artifact_sha256,
                        other.artifact_sha256,
                        first.artifact_sha256,
                    ],
                    owner_configuration_partition=partition,
                    now=201,
                )
                self.assertEqual(
                    set(resolved_batch),
                    {first.artifact_sha256, other.artifact_sha256},
                )
                lifecycle = await database.fetch_one(
                    """
                    SELECT publication_state, refcount, tombstoned_at
                    FROM nzb_artifacts
                    WHERE artifact_sha256 = :artifact_sha256
                    """,
                    {"artifact_sha256": first.artifact_sha256},
                )
                self.assertEqual(lifecycle["publication_state"], "published")
                self.assertEqual(lifecycle["refcount"], 1)
                self.assertIsNone(lifecycle["tombstoned_at"])
                content = await database.fetch_one(
                    """
                    SELECT manifest_identity, parser_version,
                           posting_set_identity, artifact_sha256,
                           inspection_state
                    FROM nzb_contents
                    """
                )
                self.assertEqual(content["manifest_identity"], first.nm1)
                self.assertEqual(content["parser_version"], 2)
                self.assertEqual(content["posting_set_identity"], first.nh1)
                self.assertEqual(
                    content["artifact_sha256"],
                    other.artifact_sha256,
                )
                self.assertEqual(content["inspection_state"], "parsed")
                await database.execute(
                    """
                    UPDATE nzb_artifacts
                    SET publication_state = 'tombstoned', tombstoned_at = 201
                    WHERE artifact_sha256 = :artifact_sha256
                    """,
                    {"artifact_sha256": other.artifact_sha256},
                )
                self.assertNotIn(
                    other.artifact_sha256,
                    await broker.resolve_owned_artifacts(
                        [first.artifact_sha256, other.artifact_sha256],
                        owner_configuration_partition=partition,
                        now=201,
                    ),
                )
                with self.assertRaisesRegex(
                    NzbBrokerError, "artifact_grant_unavailable"
                ):
                    await broker.acquire_owned_artifact(
                        other.artifact_sha256,
                        owner_configuration_partition=partition,
                        now=201,
                    )
                batched_identities = [
                    first.artifact_sha256,
                    *(f"{index:064x}" for index in range(256)),
                ]
                self.assertIn(
                    first.artifact_sha256,
                    await broker.resolve_owned_artifacts(
                        batched_identities,
                        owner_configuration_partition=partition,
                        now=201,
                    ),
                )
                with self.assertRaisesRegex(
                    NzbBrokerError, "invalid_artifact_identity"
                ):
                    await broker.resolve_owned_artifacts(
                        ["a" * 64] * (nzb_broker.MAX_NZB_FILES + 1),
                        owner_configuration_partition=partition,
                    )
                self.assertEqual(
                    broker._artifact_path(first.artifact_sha256).read_bytes(),
                    b"<nzb>one</nzb>",
                )
                grant = await database.fetch_one(
                    "SELECT expires_at FROM nzb_artifact_grants"
                )
                self.assertEqual(grant["expires_at"], 200 + 6 * 60 * 60)
                reader = await broker.acquire_granted_artifact(
                    first.grant_id, owner_configuration_partition=partition, now=201
                )
                self.assertEqual(reader.path.read_bytes(), b"<nzb>one</nzb>")
                self.assertEqual(reader.byte_size, len(b"<nzb>one</nzb>"))
                lease = await database.fetch_one("SELECT * FROM artifact_reader_leases")
                self.assertEqual(lease["artifact_sha256"], first.artifact_sha256)
                self.assertEqual(lease["expires_at"], 261)
                await reader.close()
                self.assertIsNone(
                    await database.fetch_one("SELECT * FROM artifact_reader_leases")
                )
                owned_reader = await broker.acquire_owned_artifact(
                    first.artifact_sha256,
                    owner_configuration_partition=partition,
                    now=201,
                )
                self.assertEqual(owned_reader.path, reader.path)
                self.assertEqual(owned_reader.byte_size, reader.byte_size)
                await owned_reader.close()
                self.assertEqual(
                    await broker.read_owned_artifact(
                        first.artifact_sha256,
                        owner_configuration_partition=partition,
                        now=201,
                    ),
                    b"<nzb>one</nzb>",
                )
                self.assertIsNone(
                    await database.fetch_one("SELECT * FROM artifact_reader_leases")
                )
                resolved = await broker.resolve_owned_artifact(
                    first.artifact_sha256,
                    owner_configuration_partition=partition,
                    now=201,
                )
                self.assertEqual(resolved.grant_id, first.grant_id)
                self.assertEqual(resolved.manifest, first.manifest)
                self.assertEqual(resolved.metadata, {"password": "archive-secret"})
                granted = await broker.resolve_granted_artifact(
                    first.grant_id,
                    owner_configuration_partition=partition,
                    now=201,
                )
                self.assertEqual(granted, resolved)
                with self.assertRaisesRegex(
                    NzbBrokerError,
                    "artifact_grant_unavailable",
                ):
                    await broker.resolve_granted_artifact(
                        first.grant_id,
                        owner_configuration_partition=b"b" * 32,
                        now=201,
                    )
                catalog = await broker.catalog_artifact(resolved)
                self.assertEqual(catalog[0].relative_path, "Movie.mkv")
                self.assertEqual(catalog[0].declared_bytes, 42)
                grant_usage = await database.fetch_one(
                    "SELECT last_used_at FROM nzb_artifact_grants"
                )
                artifact_usage = await database.fetch_one(
                    "SELECT last_used_at FROM nzb_artifacts"
                )
                content_usage = await database.fetch_one(
                    "SELECT last_used_at FROM nzb_contents"
                )
                self.assertEqual(grant_usage["last_used_at"], 201)
                self.assertEqual(artifact_usage["last_used_at"], 201)
                self.assertEqual(content_usage["last_used_at"], 201)
                with self.assertRaisesRegex(
                    NzbBrokerError, "artifact_grant_unavailable"
                ):
                    await broker.acquire_granted_artifact(
                        first.grant_id, owner_configuration_partition=b"b" * 32, now=201
                    )
            finally:
                await database.disconnect()

    async def test_reader_lease_heartbeats_until_idempotent_close(self):
        with TemporaryDirectory() as temporary:
            database = ReplicaAwareDatabase(
                Database(f"sqlite+aiosqlite:///{temporary}/broker.db")
            )
            await database.connect()
            try:
                context = MigrationContext(database, is_sqlite=True, is_postgres=False)
                await _ensure_usenet_schema(context)
                broker = NzbBroker(f"{temporary}/artifacts", database, FakeEngine())
                artifact = await broker.ingest_bytes(
                    b"<nzb>heartbeat</nzb>",
                    owner_configuration_partition=b"a" * 32,
                )

                with (
                    patch.object(nzb_broker, "_READER_HEARTBEAT_SECONDS", 0.01),
                    patch.object(nzb_broker, "_READER_LEASE_SECONDS", 0.05),
                ):
                    reader = await broker.acquire_owned_artifact(
                        artifact.artifact_sha256,
                        owner_configuration_partition=b"a" * 32,
                    )
                    initial = await database.fetch_one(
                        """
                        SELECT heartbeat_at, expires_at
                        FROM artifact_reader_leases
                        WHERE lease_id = :lease_id
                        """,
                        {"lease_id": reader.lease_id},
                    )
                    await asyncio.sleep(0.03)
                    renewed = await database.fetch_one(
                        """
                        SELECT heartbeat_at, expires_at
                        FROM artifact_reader_leases
                        WHERE lease_id = :lease_id
                        """,
                        {"lease_id": reader.lease_id},
                    )
                    self.assertGreater(renewed["heartbeat_at"], initial["heartbeat_at"])
                    self.assertGreater(renewed["expires_at"], initial["expires_at"])
                    await reader.close()
                    await reader.close()
                self.assertIsNone(
                    await database.fetch_one("SELECT * FROM artifact_reader_leases")
                )
            finally:
                await database.disconnect()

    async def test_broker_rejects_unbounded_documents_before_publication(self):
        with TemporaryDirectory() as temporary:
            broker = NzbBroker(f"{temporary}/artifacts", object(), FakeEngine())

            with patch.object(nzb_broker, "MAX_NZB_DOCUMENT_BYTES", 16):
                with self.assertRaisesRegex(NzbBrokerError, "nzb_document_too_large"):
                    await broker.ingest_bytes(
                        b"x" * 17,
                        owner_configuration_partition=b"a" * 32,
                    )
