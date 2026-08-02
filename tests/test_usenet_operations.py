import asyncio
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

from databases import Database

import comet.services.usenet_operations as operations
from comet.core.db_router import ReplicaAwareDatabase
from comet.core.schema_migrations import run_schema_migrations


class UsenetOperationMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = TemporaryDirectory()
        self.database = ReplicaAwareDatabase(
            Database(f"sqlite+aiosqlite:///{self.temp_dir.name}/usenet-operations.db")
        )
        await self.database.connect()
        await run_schema_migrations(
            self.database,
            is_sqlite=True,
            is_postgres=False,
        )
        self.database_patch = patch.object(operations, "database", self.database)
        self.database_patch.start()
        self.monitor = operations.UsenetOperationMonitor()

    async def asyncTearDown(self):
        self.database_patch.stop()
        await self.database.disconnect()
        self.temp_dir.cleanup()

    async def test_progress_and_terminal_history_share_one_operation_identity(self):
        operation_id = await self.monitor.start(
            client_ip="192.0.2.1",
            content_id="tt123",
            title="Example",
            member_path="Example.mkv",
            source_kind="session",
            total_bytes=100,
        )
        self.monitor.add_bytes(operation_id, 40)
        await self.monitor._sync_operations()
        row = await self.database.fetch_one(
            """
            SELECT bytes_transferred
            FROM usenet_active_operations
            WHERE id = :id
            """,
            {"id": operation_id},
        )
        self.assertEqual(row["bytes_transferred"], 40)

        await self.monitor.finish(operation_id, outcome="completed")
        history = await self.database.fetch_one(
            """
            SELECT bytes_transferred, outcome
            FROM usenet_operation_history
            WHERE id = :id
            """,
            {"id": operation_id},
        )
        self.assertEqual(
            dict(history),
            {"bytes_transferred": 40, "outcome": "completed"},
        )

    async def test_owned_cancel_flag_cancels_only_the_bound_request(self):
        operation_id = await self.monitor.start(
            client_ip="192.0.2.1",
            content_id="tt123",
            title="Example",
            member_path="Example.mkv",
            source_kind="raw_composite",
            total_bytes=100,
        )
        started = asyncio.Event()

        async def request():
            started.set()
            await asyncio.Event().wait()

        async def stream():
            try:
                await self.monitor.run_cancellable(operation_id, request())
            except asyncio.CancelledError:
                return asyncio.current_task().cancelling()

        task = asyncio.create_task(stream())
        await started.wait()
        await self.database.execute(
            """
            UPDATE usenet_active_operations
            SET cancel_requested = 1
            WHERE id = :id
            """,
            {"id": operation_id},
        )

        await self.monitor._sync_operations()

        self.assertEqual(await task, 0)
        self.assertTrue(self.monitor._operations[operation_id].admin_cancelled)
        await self.monitor.finish(operation_id, outcome="failed")
        outcome = await self.database.fetch_val(
            "SELECT outcome FROM usenet_operation_history WHERE id = :id",
            {"id": operation_id},
        )
        self.assertEqual(outcome, "cancelled")
