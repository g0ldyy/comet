import asyncio
import hashlib
import sqlite3
import unittest
import uuid
from dataclasses import replace
from tempfile import TemporaryDirectory

from databases import Database

from comet.core.db_router import ReplicaAwareDatabase
from comet.core.locator_codec import locator_from_json, locator_json, policy_json
from comet.core.schema_migrations import (
    MigrationContext,
    _ensure_usenet_schema,
)
from comet.core.sources import (
    MAX_SIGNED_BIGINT,
    EasynewsHttpRef,
    LocatorKind,
    LocatorPolicy,
    NzbArtifactRef,
    RealNzbRef,
    ReleaseCandidate,
    ReleaseScope,
    TorrentLocator,
    TransportKind,
)
from comet.discovery.models import MediaQuery
from comet.discovery.repository import ReleaseDiscoveryRepository
from comet.playback.preparations import (
    PlaybackIntent,
    PlaybackPreparationRepository,
)
from comet.playback.provider_preparations import (
    ProviderPreparationRepository,
    provider_selection_json,
)
from comet.playback.repository import RenderedReleaseRepository


class RenderedReleaseRepositoryTests(unittest.TestCase):
    def test_torrent_file_selection_metadata_round_trips(self):
        locator = TorrentLocator(
            locator_id="torrent-file",
            kind=LocatorKind.TORRENT,
            policy=LocatorPolicy(frozenset({"direct_torrent"})),
            info_hash="a" * 40,
            file_index=7,
            season_norm=1,
            episode_norm=2,
            selection_title="Show.S01E02.mkv",
            selection_size=123,
            selection_parsed_json='{"episodes":[2],"seasons":[1]}',
        )
        encoded = locator_json(locator)

        self.assertEqual(
            locator_from_json(
                locator.locator_id,
                locator.kind.value,
                encoded,
                policy_json(locator),
            ),
            locator,
        )

    def test_locator_serialization_preserves_url_shaped_remote_guid(self):
        locator = RealNzbRef(
            locator_id="source-locator",
            kind=LocatorKind.REAL_NZB,
            policy=LocatorPolicy(frozenset({"stremio_nntp"})),
            adapter_configuration_id="11111111-1111-4111-8111-111111111111",
            remote_guid="HTTPS://indexer.example/nzb/secret",
        )

        restored = locator_from_json(
            locator.locator_id,
            locator.kind.value,
            locator_json(locator),
            policy_json(locator),
        )

        self.assertEqual(restored, locator)

    def test_locator_codec_round_trips_policy_and_rejects_extra_fields(self):
        locator = RealNzbRef(
            locator_id="source-locator",
            kind=LocatorKind.REAL_NZB,
            policy=LocatorPolicy(
                frozenset({"stremio_nntp"}),
                owner_configuration_partition=b"a" * 32,
                exact_provider_configuration_id=(
                    "11111111-1111-4111-8111-111111111111"
                ),
                expires_at=123,
            ),
            adapter_configuration_id="22222222-2222-4222-8222-222222222222",
            remote_guid="opaque-guid",
        )

        restored = locator_from_json(
            locator.locator_id,
            locator.kind.value,
            locator_json(locator),
            policy_json(locator),
        )

        self.assertEqual(restored, locator)
        with self.assertRaises(ValueError):
            locator_from_json(
                locator.locator_id,
                locator.kind.value,
                (
                    '{"adapter_configuration_id":'
                    '"22222222-2222-4222-8222-222222222222",'
                    '"remote_guid":"guid","url":"x"}'
                ),
                policy_json(locator),
            )

    def test_easynews_locator_round_trips_an_absent_item_suffix(self):
        locator = EasynewsHttpRef(
            locator_id="easynews-locator",
            kind=LocatorKind.EASYNEWS_HTTP,
            policy=LocatorPolicy(
                frozenset({"easynews"}),
                owner_configuration_partition=b"a" * 32,
                exact_provider_configuration_id=(
                    "11111111-1111-4111-8111-111111111111"
                ),
            ),
            account_configuration_id=("11111111-1111-4111-8111-111111111111"),
            file_identifier="post-hash",
            download_farm="farm",
            download_port="443",
            content_hash="post-hash",
            item_identifier="",
            filename="Example.Movie.2026",
            extension="mkv",
            byte_size=42,
        )

        self.assertEqual(
            locator_from_json(
                locator.locator_id,
                locator.kind.value,
                locator_json(locator),
                policy_json(locator),
            ),
            locator,
        )

    def test_torrent_locator_values_round_trip_opaquely(self):
        torrent = TorrentLocator(
            locator_id="torrent",
            kind=LocatorKind.TORRENT,
            policy=LocatorPolicy(frozenset({"direct_torrent"})),
            info_hash="a" * 40,
        )
        for locator in (
            replace(torrent, info_hash="A" * 40),
            replace(
                torrent,
                file_index=-42,
                season_norm=-99,
                episode_norm="future",
                selection_title="",
                selection_size=-1,
                selection_parsed_json="opaque, not JSON\n" + "x" * 70_000,
            ),
        ):
            with self.subTest(locator=locator):
                self.assertEqual(
                    locator_from_json(
                        locator.locator_id,
                        locator.kind.value,
                        locator_json(locator),
                        policy_json(locator),
                    ),
                    locator,
                )

    def test_usenet_locator_policy_rejects_noncanonical_values(self):
        real_nzb = RealNzbRef(
            locator_id="real-nzb",
            kind=LocatorKind.REAL_NZB,
            policy=LocatorPolicy(frozenset({"torbox_usenet"})),
            adapter_configuration_id="11111111-1111-4111-8111-111111111111",
            remote_guid="release",
        )
        with self.subTest("provider kind incompatible with locator"):
            with self.assertRaises(ValueError):
                policy_json(
                    replace(
                        real_nzb,
                        policy=LocatorPolicy(frozenset({"direct_torrent"})),
                    )
                )
        with self.subTest("noncanonical provider set"):
            policy = policy_json(real_nzb)
            with self.assertRaises(ValueError):
                locator_from_json(
                    real_nzb.locator_id,
                    real_nzb.kind.value,
                    locator_json(real_nzb),
                    policy.replace(
                        '["torbox_usenet"]',
                        '["torbox_usenet","torbox_usenet"]',
                    ),
                )

    def test_artifact_selection_hint_round_trips_as_an_atomic_pair(self):
        locator = NzbArtifactRef(
            locator_id="artifact",
            kind=LocatorKind.NZB_ARTIFACT,
            policy=LocatorPolicy(frozenset({"comet_native_usenet"})),
            artifact_sha256="a" * 64,
            manifest_identity="nm1:" + "b" * 64,
            selection_hint_name="Movie.2026.mkv",
            selection_hint_size=42,
        )
        policy = policy_json(locator)
        encoded = locator_json(locator)

        self.assertEqual(
            locator_from_json(
                locator.locator_id,
                locator.kind.value,
                encoded,
                policy,
            ),
            locator,
        )
        with self.assertRaises(ValueError):
            locator_from_json(
                locator.locator_id,
                locator.kind.value,
                (
                    '{"artifact_sha256":"'
                    + "a" * 64
                    + '","manifest_identity":"nm1:'
                    + "b" * 64
                    + '","selection_hint_name":null,'
                    '"selection_hint_size":null}'
                ),
                policy,
            )
        self.assertIn(
            str(MAX_SIGNED_BIGINT),
            locator_json(replace(locator, selection_hint_size=MAX_SIGNED_BIGINT)),
        )
        with self.assertRaises(ValueError):
            locator_json(replace(locator, selection_hint_size=MAX_SIGNED_BIGINT + 1))


class RenderedReleaseRepositoryAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def _render_canonical(
        self,
        database,
        candidate: ReleaseCandidate,
        *,
        owner: bytes,
        configuration_id: str,
    ):
        query = MediaQuery(candidate.media_id, "movie")
        discovery = ReleaseDiscoveryRepository(database)
        await discovery.persist_success(
            query,
            "b" * 64,
            (candidate,),
            discovery_configuration_id=configuration_id,
            owner_configuration_partition=owner,
            account_partition=owner,
            next_refresh_at=60,
            now=1,
        )
        canonical = (
            await discovery.load_active(
                query,
                "b" * 64,
                owner_configuration_partition=owner,
                account_partition=owner,
                now=2,
            )
        )[0]
        repository = RenderedReleaseRepository(database)
        persisted = (
            await repository.persist(
                (canonical,),
                owner_configuration_partition=owner,
                now=2,
            )
        )[canonical.candidate_id]
        return repository, canonical, persisted

    async def test_gc_preserves_live_capabilities_and_mutation_authority(self):
        with TemporaryDirectory() as temporary:
            database = ReplicaAwareDatabase(
                Database(f"sqlite+aiosqlite:///{temporary}/rendered-gc.db")
            )
            await database.connect()
            try:
                await _ensure_usenet_schema(
                    MigrationContext(
                        database,
                        is_sqlite=True,
                        is_postgres=False,
                    )
                )
                repository = RenderedReleaseRepository(database)

                def candidate(name):
                    return ReleaseCandidate(
                        candidate_id=name,
                        media_id="tt123",
                        scope=ReleaseScope.MOVIE,
                        transport=TransportKind.USENET,
                        title=name,
                        locators=(
                            NzbArtifactRef(
                                locator_id=f"{name}-locator",
                                kind=LocatorKind.NZB_ARTIFACT,
                                policy=LocatorPolicy(frozenset({"torbox_usenet"})),
                                artifact_sha256=hashlib.sha256(
                                    name.encode()
                                ).hexdigest(),
                                manifest_identity="nm1:"
                                + hashlib.sha256(
                                    f"manifest:{name}".encode()
                                ).hexdigest(),
                            ),
                        ),
                    )

                stale, live, mutated, fresh = (
                    candidate(name) for name in ("stale", "live", "mutated", "fresh")
                )
                persisted = await repository.persist(
                    (stale, live, mutated),
                    owner_configuration_partition=b"a" * 32,
                    now=1,
                )
                await repository.persist(
                    (fresh,),
                    owner_configuration_partition=b"a" * 32,
                    now=86_402,
                )
                live_ids = persisted[live.candidate_id]
                await PlaybackPreparationRepository(database).get_or_create(
                    PlaybackIntent(
                        live_ids.candidate_id,
                        str(uuid.uuid4()),
                        tuple(live_ids.locator_ids.values()),
                        (0,),
                        "stremio",
                    ),
                    provider_kind="torbox_usenet",
                    owner_configuration_partition=b"a" * 32,
                    preparation_intent_key="a" * 64,
                    now=80_000,
                )
                mutated_ids = persisted[mutated.candidate_id]
                await ProviderPreparationRepository(database).get_or_create(
                    owner_configuration_partition=b"a" * 32,
                    provider_configuration_id=str(uuid.uuid4()),
                    credential_fingerprint="b" * 64,
                    candidate_id=mutated_ids.candidate_id,
                    locator_id=next(iter(mutated_ids.locator_ids.values())),
                    artifact_grant_id=None,
                    selection_json=provider_selection_json((0,)),
                    mutation_idempotency_key="c" * 64,
                    provider_kind="torbox_usenet",
                    now=2,
                )

                removed = await repository.garbage_collect(now=86_402)
                remaining_candidates = await database.fetch_all(
                    """
                    SELECT external_candidate_id
                    FROM rendered_release_candidates
                    ORDER BY external_candidate_id
                    """
                )
                remaining_locators = await database.fetch_all(
                    """
                    SELECT external_locator_id
                    FROM rendered_release_locators
                    ORDER BY external_locator_id
                    """
                )

                self.assertEqual(removed, (1, 1))
                self.assertEqual(
                    [row["external_candidate_id"] for row in remaining_candidates],
                    ["fresh", "live", "mutated"],
                )
                self.assertEqual(
                    [row["external_locator_id"] for row in remaining_locators],
                    [
                        "fresh-locator",
                        "live-locator",
                        "mutated-locator",
                    ],
                )
            finally:
                await database.disconnect()

    async def test_gc_prunes_an_old_unbound_locator_from_an_active_candidate(
        self,
    ):
        with TemporaryDirectory() as temporary:
            database = ReplicaAwareDatabase(
                Database(f"sqlite+aiosqlite:///{temporary}/locator-gc.db")
            )
            await database.connect()
            try:
                await _ensure_usenet_schema(
                    MigrationContext(
                        database,
                        is_sqlite=True,
                        is_postgres=False,
                    )
                )
                repository = RenderedReleaseRepository(database)

                def candidate(locator_id):
                    return ReleaseCandidate(
                        candidate_id="same-candidate",
                        media_id="tt123",
                        scope=ReleaseScope.MOVIE,
                        transport=TransportKind.USENET,
                        title="Example",
                        locators=(
                            NzbArtifactRef(
                                locator_id=locator_id,
                                kind=LocatorKind.NZB_ARTIFACT,
                                policy=LocatorPolicy(frozenset({"torbox_usenet"})),
                                artifact_sha256=hashlib.sha256(
                                    locator_id.encode()
                                ).hexdigest(),
                                manifest_identity="nm1:"
                                + hashlib.sha256(
                                    f"manifest:{locator_id}".encode()
                                ).hexdigest(),
                            ),
                        ),
                    )

                await repository.persist(
                    (candidate("old"),),
                    owner_configuration_partition=b"a" * 32,
                    now=1,
                )
                await repository.persist(
                    (candidate("current"),),
                    owner_configuration_partition=b"a" * 32,
                    now=86_402,
                )

                removed = await repository.garbage_collect(now=86_403)
                locators = await database.fetch_all(
                    """
                    SELECT external_locator_id
                    FROM rendered_release_locators
                    ORDER BY external_locator_id
                    """
                )

                self.assertEqual(removed, (1, 0))
                self.assertEqual(
                    [row["external_locator_id"] for row in locators],
                    ["current"],
                )
            finally:
                await database.disconnect()

    async def test_brokered_grab_attaches_exact_manifest_identity(self):
        with TemporaryDirectory() as temporary:
            database = ReplicaAwareDatabase(
                Database(f"sqlite+aiosqlite:///{temporary}/lineage.db")
            )
            await database.connect()
            try:
                context = MigrationContext(
                    database,
                    is_sqlite=True,
                    is_postgres=False,
                )
                await _ensure_usenet_schema(context)
                owner = b"a" * 32
                configuration_id = "11111111-1111-4111-8111-111111111111"
                query = MediaQuery("tt1234567", "movie")
                candidate = ReleaseCandidate(
                    candidate_id="nzb1:search-result",
                    media_id=query.media_id,
                    scope=query.scope,
                    transport=TransportKind.USENET,
                    title="Example.2026.1080p",
                    locators=(
                        RealNzbRef(
                            locator_id="nzb1:source",
                            kind=LocatorKind.REAL_NZB,
                            policy=LocatorPolicy(
                                frozenset({"nzbdav"}),
                                owner_configuration_partition=owner,
                            ),
                            adapter_configuration_id=configuration_id,
                            remote_guid="opaque-guid",
                        ),
                    ),
                )
                discovery = ReleaseDiscoveryRepository(database)
                await discovery.persist_success(
                    query,
                    "b" * 64,
                    (candidate,),
                    discovery_configuration_id=configuration_id,
                    owner_configuration_partition=owner,
                    account_partition=owner,
                    next_refresh_at=60,
                    now=1,
                )
                canonical = (
                    await discovery.load_active(
                        query,
                        "b" * 64,
                        owner_configuration_partition=owner,
                        account_partition=owner,
                        now=2,
                    )
                )[0]
                rendered = RenderedReleaseRepository(database)
                rendered_ids = (
                    await rendered.persist(
                        (canonical,),
                        owner_configuration_partition=owner,
                        now=2,
                    )
                )[canonical.candidate_id]

                blocker = sqlite3.connect(f"{temporary}/lineage.db")
                blocker.execute("BEGIN IMMEDIATE")
                attachment = asyncio.create_task(
                    rendered.attach_brokered_artifact(
                        rendered_ids.candidate_id,
                        rendered_ids.locator_ids[canonical.locators[0].locator_id],
                        "a" * 64,
                        "nm1:" + "b" * 64,
                        owner_configuration_partition=owner,
                        now=3,
                    )
                )
                await asyncio.sleep(0.1)
                self.assertFalse(attachment.done())
                blocker.commit()
                blocker.close()
                await asyncio.wait_for(attachment, timeout=1)
                identity = await database.fetch_one(
                    """
                    SELECT candidate_id, identity_scheme, identity_value
                    FROM candidate_identities
                    WHERE identity_scheme = 'nm1'
                    """,
                    force_primary=True,
                )

                self.assertEqual(identity["candidate_id"], canonical.candidate_id)
                self.assertEqual(identity["identity_value"], "b" * 64)
            finally:
                await database.disconnect()

    async def test_easynews_artifact_attachment_keeps_target_and_owner_policy(self):
        with TemporaryDirectory() as temporary:
            database = ReplicaAwareDatabase(
                Database(f"sqlite+aiosqlite:///{temporary}/easynews.db")
            )
            await database.connect()
            try:
                await _ensure_usenet_schema(
                    MigrationContext(
                        database,
                        is_sqlite=True,
                        is_postgres=False,
                    )
                )
                target_provider_id = "11111111-1111-4111-8111-111111111111"
                owner = b"a" * 32
                candidate = ReleaseCandidate(
                    candidate_id="easynews-candidate",
                    media_id="tt123",
                    scope=ReleaseScope.MOVIE,
                    transport=TransportKind.USENET,
                    title="Example.2024.1080p",
                    locators=(
                        EasynewsHttpRef(
                            locator_id="easynews-source",
                            kind=LocatorKind.EASYNEWS_HTTP,
                            policy=LocatorPolicy(
                                frozenset({"stremio_nntp"}),
                                owner_configuration_partition=owner,
                                exact_provider_configuration_id=target_provider_id,
                            ),
                            account_configuration_id=target_provider_id,
                            file_identifier="file",
                            download_farm="farm",
                            download_port="443",
                            content_hash="hash",
                            item_identifier="item",
                            filename="Movie",
                            extension="future-video",
                            signature="signature",
                            byte_size=42,
                        ),
                    ),
                )
                repository, canonical, ids = await self._render_canonical(
                    database,
                    candidate,
                    owner=owner,
                    configuration_id=target_provider_id,
                )

                attached = await repository.attach_brokered_artifact(
                    ids.candidate_id,
                    ids.locator_ids[canonical.locators[0].locator_id],
                    "a" * 64,
                    "nm1:" + "b" * 64,
                    owner_configuration_partition=owner,
                    now=3,
                )

                self.assertEqual(
                    attached["policy"]["exact_provider_configuration_id"],
                    target_provider_id,
                )
                self.assertEqual(
                    attached["policy"]["owner_configuration_partition"],
                    owner.hex(),
                )
                self.assertEqual(
                    attached["policy"]["allowed_provider_kinds"],
                    ["stremio_nntp"],
                )
                self.assertEqual(
                    attached["payload"]["selection_hint_name"],
                    "Movie.future-video",
                )
                self.assertEqual(
                    attached["payload"]["selection_hint_size"],
                    42,
                )
                self.assertEqual(
                    (
                        await repository.brokered_artifacts(
                            ids.candidate_id,
                            ids.locator_ids[canonical.locators[0].locator_id],
                            owner_configuration_partition=owner,
                        )
                    )[0]["locator_id"],
                    attached["locator_id"],
                )
            finally:
                await database.disconnect()

    async def test_brokered_artifacts_retain_exact_source_lineage_and_owner(self):
        with TemporaryDirectory() as temporary:
            database = ReplicaAwareDatabase(
                Database(f"sqlite+aiosqlite:///{temporary}/rendered.db")
            )
            await database.connect()
            try:
                await _ensure_usenet_schema(
                    MigrationContext(
                        database,
                        is_sqlite=True,
                        is_postgres=False,
                    )
                )
                owner = b"a" * 32
                configuration_id = "11111111-1111-4111-8111-111111111111"
                candidate = ReleaseCandidate(
                    candidate_id="external-candidate",
                    media_id="tt123",
                    scope=ReleaseScope.MOVIE,
                    transport=TransportKind.USENET,
                    title="Example.2024.1080p",
                    locators=(
                        RealNzbRef(
                            locator_id="external-source",
                            kind=LocatorKind.REAL_NZB,
                            policy=LocatorPolicy(
                                frozenset({"nzbdav", "torbox_usenet"}),
                                owner_configuration_partition=owner,
                            ),
                            adapter_configuration_id=configuration_id,
                            remote_guid="opaque-guid",
                        ),
                    ),
                )
                repository, canonical, ids = await self._render_canonical(
                    database,
                    candidate,
                    owner=owner,
                    configuration_id=configuration_id,
                )
                source_locator_id = ids.locator_ids[canonical.locators[0].locator_id]
                first = await repository.attach_brokered_artifact(
                    ids.candidate_id,
                    source_locator_id,
                    "a" * 64,
                    "nm1:" + "b" * 64,
                    owner_configuration_partition=owner,
                    now=3,
                )
                second = await repository.attach_brokered_artifact(
                    ids.candidate_id,
                    source_locator_id,
                    "c" * 64,
                    "nm1:" + "d" * 64,
                    owner_configuration_partition=owner,
                    now=4,
                )

                attached = await repository.brokered_artifacts(
                    ids.candidate_id,
                    source_locator_id,
                    owner_configuration_partition=owner,
                )

                self.assertNotEqual(first["locator_id"], second["locator_id"])
                self.assertEqual(
                    [locator["payload"]["artifact_sha256"] for locator in attached],
                    ["c" * 64, "a" * 64],
                )
                self.assertEqual(
                    attached[0]["policy"]["owner_configuration_partition"],
                    owner.hex(),
                )
                self.assertEqual(
                    attached[0]["policy"]["allowed_provider_kinds"],
                    ["nzbdav", "torbox_usenet"],
                )
                self.assertEqual(
                    await repository.brokered_artifacts(
                        ids.candidate_id,
                        source_locator_id,
                        owner_configuration_partition=b"b" * 32,
                    ),
                    (),
                )
            finally:
                await database.disconnect()

    async def test_persistence_returns_internal_candidate_and_locator_ids(self):
        """Drive the real SQL: a mocked execute cannot observe upsert RETURNING."""
        candidate = ReleaseCandidate(
            candidate_id="external-candidate",
            media_id="tt123",
            scope=ReleaseScope.MOVIE,
            transport=TransportKind.USENET,
            title="Example.2024.1080p",
            locators=(
                NzbArtifactRef(
                    locator_id="external-locator",
                    kind=LocatorKind.NZB_ARTIFACT,
                    policy=LocatorPolicy(frozenset({"torbox_usenet"})),
                    artifact_sha256="a" * 64,
                    manifest_identity="nm1:" + "b" * 64,
                ),
            ),
        )
        with TemporaryDirectory() as temporary:
            database = ReplicaAwareDatabase(
                Database(f"sqlite+aiosqlite:///{temporary}/persist.db")
            )
            await database.connect()
            try:
                await _ensure_usenet_schema(
                    MigrationContext(database, is_sqlite=True, is_postgres=False)
                )
                repository = RenderedReleaseRepository(database)

                persisted = await repository.persist(
                    (candidate,), owner_configuration_partition=b"a" * 32, now=1
                )
                ids = persisted["external-candidate"]
                self.assertRegex(ids.candidate_id, r"^[0-9a-f-]{36}$")
                self.assertRegex(
                    ids.locator_ids["external-locator"], r"^[0-9a-f-]{36}$"
                )

                repeated = await repository.persist(
                    (candidate,), owner_configuration_partition=b"a" * 32, now=2
                )
                self.assertEqual(repeated["external-candidate"], ids)

                other_owner = await repository.persist(
                    (candidate,), owner_configuration_partition=b"b" * 32, now=3
                )
                self.assertNotEqual(
                    other_owner["external-candidate"].candidate_id, ids.candidate_id
                )

                rows = await database.fetch_all(
                    "SELECT COUNT(*) AS total FROM rendered_release_locators"
                )
                self.assertEqual(rows[0]["total"], 2)
            finally:
                await database.disconnect()

    async def test_resolution_requires_every_locator_to_belong_to_the_candidate(self):
        """Real SQL: a mocked fetch cannot observe the batched lookup or its ordering."""
        locators = tuple(
            NzbArtifactRef(
                locator_id=f"external-{index}",
                kind=LocatorKind.NZB_ARTIFACT,
                policy=LocatorPolicy(frozenset({"torbox_usenet"})),
                artifact_sha256=f"{index:064x}",
                manifest_identity="nm1:" + f"{index + 10:064x}",
            )
            for index in range(3)
        )
        candidate = ReleaseCandidate(
            candidate_id="external-candidate",
            media_id="tt123",
            scope=ReleaseScope.MOVIE,
            transport=TransportKind.USENET,
            title="Example.2024.1080p",
            locators=locators,
        )
        other = ReleaseCandidate(
            candidate_id="other-candidate",
            media_id="tt123",
            scope=ReleaseScope.MOVIE,
            transport=TransportKind.USENET,
            title="Other.2024.1080p",
            locators=(
                NzbArtifactRef(
                    locator_id="external-other",
                    kind=LocatorKind.NZB_ARTIFACT,
                    policy=LocatorPolicy(frozenset({"torbox_usenet"})),
                    artifact_sha256="f" * 64,
                    manifest_identity="nm1:" + "e" * 64,
                ),
            ),
        )
        with TemporaryDirectory() as temporary:
            database = ReplicaAwareDatabase(
                Database(f"sqlite+aiosqlite:///{temporary}/resolve.db")
            )
            await database.connect()
            try:
                await _ensure_usenet_schema(
                    MigrationContext(database, is_sqlite=True, is_postgres=False)
                )
                repository = RenderedReleaseRepository(database)
                persisted = await repository.persist(
                    (candidate, other), owner_configuration_partition=b"a" * 32, now=1
                )
                ids = persisted["external-candidate"].locator_ids
                candidate_id = persisted["external-candidate"].candidate_id
                signed = [ids[f"external-{index}"] for index in (2, 0, 1)]

                resolved = await repository.resolve_intent(
                    candidate_id, signed, owner_configuration_partition=b"a" * 32
                )

                self.assertEqual(resolved.candidate_id, candidate_id)
                self.assertEqual(
                    [
                        locator["payload"]["artifact_sha256"]
                        for locator in resolved.locators
                    ],
                    [f"{index:064x}" for index in (2, 0, 1)],
                    "locators must come back in the signed order, not row order",
                )

                foreign = persisted["other-candidate"].locator_ids["external-other"]
                with self.assertRaisesRegex(ValueError, "locator is unavailable"):
                    await repository.resolve_intent(
                        candidate_id,
                        [ids["external-0"], foreign],
                        owner_configuration_partition=b"a" * 32,
                    )

                with self.assertRaisesRegex(ValueError, "locator is unavailable"):
                    await repository.resolve_intent(
                        candidate_id,
                        [str(uuid.uuid4())],
                        owner_configuration_partition=b"a" * 32,
                    )
            finally:
                await database.disconnect()

    def test_authorization_requires_matching_provider_owner_and_expiry(self):
        intent = type(
            "Intent",
            (),
            {
                "locators": (
                    {
                        "policy": {
                            "allowed_provider_kinds": ["torbox_usenet"],
                            "exact_provider_configuration_id": "11111111-1111-4111-8111-111111111111",
                            "owner_configuration_partition": (b"a" * 32).hex(),
                            "expires_at": 101,
                        }
                    },
                ),
            },
        )()

        RenderedReleaseRepository.authorize_intent(
            intent,
            provider_configuration_id="11111111-1111-4111-8111-111111111111",
            provider_kind="torbox_usenet",
            owner_configuration_partition=b"a" * 32,
            now=100,
        )
        with self.assertRaises(ValueError):
            RenderedReleaseRepository.authorize_intent(
                intent,
                provider_configuration_id="22222222-2222-4222-8222-222222222222",
                provider_kind="torbox_usenet",
                owner_configuration_partition=b"a" * 32,
                now=100,
            )
        with self.assertRaises(ValueError):
            RenderedReleaseRepository.authorize_intent(
                intent,
                provider_configuration_id="11111111-1111-4111-8111-111111111111",
                provider_kind="torbox_usenet",
                owner_configuration_partition=b"a" * 32,
                now=101,
            )
