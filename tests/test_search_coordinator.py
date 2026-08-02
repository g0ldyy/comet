import asyncio
import unittest
from dataclasses import replace
from tempfile import TemporaryDirectory
from unittest.mock import patch

from databases import Database

from comet.core.capabilities import (
    CapabilityPlan,
    CapabilityPlanner,
    EligibleDiscovery,
    EligibleProvider,
)
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
    TorrentLocator,
    TransportKind,
)
from comet.discovery.capabilities import DiscoveryBranchFingerprint
from comet.discovery.manager import SearchCoordinator
from comet.discovery.models import DiscoveryBatch, MediaQuery
from comet.services.lock import DistributedLock
from comet.usenet.access import NativeAccessAuthorizer


class FakeAdapter:
    def __init__(self, batch=None, error=None):
        self.batch = batch or DiscoveryBatch()
        self.error = error
        self.contexts = []

    async def search(self, query, context):
        self.contexts.append(context)
        if self.error:
            raise self.error
        return self.batch


def _torrent_candidate():
    return ReleaseCandidate(
        candidate_id="candidate",
        media_id="tt1",
        scope=ReleaseScope.MOVIE,
        transport=TransportKind.BITTORRENT,
        title="release",
        locators=(
            TorrentLocator(
                locator_id="torrent",
                kind=LocatorKind.TORRENT,
                policy=LocatorPolicy(frozenset({"direct_torrent"})),
                info_hash="a" * 40,
            ),
        ),
    )


def _usenet_candidate():
    return ReleaseCandidate(
        candidate_id="usenet-candidate",
        media_id="tt1",
        scope=ReleaseScope.MOVIE,
        transport=TransportKind.USENET,
        title="usenet release",
        locators=(
            RealNzbRef(
                locator_id="nzb",
                kind=LocatorKind.REAL_NZB,
                policy=LocatorPolicy(frozenset({"stremio_nntp"})),
                adapter_configuration_id="11111111-1111-4111-8111-111111111111",
                remote_guid="guid",
            ),
        ),
    )


def _branch_identity(
    source_configuration_id: str,
    branch_family: str,
    fingerprint: str,
    *,
    public_visibility: bool = False,
) -> DiscoveryBranchFingerprint:
    return DiscoveryBranchFingerprint(
        source_configuration_id,
        branch_family,
        fingerprint,
        public_visibility,
    )


def _torrent_plan(
    *source_ids: str,
    display_name: str | None = None,
) -> CapabilityPlan:
    return CapabilityPlan(
        transports=frozenset({TransportKind.BITTORRENT}),
        discovery_source_ids=source_ids,
        providers=(EligibleProvider("direct", "direct_torrent", 0),),
        diagnostics=(),
        discovery=tuple(
            EligibleDiscovery(
                source_id,
                frozenset({TransportKind.BITTORRENT}),
                display_name=display_name,
            )
            for source_id in source_ids
        ),
    )


class SearchCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_reachable_sources_are_called_and_partial_failure_is_safe(self):
        plan = _torrent_plan("ok", "failed")
        successful = FakeAdapter(
            DiscoveryBatch(
                candidates=(_torrent_candidate(),), coverage=frozenset({"bittorrent"})
            )
        )
        failed = FakeAdapter(error=RuntimeError("outage"))
        disabled = FakeAdapter()

        result = await SearchCoordinator(
            {"ok": successful, "failed": failed, "off": disabled}
        ).search(
            MediaQuery("tt1", "movie"),
            plan,
            branch_fingerprints={},
        )

        self.assertEqual(result.candidates, (_torrent_candidate(),))
        self.assertEqual(result.diagnostics, ())
        self.assertEqual(len(successful.contexts), 1)
        self.assertEqual(len(failed.contexts), 1)
        self.assertEqual(disabled.contexts, [])
        self.assertEqual(successful.contexts[0].configuration_id, "ok")
        self.assertIsNotNone(successful.contexts[0].hard_deadline)

    async def test_planned_source_requires_its_adapter(self):
        plan = CapabilityPlan(
            transports=frozenset({TransportKind.BITTORRENT}),
            discovery_source_ids=("missing",),
            providers=(EligibleProvider("direct", "direct_torrent", 0),),
            diagnostics=(),
            discovery=(
                EligibleDiscovery(
                    "missing",
                    frozenset({TransportKind.BITTORRENT}),
                ),
            ),
        )

        with self.assertRaisesRegex(ValueError, "adapter is unavailable"):
            await SearchCoordinator({}).search(MediaQuery("tt1", "movie"), plan)

    def test_release_candidate_requires_typed_transport(self):
        with self.assertRaisesRegex(ValueError, "transport is invalid"):
            replace(_torrent_candidate(), transport="bittorrent")

    async def test_configured_source_name_replaces_the_technical_adapter_label(self):
        config = {
            "schemaVersion": 2,
            "enabledTransports": ["usenet"],
            "playbackProviders": [
                {
                    "configurationId": "nntp",
                    "displayName": "NNTP",
                    "kind": "stremio_nntp",
                    "enabled": True,
                }
            ],
            "discoverySources": [
                {
                    "configurationId": "indexer",
                    "displayName": "My indexer",
                    "kind": "newznab",
                    "enabled": True,
                }
            ],
        }
        plan = CapabilityPlanner(
            usenet_offered=True,
            native_authorizer=NativeAccessAuthorizer(None),
        ).build(config)
        candidate = _usenet_candidate()
        adapter = FakeAdapter(
            DiscoveryBatch(
                candidates=(candidate,),
                coverage=frozenset({"usenet"}),
            )
        )
        result = await SearchCoordinator({"indexer": adapter}).search(
            MediaQuery("tt1", "movie"),
            plan,
        )

        self.assertEqual(result.candidates[0].source, "My indexer")
        self.assertEqual(candidate.source, "")

    async def test_global_failure_is_aggregated_in_configuration_order(self):
        plan = _torrent_plan("first", "second")

        result = await SearchCoordinator(
            {
                "first": FakeAdapter(error=RuntimeError("first outage")),
                "second": FakeAdapter(error=RuntimeError("second outage")),
            }
        ).search(MediaQuery("tt1", "movie"), plan)

        self.assertEqual(result.candidates, ())
        self.assertEqual(
            result.diagnostics,
            ("Discovery is temporarily unavailable",),
        )

    async def test_hard_deadline_cancels_pending_adapters_and_sets_the_signal(self):
        observed = asyncio.Event()

        class SlowAdapter:
            async def search(self, _query, context):
                try:
                    await asyncio.sleep(60)
                finally:
                    if context.cancelled():
                        observed.set()

        plan = _torrent_plan("slow")

        result = await SearchCoordinator(
            {"slow": SlowAdapter()},
            hard_timeout=0.02,
        ).search(MediaQuery("tt1", "movie"), plan, trace_id="trace-safe")

        self.assertTrue(observed.is_set())
        self.assertEqual(
            result.diagnostics,
            ("Discovery is temporarily unavailable",),
        )

    async def test_result_cardinality_and_diagnostics_are_bounded(self):
        plan = _torrent_plan("indexer")
        adapter = FakeAdapter(
            DiscoveryBatch(
                candidates=(
                    _torrent_candidate(),
                    _torrent_candidate(),
                    _torrent_candidate(),
                    _torrent_candidate(),
                ),
                diagnostics=(
                    "bounded diagnostic",
                    "private\nvalue",
                    "x" * 513,
                ),
            )
        )

        with patch("comet.discovery.manager.MAX_DISCOVERY_CANDIDATES", 2):
            result = await SearchCoordinator({"indexer": adapter}).search(
                MediaQuery("tt1", "movie"),
                plan,
            )

        self.assertEqual(len(result.candidates), 2)
        self.assertEqual(result.diagnostics, ("bounded diagnostic",))

    async def test_malformed_candidate_surfaces_as_an_internal_contract_failure(self):
        plan = CapabilityPlan(
            transports=frozenset({TransportKind.BITTORRENT}),
            discovery_source_ids=("indexer",),
            providers=(EligibleProvider("direct", "direct_torrent", 0),),
            diagnostics=(),
            discovery=(
                EligibleDiscovery(
                    "indexer",
                    frozenset({TransportKind.BITTORRENT}),
                ),
            ),
        )
        with self.assertRaises(AttributeError):
            await SearchCoordinator(
                {"indexer": FakeAdapter(DiscoveryBatch(candidates=(object(),)))}
            ).search(MediaQuery("tt1", "movie"), plan)

    def test_rejects_unbounded_deadline(self):
        with self.assertRaisesRegex(ValueError, "deadline is invalid"):
            SearchCoordinator({}, hard_timeout=61)


class CachedSearchCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary_directory = TemporaryDirectory()
        self.database = ReplicaAwareDatabase(
            Database(
                f"sqlite+aiosqlite:///{self.temporary_directory.name}/coordinator.db"
            )
        )
        await self.database.connect()
        context = MigrationContext(
            self.database,
            is_sqlite=True,
            is_postgres=False,
        )
        await _ensure_managed_table(context, SCRAPE_LOCKS_TABLE_SPEC)
        await _ensure_usenet_schema(context)
        self.source_id = "11111111-1111-4111-8111-111111111111"
        self.partition = b"a" * 32
        self.fingerprint = "b" * 64
        self.plan = _torrent_plan(self.source_id, display_name="Cached source")

    async def asyncTearDown(self):
        await self.database.disconnect()
        self.temporary_directory.cleanup()

    def coordinator(self, adapter, background_task_adder=None):
        return SearchCoordinator(
            {self.source_id: adapter},
            database=self.database,
            background_task_adder=background_task_adder,
            hard_timeout=1,
        )

    async def search(self, coordinator):
        return await coordinator.search(
            MediaQuery("tt1", "movie"),
            self.plan,
            account_partition=self.partition,
            branch_fingerprints={
                (self.source_id, "bittorrent"): _branch_identity(
                    self.source_id,
                    "bittorrent",
                    self.fingerprint,
                )
            },
        )

    async def test_fresh_branch_uses_persisted_results_without_adapter_call(self):
        adapter = FakeAdapter(
            DiscoveryBatch(
                candidates=(_torrent_candidate(),),
                coverage=frozenset({"bittorrent"}),
            )
        )

        cold = await self.search(self.coordinator(adapter))
        fresh = await self.search(self.coordinator(adapter))

        self.assertEqual(len(adapter.contexts), 1, (cold, fresh))
        self.assertEqual(len(cold.candidates), 1)
        self.assertEqual(len(fresh.candidates), 1)
        self.assertEqual(cold.candidates[0].source, "Cached source")
        self.assertEqual(fresh.candidates[0].source, "Cached source")
        self.assertNotEqual(
            fresh.candidates[0].candidate_id,
            _torrent_candidate().candidate_id,
        )

    async def test_stale_branch_returns_immediately_and_schedules_one_refresh(self):
        adapter = FakeAdapter(
            DiscoveryBatch(
                candidates=(_torrent_candidate(),),
                coverage=frozenset({"bittorrent"}),
            )
        )
        await self.search(self.coordinator(adapter))
        await self.database.execute("UPDATE search_coverage SET next_refresh_at = 0")
        scheduled = []

        def add_background_task(function, *args):
            scheduled.append((function, args))

        stale = await self.search(
            self.coordinator(adapter, background_task_adder=add_background_task)
        )

        self.assertEqual(len(stale.candidates), 1)
        self.assertEqual(len(adapter.contexts), 1)
        self.assertEqual(len(scheduled), 1)
        function, args = scheduled[0]
        await function(*args)
        self.assertEqual(len(adapter.contexts), 2)

    async def test_two_branches_mix_cached_and_cold_results_in_branch_order(self):
        """One source, two branches, one warm and one cold in the same search."""
        usenet_fingerprint = "d" * 64
        plan = CapabilityPlan(
            transports=frozenset({TransportKind.BITTORRENT, TransportKind.USENET}),
            discovery_source_ids=(self.source_id,),
            providers=(
                EligibleProvider("direct", "direct_torrent", 0),
                EligibleProvider("newz", "stremio_nntp", 1),
            ),
            diagnostics=(),
            discovery=(
                EligibleDiscovery(
                    self.source_id,
                    frozenset({TransportKind.BITTORRENT, TransportKind.USENET}),
                ),
            ),
        )
        fingerprints = {
            (self.source_id, "bittorrent"): _branch_identity(
                self.source_id,
                "bittorrent",
                self.fingerprint,
            ),
            (self.source_id, "usenet"): _branch_identity(
                self.source_id,
                "usenet",
                usenet_fingerprint,
            ),
        }

        async def run(adapter):
            return await self.coordinator(adapter).search(
                MediaQuery("tt1", "movie"),
                plan,
                account_partition=self.partition,
                branch_fingerprints=fingerprints,
            )

        both = FakeAdapter(
            DiscoveryBatch(
                candidates=(_torrent_candidate(), _usenet_candidate()),
                coverage=frozenset({"bittorrent", "usenet"}),
            )
        )
        cold = await run(both)
        self.assertEqual(len(cold.candidates), 2)

        await self.database.execute(
            "UPDATE search_coverage SET next_refresh_at = 0 "
            "WHERE branch_fingerprint = :branch",
            {"branch": usenet_fingerprint},
        )
        await self.database.execute(
            "DELETE FROM search_coverage WHERE branch_fingerprint = :branch",
            {"branch": usenet_fingerprint},
        )

        refetch = FakeAdapter(
            DiscoveryBatch(
                candidates=(_usenet_candidate(),),
                coverage=frozenset({"usenet"}),
            )
        )
        mixed = await run(refetch)

        self.assertEqual(len(refetch.contexts), 1)
        self.assertEqual(
            {branch for context in refetch.contexts for branch in context.branches},
            {"usenet"},
        )
        self.assertEqual(
            sorted(candidate.transport for candidate in mixed.candidates),
            sorted((TransportKind.BITTORRENT, TransportKind.USENET)),
        )

    async def test_concurrent_cold_searches_share_database_singleflight(self):
        started = asyncio.Event()
        release = asyncio.Event()
        second_lock_attempt = asyncio.Event()
        lock_attempts = 0
        acquire = DistributedLock.acquire

        async def observed_acquire(lock, *args, **kwargs):
            nonlocal lock_attempts
            lock_attempts += 1
            if lock_attempts == 2:
                second_lock_attempt.set()
            return await acquire(lock, *args, **kwargs)

        class DelayedAdapter(FakeAdapter):
            async def search(self, query, context):
                self.contexts.append(context)
                started.set()
                await release.wait()
                return self.batch

        adapter = DelayedAdapter(
            DiscoveryBatch(
                candidates=(_torrent_candidate(),),
                coverage=frozenset({"bittorrent"}),
            )
        )
        with patch.object(DistributedLock, "acquire", new=observed_acquire):
            first = asyncio.create_task(self.search(self.coordinator(adapter)))
            await started.wait()
            second = asyncio.create_task(self.search(self.coordinator(adapter)))
            await second_lock_attempt.wait()
            release.set()
            results = await asyncio.gather(first, second)

        self.assertEqual(len(adapter.contexts), 1)
        self.assertTrue(all(len(result.candidates) == 1 for result in results))

    async def test_initial_failure_honors_retry_backoff(self):
        adapter = FakeAdapter(error=RuntimeError("outage"))
        coordinator = self.coordinator(adapter)

        first = await self.search(coordinator)
        failure = await self.database.fetch_one(
            "SELECT freshness_state, next_refresh_at FROM search_coverage"
        )
        waiting = await self.search(coordinator)

        self.assertEqual(failure["freshness_state"], "failed")
        self.assertEqual(len(adapter.contexts), 1)
        self.assertEqual(first.candidates, ())
        self.assertEqual(waiting.candidates, ())
        self.assertIn(
            "Discovery source is temporarily unavailable",
            waiting.diagnostics,
        )

        await self.database.execute("UPDATE search_coverage SET next_refresh_at = 0")
        await self.search(coordinator)
        self.assertEqual(len(adapter.contexts), 2)

    async def test_public_branch_cache_is_shared_without_cross_owner_destruction(self):
        adapter = FakeAdapter(
            DiscoveryBatch(
                candidates=(_torrent_candidate(),),
                coverage=frozenset({"bittorrent"}),
            )
        )
        identity = _branch_identity(
            self.source_id,
            "bittorrent",
            self.fingerprint,
            public_visibility=True,
        )

        async def search(coordinator, partition):
            return await coordinator.search(
                MediaQuery("tt1", "movie"),
                self.plan,
                account_partition=partition,
                branch_fingerprints={
                    (self.source_id, "bittorrent"): identity,
                },
            )

        owner_a = b"a" * 32
        owner_b = b"b" * 32
        cold_a = await search(self.coordinator(adapter), owner_a)
        fresh_b = await search(self.coordinator(adapter), owner_b)

        self.assertEqual(len(cold_a.candidates), 1)
        self.assertEqual(len(fresh_b.candidates), 1)
        self.assertEqual(len(adapter.contexts), 1)
        self.assertIsNone(adapter.contexts[0].account_partition)

        await self.database.execute("UPDATE search_coverage SET next_refresh_at = 0")
        scheduled = []

        def add_background_task(function, *args):
            scheduled.append((function, args))

        stale_b = await search(
            self.coordinator(adapter, background_task_adder=add_background_task),
            owner_b,
        )
        self.assertEqual(len(stale_b.candidates), 1)
        self.assertEqual(len(scheduled), 1)
        function, args = scheduled[0]
        await function(*args)

        refreshed_a = await search(self.coordinator(adapter), owner_a)
        self.assertEqual(len(refreshed_a.candidates), 1)
        self.assertEqual(len(adapter.contexts), 2)
        self.assertTrue(
            all(context.account_partition is None for context in adapter.contexts)
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
            """
        )
        public_partition = "0" * 64
        self.assertEqual(row["visibility_partition"], public_partition)
        self.assertEqual(
            row["owner_configuration_partition"],
            public_partition,
        )
        self.assertIsNone(row["account_partition"])
