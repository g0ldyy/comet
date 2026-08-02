import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, call, patch

import comet.core.database as database_module


@asynccontextmanager
async def unlocked_schema_migration():
    yield


class DatabaseSetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_backend_lock_rejects_unsupported_backend(self):
        with (
            patch.object(database_module, "IS_SQLITE", False),
            patch.object(database_module, "IS_POSTGRES", False),
        ):
            with self.assertRaisesRegex(RuntimeError, "backend is unsupported"):
                async with database_module.backend_lock(
                    postgres_lock_id=1,
                    sqlite_lock_path="unused",
                    wait_message="unused",
                ):
                    self.fail("unsupported backend lock yielded")

    async def test_migration_failure_disconnects_open_database(self):
        connect = AsyncMock()
        disconnect = AsyncMock()
        secret = "postgresql://operator:secret@database/internal"
        migration = AsyncMock(side_effect=RuntimeError(secret))

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
            with self.assertRaisesRegex(RuntimeError, "operator:secret"):
                await database_module.setup_database()

        connect.assert_awaited_once_with()
        migration.assert_awaited_once()
        args, kwargs = migration.await_args
        self.assertEqual(args, (database_module.database,))
        self.assertEqual(kwargs["is_sqlite"], False)
        self.assertEqual(kwargs["is_postgres"], True)
        self.assertTrue(callable(kwargs["on_pending"]))
        disconnect.assert_awaited_once_with()

    async def test_current_sqlite_schema_skips_exclusive_migration_mode(self):
        connect = AsyncMock()
        pending = AsyncMock(return_value=False)
        pragmas = AsyncMock()
        execute = AsyncMock()
        cleanup = AsyncMock()

        with (
            patch.object(database_module, "IS_SQLITE", True),
            patch.object(database_module, "IS_POSTGRES", False),
            patch.object(database_module.settings, "DATABASE_PATH", ""),
            patch.object(database_module, "_prepare_sqlite_database_file") as prepare,
            patch.object(
                database_module._models_mod,
                "set_comet_foreign_keys_enabled",
            ),
            patch.object(database_module.database, "connect", new=connect),
            patch.object(database_module.database, "execute", new=execute),
            patch.object(
                database_module,
                "_schema_migration_lock",
                new=unlocked_schema_migration,
            ),
            patch.object(
                database_module,
                "has_pending_schema_migrations",
                new=pending,
            ),
            patch.object(
                database_module,
                "_apply_sqlite_pragmas",
                new=pragmas,
            ),
            patch.object(
                database_module,
                "run_schema_migrations",
                new=AsyncMock(),
            ) as migration,
            patch.object(database_module, "_run_startup_cleanup", new=cleanup),
        ):
            await database_module.setup_database()

        pending.assert_awaited_once_with(
            database_module.database,
            is_sqlite=True,
            is_postgres=False,
        )
        migration.assert_not_awaited()
        pragmas.assert_not_awaited()
        cleanup.assert_awaited_once_with()
        prepare.assert_called_once_with("")

    async def test_pending_sqlite_schema_uses_exclusive_migration_mode_once(self):
        pending = AsyncMock(return_value=True)
        pragmas = AsyncMock()
        migration = AsyncMock()

        with (
            patch.object(database_module, "IS_SQLITE", True),
            patch.object(database_module, "IS_POSTGRES", False),
            patch.object(database_module.settings, "DATABASE_PATH", ""),
            patch.object(database_module, "_prepare_sqlite_database_file") as prepare,
            patch.object(
                database_module._models_mod,
                "set_comet_foreign_keys_enabled",
            ),
            patch.object(database_module.database, "connect", new=AsyncMock()),
            patch.object(database_module.database, "execute", new=AsyncMock()),
            patch.object(
                database_module,
                "_schema_migration_lock",
                new=unlocked_schema_migration,
            ),
            patch.object(
                database_module,
                "has_pending_schema_migrations",
                new=pending,
            ),
            patch.object(
                database_module,
                "_apply_sqlite_pragmas",
                new=pragmas,
            ),
            patch.object(
                database_module,
                "run_schema_migrations",
                new=migration,
            ),
            patch.object(
                database_module,
                "_run_startup_cleanup",
                new=AsyncMock(),
            ),
        ):
            await database_module.setup_database()

        migration.assert_awaited_once_with(
            database_module.database,
            is_sqlite=True,
            is_postgres=False,
        )
        self.assertEqual(
            pragmas.await_args_list,
            [
                call(
                    foreign_keys=False,
                    journal_mode=database_module._SQLITE_MIGRATION_JOURNAL_MODE,
                ),
                call(
                    foreign_keys=True,
                    journal_mode=database_module._SQLITE_DEFAULT_JOURNAL_MODE,
                ),
            ],
        )
        prepare.assert_called_once_with("")

    async def test_sqlite_pragmas_retain_durability_and_deleted_value_hygiene(self):
        execute = AsyncMock()
        with (
            patch.object(database_module.database, "execute", new=execute),
            patch.object(
                database_module._models_mod,
                "apply_sqlite_connection_pragmas",
                new=AsyncMock(),
            ),
        ):
            await database_module._apply_sqlite_pragmas(
                foreign_keys=True,
            )

        queries = [call.args[0] for call in execute.await_args_list]
        self.assertIn("PRAGMA synchronous=NORMAL", queries)
        self.assertIn("PRAGMA secure_delete=FAST", queries)
        self.assertNotIn("PRAGMA synchronous=OFF", queries)
        self.assertNotIn("PRAGMA secure_delete=OFF", queries)

    def test_sqlite_database_file_is_private_regular_and_nonfollowing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "comet.db"
            database_module._prepare_sqlite_database_file(str(path))
            self.assertTrue(path.is_file())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

            path.chmod(0o644)
            database_module._prepare_sqlite_database_file(str(path))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

            target = Path(directory) / "target.db"
            target.write_bytes(b"not-the-database")
            symlink = Path(directory) / "linked.db"
            os.symlink(target, symlink)
            with self.assertRaises(OSError):
                database_module._prepare_sqlite_database_file(str(symlink))
            self.assertEqual(target.read_bytes(), b"not-the-database")

    async def test_startup_cleanup_preserves_alias_only_metadata_rows(self):
        delete_where = AsyncMock()
        with (
            patch.object(
                database_module,
                "_perform_usenet_lifecycle_cleanup",
                new=AsyncMock(),
            ),
            patch.object(
                database_module.database,
                "execute",
                new=AsyncMock(),
            ),
            patch.object(
                database_module,
                "_delete_where",
                new=delete_where,
            ),
            patch(
                "comet.discovery.torrent_repository."
                "TorrentReleaseRepository.delete_stale_public",
                new=AsyncMock(),
            ),
        ):
            await database_module._perform_startup_cleanup(10_000_000.0)

        media_cache_calls = [
            call
            for call in delete_where.await_args_list
            if call.args[0] == "media_metadata_cache"
        ]
        self.assertEqual(len(media_cache_calls), 1)
        predicate = media_cache_calls[0].args[1]
        self.assertIn("metadata_updated_at IS NULL", predicate)
        self.assertIn("aliases_updated_at IS NULL", predicate)
        self.assertIn("release_updated_at IS NULL", predicate)

    async def test_periodic_database_cleanup_exposes_unexpected_failures(self):
        for cleanup in (
            database_module.cleanup_expired_locks,
            database_module.cleanup_expired_kodi_setup_codes,
        ):
            with (
                self.subTest(cleanup=cleanup.__name__),
                patch.object(
                    database_module.database,
                    "execute",
                    new=AsyncMock(side_effect=RuntimeError("implementation failed")),
                ),
                self.assertRaisesRegex(RuntimeError, "implementation failed"),
            ):
                await cleanup()

    async def test_teardown_propagates_disconnect_failure(self):
        with (
            patch.object(
                database_module.database,
                "disconnect",
                new=AsyncMock(side_effect=RuntimeError("implementation failed")),
            ),
            self.assertRaisesRegex(RuntimeError, "implementation failed"),
        ):
            await database_module.teardown_database()
