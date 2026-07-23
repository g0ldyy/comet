import unittest
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock

from databases import Database

from comet.core.db_router import ReplicaAwareDatabase
from comet.core.schema_migrations import (
    MigrationContext,
    _add_column_if_missing,
    _column_exists,
    _drop_column_if_exists,
    _ensure_managed_table,
    _migration_debrid_account_cleanup_index,
    _migration_original_indexer_titles,
    _migration_tmdb_title_aliases,
    _rename_column_if_missing,
)
from comet.core.schema_specs import ManagedTableSpec


class SchemaMigrationMetadataCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_debrid_account_cleanup_migration_creates_partial_index(self):
        with TemporaryDirectory() as temp_dir:
            database = ReplicaAwareDatabase(
                Database(f"sqlite+aiosqlite:///{temp_dir}/migration.db")
            )
            await database.connect()
            try:
                context = MigrationContext(database, is_sqlite=True, is_postgres=False)

                await _migration_debrid_account_cleanup_index(context)

                row = await database.fetch_one(
                    """
                    SELECT sql
                    FROM sqlite_master
                    WHERE type = 'index'
                      AND name = 'idx_torrents_debrid_account_media_v1'
                    """
                )
                self.assertIn("ON torrents (media_id, info_hash)", row["sql"])
                self.assertIn("substr(tracker, 1, 14) = 'DebridAccount|'", row["sql"])
            finally:
                await database.disconnect()

    async def test_original_title_migration_invalidates_imdb_and_kitsu_aliases(self):
        with TemporaryDirectory() as temp_dir:
            database = ReplicaAwareDatabase(
                Database(f"sqlite+aiosqlite:///{temp_dir}/migration.db")
            )
            await database.connect()
            try:
                await database.execute(
                    """
                    CREATE TABLE media_metadata_cache (
                        media_id TEXT PRIMARY KEY,
                        title TEXT,
                        year INTEGER,
                        year_end INTEGER,
                        aliases_json TEXT,
                        metadata_updated_at REAL,
                        aliases_updated_at REAL,
                        release_date BIGINT,
                        release_updated_at REAL
                    )
                    """
                )
                await database.execute_many(
                    """
                    INSERT INTO media_metadata_cache (
                        media_id, aliases_json, aliases_updated_at
                    ) VALUES (
                        :media_id, :aliases_json, 123.0
                    )
                    """,
                    [
                        {"media_id": "imdb:tt123", "aliases_json": '{"ez":["Old"]}'},
                        {
                            "media_id": "kitsu:456",
                            "aliases_json": '{"lang:fr":["Film"]}',
                        },
                        {
                            "media_id": "tmdb:789",
                            "aliases_json": '{"lang:fr":["Film"]}',
                        },
                    ],
                )
                context = MigrationContext(database, is_sqlite=True, is_postgres=False)

                await _migration_original_indexer_titles(context)

                rows = await database.fetch_all(
                    """
                    SELECT media_id, aliases_updated_at
                    FROM media_metadata_cache
                    ORDER BY media_id
                    """
                )
                self.assertIsNone(rows[0]["aliases_updated_at"])
                self.assertIsNone(rows[1]["aliases_updated_at"])
                self.assertEqual(rows[2]["aliases_updated_at"], 123.0)
            finally:
                await database.disconnect()

    async def test_tmdb_alias_migration_invalidates_only_imdb_aliases(self):
        with TemporaryDirectory() as temp_dir:
            database = ReplicaAwareDatabase(
                Database(f"sqlite+aiosqlite:///{temp_dir}/migration.db")
            )
            await database.connect()
            try:
                await database.execute(
                    """
                    CREATE TABLE media_metadata_cache (
                        media_id TEXT PRIMARY KEY,
                        title TEXT,
                        year INTEGER,
                        year_end INTEGER,
                        aliases_json TEXT,
                        metadata_updated_at REAL,
                        aliases_updated_at REAL,
                        release_date BIGINT,
                        release_updated_at REAL
                    )
                    """
                )
                await database.execute_many(
                    """
                    INSERT INTO media_metadata_cache (
                        media_id,
                        aliases_json,
                        aliases_updated_at
                    ) VALUES (
                        :media_id,
                        :aliases_json,
                        :aliases_updated_at
                    )
                    """,
                    [
                        {
                            "media_id": "imdb:tt123",
                            "aliases_json": '{"us":["Old"]}',
                            "aliases_updated_at": 123.0,
                        },
                        {
                            "media_id": "kitsu:123",
                            "aliases_json": '{"ez":["Anime"]}',
                            "aliases_updated_at": 123.0,
                        },
                    ],
                )
                context = MigrationContext(
                    database,
                    is_sqlite=True,
                    is_postgres=False,
                )

                await _migration_tmdb_title_aliases(context)

                rows = await database.fetch_all(
                    """
                    SELECT media_id, aliases_json, aliases_updated_at
                    FROM media_metadata_cache
                    ORDER BY media_id
                    """
                )
                self.assertEqual(rows[0]["media_id"], "imdb:tt123")
                self.assertEqual(rows[0]["aliases_json"], '{"us":["Old"]}')
                self.assertIsNone(rows[0]["aliases_updated_at"])
                self.assertEqual(rows[1]["media_id"], "kitsu:123")
                self.assertEqual(rows[1]["aliases_updated_at"], 123.0)
            finally:
                await database.disconnect()

    async def test_column_metadata_is_loaded_once_per_table(self):
        database = AsyncMock()
        database.fetch_one.return_value = {"exists": 1}
        database.fetch_all.return_value = [{"name": "id"}, {"name": "value"}]
        context = MigrationContext(database, is_sqlite=True, is_postgres=False)

        self.assertTrue(await _column_exists(context, "items", "id"))
        self.assertTrue(await _column_exists(context, "items", "value"))
        self.assertFalse(await _column_exists(context, "items", "missing"))

        database.fetch_one.assert_awaited_once()
        database.fetch_all.assert_awaited_once_with(
            "PRAGMA table_info(items)", force_primary=True
        )

    async def test_new_managed_table_checks_existence_once(self):
        database = AsyncMock()
        database.fetch_one.return_value = None
        context = MigrationContext(database, is_sqlite=True, is_postgres=False)
        spec = ManagedTableSpec(
            table_name="items",
            create_sql="CREATE TABLE {table_name} (id INTEGER PRIMARY KEY)",
        )

        existed = await _ensure_managed_table(context, spec)

        self.assertFalse(existed)
        database.fetch_one.assert_awaited_once()
        database.execute.assert_awaited_once_with(
            "CREATE TABLE items (id INTEGER PRIMARY KEY)"
        )

    async def test_column_cache_tracks_schema_mutations(self):
        database = AsyncMock()
        context = MigrationContext(database, is_sqlite=True, is_postgres=False)
        context.table_exists_cache["items"] = True
        context.table_columns_cache["items"] = {"old_name"}

        renamed = await _rename_column_if_missing(
            context, "items", "old_name", "new_name"
        )
        await _add_column_if_missing(context, "items", "extra", "extra TEXT")
        dropped = await _drop_column_if_exists(context, "items", "new_name")

        self.assertTrue(renamed)
        self.assertTrue(dropped)
        self.assertEqual(context.table_columns_cache["items"], {"extra"})
        database.fetch_one.assert_not_awaited()
        database.fetch_all.assert_not_awaited()
        self.assertEqual(database.execute.await_count, 3)
