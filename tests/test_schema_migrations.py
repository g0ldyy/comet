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
    _migration_candidate_identity_scope,
    _migration_debrid_account_cleanup_index,
    _migration_media_demand_scrape_coverage,
    _migration_original_indexer_titles,
    _migration_tmdb_title_aliases,
    _rename_column_if_missing,
    _upgrade_download_link_cache,
)
from comet.core.schema_specs import ManagedTableSpec


class SchemaMigrationMetadataCacheTests(unittest.IsolatedAsyncioTestCase):
    def test_migration_context_requires_one_backend(self):
        with self.assertRaisesRegex(ValueError, "exactly one database backend"):
            MigrationContext(AsyncMock(), is_sqlite=False, is_postgres=False)
        with self.assertRaisesRegex(ValueError, "exactly one database backend"):
            MigrationContext(AsyncMock(), is_sqlite=True, is_postgres=True)

    async def test_candidate_identity_migration_scopes_exact_identity_by_family(self):
        with TemporaryDirectory() as temp_dir:
            database = ReplicaAwareDatabase(
                Database(f"sqlite+aiosqlite:///{temp_dir}/migration.db")
            )
            await database.connect()
            try:
                await database.execute(
                    """
                    CREATE TABLE release_candidates (
                        candidate_id VARCHAR(36) PRIMARY KEY
                    )
                    """
                )
                await database.execute(
                    """
                    CREATE TABLE candidate_identities (
                        candidate_id VARCHAR(36) NOT NULL REFERENCES
                            release_candidates(candidate_id) ON DELETE CASCADE,
                        visibility_partition CHAR(64) NOT NULL,
                        transport VARCHAR(16) NOT NULL,
                        identity_scheme VARCHAR(16) NOT NULL,
                        identity_value VARCHAR(256) NOT NULL,
                        created_at_ms BIGINT NOT NULL,
                        updated_at_ms BIGINT NOT NULL,
                        PRIMARY KEY (
                            candidate_id, identity_scheme, identity_value
                        ),
                        UNIQUE (
                            visibility_partition, transport,
                            identity_scheme, identity_value
                        )
                    )
                    """
                )
                candidate_ids = (
                    "11111111-1111-4111-8111-111111111111",
                    "22222222-2222-4222-8222-222222222222",
                )
                await database.execute_many(
                    "INSERT INTO release_candidates VALUES (:candidate_id)",
                    [{"candidate_id": value} for value in candidate_ids],
                )
                identity = {
                    "candidate_id": candidate_ids[0],
                    "visibility_partition": "a" * 64,
                    "transport": "bittorrent",
                    "identity_scheme": "btih",
                    "identity_value": "b" * 40,
                    "created_at_ms": 1,
                    "updated_at_ms": 1,
                }
                insert = """
                    INSERT INTO candidate_identities (
                        candidate_id, visibility_partition, transport,
                        identity_scheme, identity_value,
                        created_at_ms, updated_at_ms
                    ) VALUES (
                        :candidate_id, :visibility_partition, :transport,
                        :identity_scheme, :identity_value,
                        :created_at_ms, :updated_at_ms
                    )
                """
                await database.execute(insert, identity)

                await _migration_candidate_identity_scope(
                    MigrationContext(database, is_sqlite=True, is_postgres=False)
                )
                await database.execute(
                    insert,
                    {**identity, "candidate_id": candidate_ids[1]},
                )

                rows = await database.fetch_all(
                    "SELECT candidate_id FROM candidate_identities"
                )
                self.assertEqual(
                    {row["candidate_id"] for row in rows},
                    set(candidate_ids),
                )
            finally:
                await database.disconnect()

    async def test_download_link_cache_scope_separates_file_and_client(self):
        with TemporaryDirectory() as temp_dir:
            database = ReplicaAwareDatabase(
                Database(f"sqlite+aiosqlite:///{temp_dir}/migration.db")
            )
            await database.connect()
            try:
                await database.execute(
                    """
                    CREATE TABLE download_links_cache (
                        debrid_service TEXT NOT NULL,
                        account_key_hash TEXT NOT NULL,
                        info_hash TEXT NOT NULL,
                        season INTEGER,
                        episode INTEGER,
                        season_norm INTEGER NOT NULL,
                        episode_norm INTEGER NOT NULL,
                        download_url TEXT NOT NULL,
                        updated_at REAL NOT NULL
                    )
                    """
                )
                await database.execute(
                    """
                    CREATE UNIQUE INDEX unq_download_links_scope_v3
                    ON download_links_cache (
                        debrid_service, account_key_hash, info_hash,
                        season_norm, episode_norm
                    )
                    """
                )
                await database.execute(
                    """
                    INSERT INTO download_links_cache VALUES (
                        'realdebrid', 'account', 'hash', NULL, NULL,
                        -1, -1, 'https://download.example/video', 1
                    )
                    """
                )
                context = MigrationContext(database, is_sqlite=True, is_postgres=False)

                await _upgrade_download_link_cache(context)

                row = await database.fetch_one(
                    """
                    SELECT sql
                    FROM sqlite_master
                    WHERE type = 'index'
                      AND name = 'unq_download_links_scope_v4'
                    """
                )
                self.assertIn("selection_key", row["sql"])
                self.assertIn("client_scope", row["sql"])
                preserved = await database.fetch_one(
                    "SELECT selection_key, client_scope FROM download_links_cache"
                )
                self.assertEqual(
                    dict(preserved), {"selection_key": "", "client_scope": ""}
                )
            finally:
                await database.disconnect()

    async def test_scope_coverage_migration_preserves_existing_demand(self):
        with TemporaryDirectory() as temp_dir:
            database = ReplicaAwareDatabase(
                Database(f"sqlite+aiosqlite:///{temp_dir}/migration.db")
            )
            await database.connect()
            try:
                await database.execute(
                    """
                    CREATE TABLE media_demand (
                        media_id TEXT PRIMARY KEY,
                        first_seen_at REAL NOT NULL,
                        last_seen_at REAL NOT NULL
                    )
                    """
                )
                await database.execute(
                    """
                    INSERT INTO media_demand (
                        media_id,
                        first_seen_at,
                        last_seen_at
                    ) VALUES (
                        'tt123:2',
                        1,
                        2
                    )
                    """
                )
                context = MigrationContext(database, is_sqlite=True, is_postgres=False)

                await _migration_media_demand_scrape_coverage(context)

                row = await database.fetch_one(
                    """
                    SELECT media_id, first_seen_at, last_seen_at, last_scraped_at
                    FROM media_demand
                    """
                )
                self.assertEqual(row["media_id"], "tt123:2")
                self.assertEqual(row["first_seen_at"], 1)
                self.assertEqual(row["last_seen_at"], 2)
                self.assertIsNone(row["last_scraped_at"])
            finally:
                await database.disconnect()

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
