import asyncio
import unittest
import uuid
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from databases import Database

from comet.core.db_router import ReplicaAwareDatabase
from comet.core.schema_migrations import (
    MigrationContext,
    _ensure_managed_table,
    _ensure_usenet_schema,
)
from comet.core.schema_specs import SCRAPE_LOCKS_TABLE_SPEC
from comet.core.sources import (
    LocatorKind,
    LocatorPolicy,
    RealNzbRef,
    ReleaseCandidate,
    ReleaseScope,
    TransportKind,
)
from comet.discovery.models import MediaQuery
from comet.discovery.repository import ReleaseDiscoveryRepository
from comet.playback.manager import (
    PlaybackIntentResolution,
    PreparedPlaybackIntent,
    broker_nzb_sources,
)
from comet.playback.preparations import PlaybackPreparation
from comet.playback.repository import RenderedReleaseRepository
from comet.playback.tokens import PlaybackIntent
from comet.services.lock import DistributedLock


class _Adapter:
    def __init__(self):
        self.grabs = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def grab(self, remote_guid: str) -> bytes:
        assert remote_guid == "opaque-guid"
        self.grabs += 1
        self.started.set()
        await self.release.wait()
        return b"<nzb/>"


class _Broker:
    def __init__(self):
        self.ingestions = 0
        self.artifact = type(
            "Artifact",
            (),
            {
                "artifact_sha256": "a" * 64,
                "grant_id": str(uuid.uuid4()),
                "nh1": "nh1:" + "c" * 40,
                "nm1": "nm1:" + "b" * 64,
            },
        )()

    async def ingest_bytes(self, document: bytes, **_kwargs):
        assert document == b"<nzb/>"
        self.ingestions += 1
        return self.artifact

    async def resolve_owned_artifact(self, artifact_sha256: str, **_kwargs):
        assert artifact_sha256 == self.artifact.artifact_sha256
        return self.artifact


class RealNzbBrokerageTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_preparations_singleflight_one_exact_origin_grab(self):
        with TemporaryDirectory() as temporary:
            database = ReplicaAwareDatabase(
                Database(f"sqlite+aiosqlite:///{temporary}/brokerage.db")
            )
            await database.connect()
            try:
                context = MigrationContext(
                    database,
                    is_sqlite=True,
                    is_postgres=False,
                )
                await _ensure_usenet_schema(context)
                await _ensure_managed_table(context, SCRAPE_LOCKS_TABLE_SPEC)
                owner = b"a" * 32
                provider_id = str(uuid.uuid4())
                source_configuration_id = str(uuid.uuid4())
                query = MediaQuery("tt123", "movie")
                candidate = ReleaseCandidate(
                    candidate_id="source-candidate",
                    media_id=query.media_id,
                    scope=ReleaseScope.MOVIE,
                    transport=TransportKind.USENET,
                    title="Example.2024.1080p",
                    locators=(
                        RealNzbRef(
                            locator_id="source-locator",
                            kind=LocatorKind.REAL_NZB,
                            policy=LocatorPolicy(
                                frozenset({"nzbdav"}),
                                owner_configuration_partition=owner,
                            ),
                            adapter_configuration_id=source_configuration_id,
                            remote_guid="opaque-guid",
                        ),
                    ),
                )
                discovery = ReleaseDiscoveryRepository(database)
                await discovery.persist_success(
                    query,
                    "b" * 64,
                    (candidate,),
                    discovery_configuration_id=source_configuration_id,
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
                persisted = await repository.persist(
                    (canonical,),
                    owner_configuration_partition=owner,
                )
                ids = persisted[canonical.candidate_id]
                source_locator_id = canonical.locators[0].locator_id
                release = await repository.resolve_intent(
                    ids.candidate_id,
                    [ids.locator_ids[source_locator_id]],
                    owner_configuration_partition=owner,
                )
                provider = type(
                    "Provider",
                    (),
                    {
                        "descriptor": type(
                            "Descriptor",
                            (),
                            {"kind": "nzbdav"},
                        )()
                    },
                )()
                intent = PlaybackIntent(
                    ids.candidate_id,
                    provider_id,
                    (ids.locator_ids[source_locator_id],),
                    (0,),
                    "stremio",
                )
                resolution = PlaybackIntentResolution(
                    intent,
                    provider,
                    {},
                    release,
                    b"c" * 32,
                    "d" * 64,
                )

                def prepared() -> PreparedPlaybackIntent:
                    preparation = PlaybackPreparation(
                        str(uuid.uuid4()),
                        ids.candidate_id,
                        provider_id,
                        "nzbdav",
                        intent.locator_ids,
                        intent.selection_intent,
                        "stremio",
                        "pending",
                        None,
                        None,
                        1,
                    )
                    return PreparedPlaybackIntent(
                        resolution,
                        preparation,
                        "pa2.test",
                    )

                adapter = _Adapter()
                broker = _Broker()
                acquire = DistributedLock.acquire
                lock_attempts = 0
                second_lock_attempt = asyncio.Event()

                async def observed_acquire(lock, *args, **kwargs):
                    nonlocal lock_attempts
                    lock_attempts += 1
                    if lock_attempts == 2:
                        second_lock_attempt.set()
                    return await acquire(lock, *args, **kwargs)

                with (
                    patch.object(
                        DistributedLock,
                        "acquire",
                        new=observed_acquire,
                    ),
                    patch(
                        "comet.playback.manager.PlaybackPreparationRepository."
                        "bind_artifact",
                        AsyncMock(),
                    ) as bound,
                ):
                    first = asyncio.create_task(
                        broker_nzb_sources(
                            prepared(),
                            broker,
                            database,
                            {source_configuration_id: adapter},
                            owner_configuration_partition=owner,
                        )
                    )
                    await adapter.started.wait()
                    second = asyncio.create_task(
                        broker_nzb_sources(
                            prepared(),
                            broker,
                            database,
                            {source_configuration_id: adapter},
                            owner_configuration_partition=owner,
                        )
                    )
                    await second_lock_attempt.wait()
                    adapter.release.set()
                    transformed = await asyncio.gather(first, second)

                self.assertEqual(adapter.grabs, 1)
                self.assertEqual(broker.ingestions, 1)
                self.assertEqual(bound.await_count, 2)
                self.assertEqual(
                    {
                        item.resolution.release.locators[0]["payload"][
                            "artifact_sha256"
                        ]
                        for item in transformed
                    },
                    {"a" * 64},
                )
                self.assertEqual(
                    len(
                        await repository.brokered_artifacts(
                            ids.candidate_id,
                            ids.locator_ids[source_locator_id],
                            owner_configuration_partition=owner,
                        )
                    ),
                    1,
                )
            finally:
                await database.disconnect()
