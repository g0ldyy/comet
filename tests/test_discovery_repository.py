import hashlib
import pathlib
import re
import unittest
import uuid
from dataclasses import replace
from tempfile import TemporaryDirectory

from databases import Database

from comet.core.db_router import ReplicaAwareDatabase
from comet.core.schema_migrations import (
    MigrationContext,
    _ensure_usenet_schema,
)
from comet.core.sources import (
    MAX_SIGNED_BIGINT,
    LocatorKind,
    LocatorPolicy,
    NzbArtifactRef,
    ReleaseCandidate,
    ReleaseScope,
    TorrentLocator,
    TransportKind,
)
from comet.discovery.models import MediaQuery
from comet.discovery.repository import ReleaseDiscoveryRepository


class PortableSqlParameterTests(unittest.TestCase):
    """PostgreSQL cannot infer a bare bind parameter's type in an IS NULL test.

    asyncpg raises AmbiguousParameterError before the query ever runs, so a shape like
    ``:name IS NULL`` is a hard failure on the production database while passing on
    SQLite. Guard every SQL string in the package, not just the one that regressed.
    """

    UNTYPED_NULL_TEST = re.compile(
        r":[a-z_][a-z0-9_]*\s+IS\s+(?:NOT\s+)?NULL", re.IGNORECASE
    )

    def test_no_sql_compares_a_bare_bind_parameter_against_null(self):
        package = pathlib.Path(__file__).resolve().parent.parent / "comet"
        offenders = []
        for source in package.rglob("*.py"):
            if "__pycache__" in source.parts:
                continue
            for number, line in enumerate(source.read_text().splitlines(), 1):
                if self.UNTYPED_NULL_TEST.search(line):
                    offenders.append(
                        f"{source.relative_to(package.parent)}:{number}: {line.strip()}"
                    )
        self.assertEqual(offenders, [], "wrap the parameter in CAST(:name AS TEXT)")


class ReleaseDiscoveryRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary_directory = TemporaryDirectory()
        self.database = ReplicaAwareDatabase(
            Database(
                f"sqlite+aiosqlite:///{self.temporary_directory.name}/repository.db"
            )
        )
        await self.database.connect()
        context = MigrationContext(
            self.database,
            is_sqlite=True,
            is_postgres=False,
        )
        await _ensure_usenet_schema(context)
        self.repository = ReleaseDiscoveryRepository(self.database)
        self.configuration_id = "11111111-1111-4111-8111-111111111111"
        self.owner_partition = b"a" * 32
        self.branch_fingerprint = "b" * 64

    async def asyncTearDown(self):
        await self.database.disconnect()
        self.temporary_directory.cleanup()

    def candidate(self, media_id: str, suffix: str = "one") -> ReleaseCandidate:
        return ReleaseCandidate(
            candidate_id=f"tbx1:{suffix}",
            media_id=media_id,
            scope=ReleaseScope.MOVIE,
            transport=TransportKind.USENET,
            title=f"Example.{suffix}.2026.1080p",
            locators=(
                NzbArtifactRef(
                    locator_id=f"nzb1:locator:{suffix}",
                    kind=LocatorKind.NZB_ARTIFACT,
                    policy=LocatorPolicy(
                        frozenset({"torbox_usenet"}),
                        owner_configuration_partition=self.owner_partition,
                        exact_provider_configuration_id=self.configuration_id,
                    ),
                    artifact_sha256=hashlib.sha256(suffix.encode()).hexdigest(),
                    manifest_identity="nm1:"
                    + hashlib.sha256(f"manifest:{suffix}".encode()).hexdigest(),
                ),
            ),
            size=123,
            published_at_ms=456,
            source="Indexer",
        )

    def test_candidate_integer_domains_match_database_bigints(self):
        candidate = self.candidate("tt123")

        self.assertEqual(
            replace(candidate, size=MAX_SIGNED_BIGINT).size,
            MAX_SIGNED_BIGINT,
        )
        self.assertEqual(
            replace(candidate, published_at_ms=MAX_SIGNED_BIGINT).published_at_ms,
            MAX_SIGNED_BIGINT,
        )
        for field, values in (
            ("size", (True, 1.5, 0, MAX_SIGNED_BIGINT + 1)),
            ("published_at_ms", (True, 1.5, -1, MAX_SIGNED_BIGINT + 1)),
        ):
            for value in values:
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaises(ValueError),
                ):
                    replace(candidate, **{field: value})

    def manual_candidate(
        self,
        *,
        artifact_sha256: str,
    ) -> ReleaseCandidate:
        candidate_id = f"manual:{uuid.uuid4()}"
        return ReleaseCandidate(
            candidate_id=candidate_id,
            media_id=candidate_id,
            scope=ReleaseScope.MOVIE,
            transport=TransportKind.USENET,
            title="Imported NZB",
            locators=(
                NzbArtifactRef(
                    locator_id=f"manual-nzb:{artifact_sha256}",
                    kind=LocatorKind.NZB_ARTIFACT,
                    policy=LocatorPolicy(
                        frozenset({"stremio_nntp"}),
                        owner_configuration_partition=self.owner_partition,
                    ),
                    artifact_sha256=artifact_sha256,
                    manifest_identity="nm1:" + "d" * 64,
                ),
            ),
            source="Manual import",
        )

    async def persist(
        self,
        query: MediaQuery,
        candidates: tuple[ReleaseCandidate, ...],
        *,
        now: float,
    ):
        return await self.repository.persist_success(
            query,
            self.branch_fingerprint,
            candidates,
            discovery_configuration_id=self.configuration_id,
            owner_configuration_partition=self.owner_partition,
            next_refresh_at=now + 60,
            now=now,
        )

    async def test_upsert_keeps_uuid7_ids_and_round_trips_active_candidate(self):
        query = MediaQuery("tt1234567", "movie")
        candidate = self.candidate(query.media_id)

        first = await self.persist(query, (candidate,), now=1)
        second = await self.persist(query, (candidate,), now=2)
        loaded = await self.repository.load_active(
            query,
            self.branch_fingerprint,
            owner_configuration_partition=self.owner_partition,
            now=3,
        )

        first_ids = first[candidate.candidate_id]
        second_ids = second[candidate.candidate_id]
        self.assertEqual(first_ids, second_ids)
        self.assertEqual(uuid.UUID(first_ids.candidate_id).version, 7)
        self.assertEqual(
            uuid.UUID(first_ids.locator_ids[candidate.locators[0].locator_id]).version,
            7,
        )
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].candidate_id, first_ids.candidate_id)
        self.assertIs(loaded[0].scope, ReleaseScope.MOVIE)
        self.assertEqual(loaded[0].source, "Indexer")
        self.assertEqual(loaded[0].transport_stats, {})
        self.assertEqual(loaded[0].published_at_ms, 456)
        self.assertEqual(
            loaded[0].locators[0].artifact_sha256,
            hashlib.sha256(b"one").hexdigest(),
        )

    async def test_candidate_locators_are_unbounded_and_chunked(self):
        query = MediaQuery("tt1234569", "movie")
        info_hash = "a" * 40
        locators = tuple(
            TorrentLocator(
                locator_id=f"torrent:locator:{index}",
                kind=LocatorKind.TORRENT,
                policy=LocatorPolicy(
                    frozenset({"direct_torrent"}),
                    owner_configuration_partition=self.owner_partition,
                ),
                info_hash=info_hash,
                file_index=index,
                selection_title=f"File.{index}.mkv",
                selection_size=index + 1,
                selection_parsed_json="{}",
            )
            for index in range(333)
        )
        candidate = ReleaseCandidate(
            candidate_id=f"btih:{info_hash}",
            media_id=query.media_id,
            scope=query.scope,
            transport=TransportKind.BITTORRENT,
            title="Large.Release.2026.1080p",
            locators=locators,
            source="Legacy cache",
        )

        stored = await self.persist(query, (candidate,), now=1)
        loaded = await self.repository.load_active(
            query,
            self.branch_fingerprint,
            owner_configuration_partition=self.owner_partition,
            now=2,
        )

        self.assertEqual(len(stored[candidate.candidate_id].locator_ids), 333)
        self.assertEqual(len(loaded[0].locators), 333)

    async def test_transport_stats_round_trip_as_opaque_json(self):
        query = MediaQuery("tt1234568", "movie")
        stats = {
            "download_url": "https://cdn.example/release?token=opaque",
            "authorization_hint": "Bearer\nopaque",
            "": "empty keys are valid JSON",
        }
        candidate = replace(
            self.candidate(query.media_id),
            source="https://tracker.example/announce",
            transport_stats=stats,
        )

        await self.persist(query, (candidate,), now=1)
        loaded = await self.repository.load_active(
            query,
            self.branch_fingerprint,
            owner_configuration_partition=self.owner_partition,
            now=2,
        )

        self.assertEqual(loaded[0].transport_stats, stats)
        self.assertEqual(loaded[0].source, "https://tracker.example/announce")

    async def test_candidate_scope_must_match_its_query(self):
        query = MediaQuery("tt1234567", "movie")
        candidate = replace(
            self.candidate(query.media_id),
            scope=ReleaseScope.EPISODE,
        )

        with self.assertRaisesRegex(ValueError, "scope does not match"):
            await self.persist(query, (candidate,), now=1)

    async def test_duplicate_locator_content_collapses_to_one_coverage_row(self):
        """Two locators with identical content share one row, so coverage must dedupe."""
        query = MediaQuery("tt2222222", "movie")
        base = self.candidate(query.media_id)
        duplicate = NzbArtifactRef(
            locator_id="nzb1:locator:duplicate",
            kind=base.locators[0].kind,
            policy=base.locators[0].policy,
            artifact_sha256=base.locators[0].artifact_sha256,
            manifest_identity=base.locators[0].manifest_identity,
        )
        candidate = ReleaseCandidate(
            candidate_id=base.candidate_id,
            media_id=base.media_id,
            scope=base.scope,
            transport=base.transport,
            title=base.title,
            locators=(base.locators[0], duplicate),
            size=base.size,
            published_at_ms=base.published_at_ms,
            source=base.source,
            transport_stats=base.transport_stats,
        )

        stored = await self.persist(query, (candidate,), now=1_000.0)

        ids = stored[candidate.candidate_id]
        self.assertEqual(
            ids.locator_ids["nzb1:locator:one"],
            ids.locator_ids["nzb1:locator:duplicate"],
        )
        rows = await self.database.fetch_all(
            "SELECT locator_id FROM release_locator_coverage"
        )
        self.assertEqual(len(rows), 1)
        active = await self.repository.load_active(
            query,
            self.branch_fingerprint,
            owner_configuration_partition=self.owner_partition,
        )
        self.assertEqual(len(active), 1)
        self.assertEqual(len(active[0].locators), 1)

    async def test_exact_identity_convergence_preserves_locators_and_coverage(self):
        query = MediaQuery("tt3333333", "movie")
        first = self.candidate(query.media_id, "first")
        second = self.candidate(query.media_id, "second")
        stored = await self.persist(query, (first, second), now=1)
        first_id = stored[first.candidate_id].candidate_id
        second_id = stored[second.candidate_id].candidate_id
        identity = "nm1:" + "c" * 64

        await self.repository.attach_identity(
            first_id,
            identity,
            now=2,
        )
        canonical_id = await self.repository.attach_identity(
            second_id,
            identity,
            now=3,
        )
        active = await self.repository.load_active(
            query,
            self.branch_fingerprint,
            owner_configuration_partition=self.owner_partition,
            now=4,
        )
        redirect = await self.database.fetch_one(
            """
            SELECT redirected_candidate_id, canonical_candidate_id
            FROM candidate_redirects
            """,
            force_primary=True,
        )

        self.assertEqual(canonical_id, min(first_id, second_id))
        self.assertEqual(
            await self.repository.resolve_candidate_id(first_id),
            canonical_id,
        )
        self.assertEqual(
            await self.repository.resolve_candidate_id(second_id),
            canonical_id,
        )
        self.assertEqual(redirect["canonical_candidate_id"], canonical_id)
        self.assertNotEqual(redirect["redirected_candidate_id"], canonical_id)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].candidate_id, canonical_id)
        self.assertEqual(len(active[0].locators), 2)

    async def test_discovery_automatically_converges_exact_manifest_identity(self):
        query = MediaQuery("tt3333334", "movie")

        def artifact_candidate(suffix: str, artifact: str) -> ReleaseCandidate:
            return ReleaseCandidate(
                candidate_id=f"sa1:{suffix}",
                media_id=query.media_id,
                scope=query.scope,
                transport=TransportKind.USENET,
                title=f"Example.{suffix}.2026.1080p",
                locators=(
                    NzbArtifactRef(
                        locator_id=f"sa1:locator:{suffix}",
                        kind=LocatorKind.NZB_ARTIFACT,
                        policy=LocatorPolicy(
                            frozenset({"stremio_nntp"}),
                            owner_configuration_partition=self.owner_partition,
                        ),
                        artifact_sha256=artifact * 64,
                        manifest_identity="nm1:" + "f" * 64,
                    ),
                ),
                source="Upstream addon",
            )

        first = artifact_candidate("first", "a")
        second = artifact_candidate("second", "b")
        stored = await self.persist(query, (first, second), now=1)
        active = await self.repository.load_active(
            query,
            self.branch_fingerprint,
            owner_configuration_partition=self.owner_partition,
            now=2,
        )

        self.assertEqual(
            stored[first.candidate_id].candidate_id,
            stored[second.candidate_id].candidate_id,
        )
        self.assertEqual(len(active), 1)
        self.assertEqual(len(active[0].locators), 2)

    async def test_exact_identity_is_independent_across_candidate_families(self):
        first_query = MediaQuery("tt5555555", "movie")
        second_query = MediaQuery("tt6666666", "movie")
        first = await self.persist(
            first_query,
            (self.candidate(first_query.media_id, "first"),),
            now=1,
        )
        second = await self.persist(
            second_query,
            (self.candidate(second_query.media_id, "second"),),
            now=2,
        )
        identity = "nm1:" + "e" * 64
        await self.repository.attach_identity(
            next(iter(first.values())).candidate_id,
            identity,
            now=3,
        )

        second_id = next(iter(second.values())).candidate_id
        self.assertEqual(
            await self.repository.attach_identity(second_id, identity, now=4),
            second_id,
        )
        rows = await self.database.fetch_all(
            """
            SELECT candidate_id
            FROM candidate_identities
            WHERE identity_scheme = 'nm1' AND identity_value = :identity_value
            ORDER BY candidate_id
            """,
            {"identity_value": "e" * 64},
            force_primary=True,
        )
        self.assertEqual(
            [row["candidate_id"] for row in rows],
            sorted(
                (
                    next(iter(first.values())).candidate_id,
                    second_id,
                )
            ),
        )

    async def test_redirect_cycles_are_detected(self):
        query = MediaQuery("tt7777777", "movie")
        first = self.candidate(query.media_id, "first")
        second = self.candidate(query.media_id, "second")
        stored = await self.persist(query, (first, second), now=1)
        first_id = stored[first.candidate_id].candidate_id
        second_id = stored[second.candidate_id].candidate_id
        await self.database.execute_many(
            """
            INSERT INTO candidate_redirects (
                redirected_candidate_id, canonical_candidate_id,
                created_at_ms, updated_at_ms
            ) VALUES (
                :redirected_candidate_id, :canonical_candidate_id, 1, 1
            )
            """,
            [
                {
                    "redirected_candidate_id": first_id,
                    "canonical_candidate_id": second_id,
                },
                {
                    "redirected_candidate_id": second_id,
                    "canonical_candidate_id": first_id,
                },
            ],
            force_primary=True,
        )

        with self.assertRaisesRegex(RuntimeError, "cycle"):
            await self.repository.resolve_candidate_id(first_id)

    async def test_empty_refresh_tombstones_only_the_exact_query_membership(self):
        first_query = MediaQuery("tt1111111", "movie")
        second_query = MediaQuery("tt2222222", "movie")
        await self.persist(
            first_query,
            (self.candidate(first_query.media_id, "first"),),
            now=1,
        )
        await self.persist(
            second_query,
            (self.candidate(second_query.media_id, "second"),),
            now=2,
        )

        await self.persist(first_query, (), now=3)

        first_loaded = await self.repository.load_active(
            first_query,
            self.branch_fingerprint,
            owner_configuration_partition=self.owner_partition,
            now=4,
        )
        second_loaded = await self.repository.load_active(
            second_query,
            self.branch_fingerprint,
            owner_configuration_partition=self.owner_partition,
            now=4,
        )
        self.assertEqual(first_loaded, ())
        self.assertEqual(len(second_loaded), 1)

    async def test_public_visibility_requires_explicit_authorization(self):
        query = MediaQuery("tt1234567", "movie")
        with self.assertRaises(ValueError):
            await self.repository.persist_success(
                query,
                self.branch_fingerprint,
                (self.candidate(query.media_id),),
                discovery_configuration_id=self.configuration_id,
                owner_configuration_partition=self.owner_partition,
                visibility_partition=bytes(32),
                next_refresh_at=2,
                now=1,
            )

    async def test_public_rows_are_shared_without_retaining_request_partitions(self):
        query = MediaQuery("tt7654321", "movie")
        candidate = ReleaseCandidate(
            candidate_id="btih:" + "c" * 40,
            media_id=query.media_id,
            scope=query.scope,
            transport=TransportKind.BITTORRENT,
            title="Example.2026.1080p",
            locators=(
                TorrentLocator(
                    locator_id="torrent:public",
                    kind=LocatorKind.TORRENT,
                    policy=LocatorPolicy(frozenset({"direct_torrent"})),
                    info_hash="c" * 40,
                ),
            ),
            source="Public",
        )
        other_owner = b"z" * 32

        await self.repository.persist_success(
            query,
            self.branch_fingerprint,
            (candidate,),
            discovery_configuration_id=self.configuration_id,
            owner_configuration_partition=self.owner_partition,
            account_partition=self.owner_partition,
            public_visibility=True,
            next_refresh_at=2,
            now=1,
        )
        shared = await self.repository.load_active(
            query,
            self.branch_fingerprint,
            owner_configuration_partition=other_owner,
            account_partition=other_owner,
            public_visibility=True,
            now=1,
        )
        private = await self.repository.load_active(
            query,
            self.branch_fingerprint,
            owner_configuration_partition=other_owner,
            account_partition=other_owner,
            now=1,
        )
        row = await self.database.fetch_one(
            """
            SELECT
                candidate.visibility_partition,
                locator.owner_configuration_partition,
                locator.account_partition
            FROM release_candidates candidate
            JOIN release_locators locator
              ON locator.candidate_id = candidate.candidate_id
            WHERE candidate.media_id = :media_id
            """,
            {"media_id": query.media_id},
            force_primary=True,
        )

        self.assertEqual(len(shared), 1)
        self.assertEqual(private, ())
        self.assertEqual(row["visibility_partition"], "0" * 64)
        self.assertEqual(row["owner_configuration_partition"], "0" * 64)
        self.assertIsNone(row["account_partition"])

    async def test_public_persistence_rejects_owner_bound_locator_policy(self):
        query = MediaQuery("tt1234567", "movie")

        with self.assertRaisesRegex(
            ValueError,
            "public locator must not have an owner policy",
        ):
            await self.repository.persist_success(
                query,
                self.branch_fingerprint,
                (self.candidate(query.media_id),),
                discovery_configuration_id=self.configuration_id,
                owner_configuration_partition=self.owner_partition,
                public_visibility=True,
                next_refresh_at=2,
                now=1,
            )

    async def test_manual_origins_are_durable_and_owner_authorized(self):
        uploaded = self.manual_candidate(artifact_sha256="c" * 64)
        imported = self.manual_candidate(artifact_sha256="e" * 64)

        uploaded_ids = await self.repository.persist_manual(
            uploaded,
            origin_kind="manual_upload",
            owner_configuration_partition=self.owner_partition,
            now=1,
        )
        imported_ids = await self.repository.persist_manual(
            imported,
            origin_kind="manual_url",
            owner_configuration_partition=self.owner_partition,
            now=2,
        )
        repeated_ids = await self.repository.persist_manual(
            uploaded,
            origin_kind="manual_url",
            owner_configuration_partition=self.owner_partition,
            now=3,
        )
        rows = await self.database.fetch_all(
            """
            SELECT
                candidate.release_key, locator.origin_kind,
                locator.discovery_configuration_id
            FROM release_candidates candidate
            JOIN release_locators locator
              ON locator.candidate_id = candidate.candidate_id
            WHERE candidate.release_key IN (:uploaded, :imported)
            ORDER BY candidate.release_key
            """,
            {
                "uploaded": uploaded.candidate_id,
                "imported": imported.candidate_id,
            },
            force_primary=True,
        )

        self.assertEqual(uuid.UUID(uploaded_ids.candidate_id).version, 7)
        self.assertEqual(uuid.UUID(imported_ids.candidate_id).version, 7)
        self.assertEqual(repeated_ids, uploaded_ids)
        self.assertEqual(
            {row["release_key"]: row["origin_kind"] for row in rows},
            {
                uploaded.candidate_id: "manual_upload",
                imported.candidate_id: "manual_url",
            },
        )
        self.assertTrue(all(row["discovery_configuration_id"] is None for row in rows))
        self.assertEqual(
            await self.repository.manual_artifact_origin(
                uploaded.candidate_id,
                "c" * 64,
                owner_configuration_partition=self.owner_partition,
            ),
            "manual_upload",
        )
        with self.assertRaisesRegex(
            ValueError,
            "manual artifact candidate is unavailable",
        ):
            await self.repository.manual_artifact_origin(
                uploaded.candidate_id,
                "c" * 64,
                owner_configuration_partition=b"z" * 32,
            )
