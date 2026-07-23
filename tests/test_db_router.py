import unittest
from unittest.mock import AsyncMock, Mock

from comet.core.db_router import ReplicaAwareDatabase


class FailingTransaction:
    async def __aenter__(self):
        raise RuntimeError("transaction connection failed")

    async def __aexit__(self, exc_type, exc, tb):
        return False


class ReplicaAwareDatabaseTests(unittest.IsolatedAsyncioTestCase):
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
        primary = Mock()
        primary.fetch_one = AsyncMock(return_value={"source": "primary"})
        primary.is_connected = True
        replica = Mock()
        replica.fetch_one = AsyncMock(
            side_effect=[RuntimeError("replica offline"), {"source": "replica"}]
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
