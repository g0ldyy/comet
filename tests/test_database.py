import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import comet.core.database as database_module


@asynccontextmanager
async def unlocked_schema_migration():
    yield


class DatabaseSetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_migration_failure_disconnects_open_database(self):
        connect = AsyncMock()
        disconnect = AsyncMock()
        migration = AsyncMock(side_effect=RuntimeError("migration failed"))

        with (
            patch.object(database_module, "IS_SQLITE", False),
            patch.object(database_module, "IS_POSTGRES", True),
            patch.object(database_module.database, "connect", new=connect),
            patch.object(database_module.database, "disconnect", new=disconnect),
            patch.object(
                database_module,
                "_schema_migration_lock",
                new=unlocked_schema_migration,
            ),
            patch.object(database_module, "run_schema_migrations", new=migration),
        ):
            with self.assertRaisesRegex(RuntimeError, "migration failed"):
                await database_module.setup_database()

        connect.assert_awaited_once_with()
        migration.assert_awaited_once_with(
            database_module.database,
            is_sqlite=False,
            is_postgres=True,
        )
        disconnect.assert_awaited_once_with()
