import unittest
from unittest.mock import AsyncMock, Mock, patch

from databases import Database

from comet.core.db_router import ReplicaAwareDatabase


class FailingTransaction:
    async def __aenter__(self):
        raise RuntimeError("transaction connection failed")

    async def __aexit__(self, exc_type, exc, tb):
        return False


class ReplicaAwareDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_sqlite_transactions_reserve_the_writer_at_entry(self):
        primary = Mock()
        primary.url = "sqlite+aiosqlite:///comet.db"
        router = ReplicaAwareDatabase(primary)

        router.transaction()

        primary.transaction.assert_called_once_with(sqlite_begin_immediate=True)

    async def test_postgres_transactions_keep_native_transaction_semantics(self):
        primary = Mock()
        primary.url = "postgresql+asyncpg://database/comet"
        router = ReplicaAwareDatabase(primary)

        router.transaction()

        primary.transaction.assert_called_once_with()

    async def test_failed_transaction_entry_does_not_pin_reads_to_primary(self):
        primary = Mock()
        primary.transaction.return_value = FailingTransaction()
        primary.fetch_one = AsyncMock(return_value={"source": "primary"})
        primary.is_connected = True
        replica = Mock()
        replica.fetch_one = AsyncMock(return_value={"source": "replica"})
        router = ReplicaAwareDatabase(primary, [replica])
        router._active_replicas = [replica]

        with self.assertRaisesRegex(RuntimeError, "transaction connection failed"):
            async with router.transaction():
                pass

        result = await router.fetch_one("SELECT 1")

        self.assertEqual(result, {"source": "replica"})
        replica.fetch_one.assert_awaited_once_with("SELECT 1", None)
        primary.fetch_one.assert_not_awaited()

    async def test_failed_replica_is_quarantined_before_retry(self):
        secret = "postgresql://reader:secret@replica/internal"
        primary = Mock()
        primary.fetch_one = AsyncMock(return_value={"source": "primary"})
        primary.is_connected = True
        replica = Mock()
        replica.fetch_one = AsyncMock(
            side_effect=[RuntimeError(secret), {"source": "replica"}]
        )
        router = ReplicaAwareDatabase(primary, [replica])
        router._active_replicas = [replica]
        first = await router.fetch_one("SELECT 1")
        second = await router.fetch_one("SELECT 1")
        router._replica_retry_after[replica] = 0
        third = await router.fetch_one("SELECT 1")

        self.assertEqual(first, {"source": "primary"})
        self.assertEqual(second, {"source": "primary"})
        self.assertEqual(third, {"source": "replica"})
        self.assertEqual(replica.fetch_one.await_count, 2)
        self.assertEqual(primary.fetch_one.await_count, 2)
        self.assertEqual(router._active_replicas, [replica])

    async def test_failed_replica_connection_does_not_log_its_url(self):
        secret = "postgresql://reader:secret@replica/internal"
        primary = Mock(is_connected=False)
        primary.connect = AsyncMock()
        replica = Mock(is_connected=False)
        replica.url = secret
        replica.connect = AsyncMock(side_effect=RuntimeError(secret))
        router = ReplicaAwareDatabase(primary, [replica])
        await router.connect()

    async def test_initially_failed_replica_is_reconnected_and_reports_recovery(self):
        primary = Mock(is_connected=False)
        primary.connect = AsyncMock()
        primary.fetch_one = AsyncMock(return_value={"source": "primary"})
        replica = Mock(is_connected=False)
        replica.connect = AsyncMock(
            side_effect=[RuntimeError("private replica detail"), None]
        )
        replica.fetch_one = AsyncMock(return_value={"source": "replica"})
        router = ReplicaAwareDatabase(primary, [replica])
        with (
            patch("comet.core.db_router.log.warning") as warning,
            patch("comet.core.db_router.log.info") as info,
        ):
            await router.connect()
            router._replica_retry_after[replica] = 0
            result = await router.fetch_one("SELECT 1")

        self.assertEqual(result, {"source": "replica"})
        self.assertEqual(replica.connect.await_count, 2)
        self.assertEqual(warning.call_count, 1)
        self.assertEqual(info.call_count, 1)
        self.assertEqual(info.call_args.args[0], "database.replica.recovered")

    async def test_ipv4_resolution_is_async_bounded_and_preserves_credentials(self):
        source = Database("postgresql+asyncpg://operator:secret@db.example:5432/comet")
        loop = Mock()
        loop.getaddrinfo = AsyncMock(
            return_value=[
                (
                    2,
                    1,
                    6,
                    "",
                    ("203.0.113.10", 5432),
                )
            ]
        )
        router = ReplicaAwareDatabase(source, force_ipv4=True)

        with (
            patch("comet.core.db_router.asyncio.get_running_loop", return_value=loop),
            patch("comet.core.db_router.Database") as database_class,
        ):
            replacement = await router._resolve_and_recreate(source)

        loop.getaddrinfo.assert_awaited_once_with(
            "db.example",
            5432,
            family=2,
            type=1,
        )
        rendered = database_class.call_args.args[0]
        self.assertIn(
            "postgresql+asyncpg://operator:secret@203.0.113.10:5432/comet", rendered
        )
        self.assertIs(replacement, database_class.return_value)
