import asyncio
import unittest
from tempfile import TemporaryDirectory

from databases import Database

from comet.core.db_router import ReplicaAwareDatabase
from comet.core.provider_governor import ProviderGovernor
from comet.core.schema_migrations import (
    MigrationContext,
    _ensure_usenet_schema,
)


class ProviderGovernorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary_directory = TemporaryDirectory()
        self.database = ReplicaAwareDatabase(
            Database(f"sqlite+aiosqlite:///{self.temporary_directory.name}/governor.db")
        )
        await self.database.connect()
        await _ensure_usenet_schema(
            MigrationContext(
                self.database,
                is_sqlite=True,
                is_postgres=False,
            )
        )
        self.governor = ProviderGovernor(self.database)
        self.scope = b"a" * 32

    async def asyncTearDown(self):
        await self.database.disconnect()
        self.temporary_directory.cleanup()

    async def test_window_preserves_interactive_reserve_and_hard_limit(self):
        background = [
            await self.governor.acquire_window(
                self.scope,
                "query",
                limit=3,
                window_seconds=60,
                priority="background",
                interactive_reserve=1,
                now=1,
            )
            for _ in range(3)
        ]
        interactive = [
            await self.governor.acquire_window(
                self.scope,
                "query",
                limit=3,
                window_seconds=60,
                now=1,
            )
            for _ in range(2)
        ]

        self.assertIsNotNone(background[0])
        self.assertIsNotNone(background[1])
        self.assertIsNone(background[2])
        self.assertIsNotNone(interactive[0])
        self.assertIsNone(interactive[1])
        self.assertEqual(interactive[0].reset_at_ms, 60_000)

    async def test_concurrent_acquisitions_never_exceed_window_or_slot_limits(self):
        windows = await asyncio.gather(
            *(
                self.governor.acquire_window(
                    self.scope,
                    "parallel_query",
                    limit=5,
                    window_seconds=60,
                    now=1,
                )
                for _ in range(20)
            )
        )
        leases = await asyncio.gather(
            *(
                self.governor.acquire_concurrency(
                    self.scope,
                    "parallel_fetch",
                    limit=3,
                    owner_request_id=f"request-{index}",
                    lease_seconds=10,
                    now=1,
                )
                for index in range(12)
            )
        )

        admitted_windows = [permit for permit in windows if permit is not None]
        admitted_leases = [lease for lease in leases if lease is not None]
        self.assertEqual(len(admitted_windows), 5)
        self.assertEqual(
            sorted(permit.used for permit in admitted_windows),
            [1, 2, 3, 4, 5],
        )
        self.assertEqual(len(admitted_leases), 3)
        self.assertEqual(
            {lease.slot for lease in admitted_leases},
            {0, 1, 2},
        )

    async def test_provider_limit_can_tighten_below_already_used_count(self):
        for _ in range(3):
            self.assertIsNotNone(
                await self.governor.acquire_window(
                    self.scope,
                    "search",
                    limit=5,
                    window_seconds=60,
                    now=1,
                )
            )

        changed = await self.governor.tighten_window(
            self.scope,
            "search",
            limit=2,
            window_seconds=60,
            now=1,
        )
        denied = await self.governor.acquire_window(
            self.scope,
            "search",
            limit=5,
            window_seconds=60,
            now=1,
        )
        row = await self.database.fetch_one(
            """
            SELECT limit_count, used_count
            FROM provider_governor_windows
            WHERE operation = 'search'
            """
        )

        self.assertTrue(changed)
        self.assertIsNone(denied)
        self.assertEqual(dict(row), {"limit_count": 2, "used_count": 3})

    async def test_concurrency_slots_release_and_reclaim_expired_leases(self):
        first = await self.governor.acquire_concurrency(
            self.scope,
            "easynews_v3",
            limit=2,
            owner_request_id="request-1",
            lease_seconds=10,
            now=1,
        )
        second = await self.governor.acquire_concurrency(
            self.scope,
            "easynews_v3",
            limit=2,
            owner_request_id="request-2",
            lease_seconds=10,
            now=1,
        )
        denied = await self.governor.acquire_concurrency(
            self.scope,
            "easynews_v3",
            limit=2,
            owner_request_id="request-3",
            lease_seconds=10,
            now=1,
        )
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertIsNone(denied)

        await first.release()
        replacement = await self.governor.acquire_concurrency(
            self.scope,
            "easynews_v3",
            limit=2,
            owner_request_id="request-4",
            lease_seconds=10,
            now=2,
        )
        self.assertEqual(replacement.slot, first.slot)

        reclaimed = await self.governor.acquire_concurrency(
            self.scope,
            "easynews_v3",
            limit=2,
            owner_request_id="request-5",
            lease_seconds=10,
            now=12,
        )
        self.assertIsNotNone(reclaimed)
        await second.release()
        row = await self.database.fetch_one(
            """
            SELECT lease_id FROM provider_governor_leases
            WHERE lease_id = :lease_id
            """,
            {"lease_id": reclaimed.lease_id},
        )
        self.assertIsNotNone(row)

    async def test_bounded_gc_removes_expired_leases_and_windows(self):
        await self.governor.acquire_window(
            self.scope,
            "query",
            limit=1,
            window_seconds=1,
            now=0,
        )
        await self.governor.acquire_concurrency(
            self.scope,
            "query",
            limit=1,
            owner_request_id="request",
            lease_seconds=1,
            now=0,
        )

        removed = await self.governor.collect_expired(now=3, batch_size=2)

        self.assertEqual(removed, (1, 1))
        self.assertEqual(
            await self.database.fetch_all(
                "SELECT lease_id FROM provider_governor_leases"
            ),
            [],
        )
        self.assertEqual(
            await self.database.fetch_all(
                "SELECT scope_key FROM provider_governor_windows"
            ),
            [],
        )
