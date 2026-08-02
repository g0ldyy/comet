import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

import orjson
from databases import Database
from RTN import parse

from comet.api.endpoints import torznab
from comet.core import schema_migrations
from comet.core.db_router import ReplicaAwareDatabase
from comet.core.schema_migrations import (
    USENET_RELEASE_SCHEMA_MIGRATION,
    MigrationContext,
    _ensure_managed_table,
    _ensure_usenet_schema,
    _migration_remove_legacy_torrent_storage,
    run_schema_migrations,
)
from comet.core.schema_specs import (
    DEBRID_AVAILABILITY_TABLE_SPEC,
    MEDIA_DEMAND_TABLE_SPEC,
    METRICS_CACHE_TABLE_SPEC,
    SCRAPE_LOCKS_TABLE_SPEC,
    TORRENTS_TABLE_SPEC,
)
from comet.discovery.repository import (
    LEGACY_TORRENT_DISCOVERY_CONFIGURATION_ID,
)
from comet.discovery.torrent_backfill import backfill_legacy_torrents
from comet.discovery.torrent_repository import TorrentReleaseRepository
from comet.services import orchestration, torrent_manager
from comet.services.orchestration import TorrentResultAccumulator
from comet.utils.parsing import MediaScope

DEVELOPMENT_MIGRATIONS = (
    "2026030901_foundation",
    "2026030902_backfill_canonical_tables",
    "2026030903_integrity_rollout",
    "2026030904_indexes",
    "2026030905_cleanup_legacy_storage",
    "2026031201_remove_dead_kodi_columns",
    "2026031601_series_episode_index",
    "2026031602_series_episode_index_refresh",
    "2026072201_tmdb_title_aliases",
    "2026072202_tmdb_localized_titles",
    "2026072203_original_indexer_titles",
    "2026072204_debrid_account_cleanup_index",
    "2026072301_media_demand_scrape_coverage",
    "2026072701_imdb_title_lookup",
)


class LegacyTorrentBackfillTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary_directory = TemporaryDirectory()
        self.database = ReplicaAwareDatabase(
            Database(f"sqlite+aiosqlite:///{self.temporary_directory.name}/backfill.db")
        )
        await self.database.connect()
        self.context = MigrationContext(
            self.database,
            is_sqlite=True,
            is_postgres=False,
        )
        await _ensure_managed_table(self.context, TORRENTS_TABLE_SPEC)
        await self.database.execute(
            """
            CREATE UNIQUE INDEX unq_torrents_scope_v3
            ON torrents (media_id, info_hash, season_norm, episode_norm)
            """
        )
        await _ensure_usenet_schema(self.context)
        for spec in (
            METRICS_CACHE_TABLE_SPEC,
            MEDIA_DEMAND_TABLE_SPEC,
            SCRAPE_LOCKS_TABLE_SPEC,
            DEBRID_AVAILABILITY_TABLE_SPEC,
        ):
            await _ensure_managed_table(self.context, spec)

    async def asyncTearDown(self):
        await self.database.disconnect()
        self.temporary_directory.cleanup()

    async def _insert(
        self,
        *,
        media_id: str,
        info_hash: str,
        title: str,
        season: int | None = None,
        episode: int | None = None,
        tracker: str | None = "Indexer",
        file_index: int = 2,
    ):
        parsed = parse(title)
        await self.database.execute(
            """
            INSERT INTO torrents (
                media_id, info_hash, season, episode,
                season_norm, episode_norm, file_index, title,
                seeders, size, tracker, sources_json,
                parsed_json, updated_at
            ) VALUES (
                :media_id, :info_hash, :season, :episode,
                :season_norm, :episode_norm, :file_index, :title,
                7, 1000000000, :tracker, '[]',
                :parsed_json, 10
            )
            """,
            {
                "media_id": media_id,
                "info_hash": info_hash,
                "season": season,
                "episode": episode,
                "season_norm": season if season is not None else -1,
                "episode_norm": episode if episode is not None else -1,
                "title": title,
                "tracker": tracker,
                "file_index": file_index,
                "parsed_json": orjson.dumps(parsed.model_dump()).decode(),
            },
        )

    async def test_backfills_only_public_rows_and_is_idempotent(self):
        await self._insert(
            media_id="tt1234567",
            info_hash="a" * 40,
            title="Movie.2026.1080p.WEB-DL-GROUP",
        )
        await self._insert(
            media_id="tt2345678",
            info_hash="b" * 40,
            title="Show.S01E02.2026.1080p.WEB-DL-GROUP",
            season=1,
            episode=2,
            tracker=None,
            file_index=2,
        )
        await self._insert(
            media_id="tt2345678",
            info_hash="b" * 40,
            title="Show.S01E03.2026.1080p.WEB-DL-GROUP",
            season=1,
            episode=3,
            tracker=None,
            file_index=3,
        )
        await self._insert(
            media_id="tt3456789",
            info_hash="c" * 40,
            title="Private.2026.1080p.WEB-DL-GROUP",
            tracker="DebridAccount|realdebrid|digest",
        )

        await backfill_legacy_torrents(self.database)
        await backfill_legacy_torrents(self.database)

        candidates = await self.database.fetch_all(
            """
            SELECT media_id, scope, season_norm, episode_norm, transport,
                   release_key, attributes_json
            FROM release_candidates
            ORDER BY media_id
            """,
            force_primary=True,
        )
        locators = await self.database.fetch_all(
            """
            SELECT locator_kind, owner_configuration_partition,
                   account_partition
            FROM release_locators
            WHERE discovery_configuration_id = :configuration_id
            ORDER BY locator_id
            """,
            {"configuration_id": (LEGACY_TORRENT_DISCOVERY_CONFIGURATION_ID)},
            force_primary=True,
        )
        identities = await self.database.fetch_all(
            """
            SELECT identity_scheme, identity_value
            FROM candidate_identities
            ORDER BY identity_value
            """,
            force_primary=True,
        )

        self.assertEqual(
            [
                (
                    row["media_id"],
                    row["scope"],
                    row["season_norm"],
                    row["episode_norm"],
                    row["transport"],
                )
                for row in candidates
            ],
            [
                ("tt1234567", "movie", -1, -1, "bittorrent"),
                ("tt2345678", "series_pack", -1, -1, "bittorrent"),
            ],
        )
        self.assertEqual(
            [row["release_key"] for row in candidates],
            ["btih:" + "a" * 40, "btih:" + "b" * 40],
        )
        self.assertEqual(len(locators), 3)
        self.assertTrue(all(row["locator_kind"] == "torrent" for row in locators))
        self.assertTrue(
            all(
                row["owner_configuration_partition"] == "0" * 64
                and row["account_partition"] is None
                for row in locators
            )
        )
        self.assertEqual(
            [
                (
                    row["identity_scheme"],
                    row["identity_value"],
                )
                for row in identities
            ],
            [
                ("btih", "a" * 40),
                ("btih", "b" * 40),
            ],
        )
        episode_two = await TorrentReleaseRepository(self.database).load_cache_rows(
            "tt2345678",
            MediaScope.EPISODE,
            1,
            2,
        )
        episode_three = await TorrentReleaseRepository(self.database).load_cache_rows(
            "tt2345678",
            MediaScope.EPISODE,
            1,
            3,
        )
        self.assertEqual(
            [(row["episode"], row["file_index"]) for row in episode_two],
            [(2, 2)],
        )
        self.assertEqual(
            [(row["episode"], row["file_index"]) for row in episode_three],
            [(3, 3)],
        )

    async def test_invalid_cache_row_is_discarded_without_blocking_valid_data(self):
        await self._insert(
            media_id="tt1234567",
            info_hash="a" * 40,
            title="Movie.2026.1080p.WEB-DL-GROUP",
        )
        await self._insert(
            media_id="tt2345678",
            info_hash="not-an-info-hash",
            title="Broken",
        )

        result = await backfill_legacy_torrents(self.database)

        candidates = await self.database.fetch_all(
            "SELECT media_id, release_key FROM release_candidates",
            force_primary=True,
        )
        self.assertEqual(
            [tuple(row.values()) for row in candidates],
            [("tt1234567", "btih:" + "a" * 40)],
        )
        self.assertEqual(result.eligible_rows, 1)
        self.assertEqual(result.persisted_rows, 1)
        self.assertEqual(result.discarded_rows, 1)

    async def test_update_writer_uses_only_generic_public_storage(self):
        public = torrent_manager._construct_torrent_update(
            media_id="tt1234567",
            info_hash="a" * 40,
            season=None,
            episode=None,
            file_index=2,
            title="Movie.2026.1080p.WEB-DL-GROUP",
            seeders=7,
            size=1_000_000_000,
            tracker="Indexer",
            sources=["udp://tracker.example"],
            parsed=parse("Movie.2026.1080p.WEB-DL-GROUP").model_dump(),
            from_cometnet=False,
        )
        account = torrent_manager._construct_torrent_update(
            media_id="tt1234567",
            info_hash="b" * 40,
            season=None,
            episode=None,
            file_index=3,
            title="Private.2026.1080p.WEB-DL-GROUP",
            seeders=0,
            size=2_000_000_000,
            tracker="DebridAccount|realdebrid|digest",
            sources=["https://credential.invalid/announce"],
            parsed=parse("Private.2026.1080p.WEB-DL-GROUP").model_dump(),
            from_cometnet=False,
        )

        with patch.object(torrent_manager, "database", self.database):
            await torrent_manager._execute_batched_upsert(
                [public],
                updated_at=20,
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "account-derived torrent cannot use the public repository",
            ):
                await torrent_manager._execute_batched_upsert(
                    [account],
                    updated_at=20,
                )

        generic = await TorrentReleaseRepository(self.database).load_cache_rows(
            "tt1234567",
            MediaScope.MOVIE,
            None,
            None,
        )
        legacy = await self.database.fetch_all(
            """
            SELECT info_hash, tracker, sources_json
            FROM torrents
            ORDER BY info_hash
            """,
            force_primary=True,
        )

        self.assertEqual(len(generic), 1)
        self.assertEqual(generic[0]["info_hash"], "a" * 40)
        self.assertEqual(generic[0]["file_index"], 2)
        self.assertEqual(generic[0]["seeders"], 7)
        self.assertEqual(generic[0]["sources_json"], '["udp://tracker.example"]')
        self.assertEqual(legacy, [])
        repository = TorrentReleaseRepository(self.database)
        self.assertEqual(
            await repository.existing_hashes(("a" * 40, "b" * 40)),
            {"a" * 40},
        )
        self.assertEqual(
            await repository.existing_media_keys(
                "tt1234567",
                ("a" * 40, "b" * 40),
            ),
            {("a" * 40, None, None)},
        )
        self.assertEqual(
            await repository.find_context("a" * 40),
            {
                "media_id": "tt1234567",
                "sources": ["udp://tracker.example"],
            },
        )
        with patch.object(torrent_manager, "database", self.database):
            self.assertEqual(
                await torrent_manager.check_torrents_exist(["a" * 40, "b" * 40]),
                {"a" * 40},
            )
        with (
            patch.object(
                torrent_manager.TorrentReleaseRepository,
                "existing_hashes",
                side_effect=RuntimeError("database failure"),
            ),
            self.assertRaisesRegex(RuntimeError, "database failure"),
        ):
            await torrent_manager.check_torrents_exist(["a" * 40])
        manager = TorrentResultAccumulator(
            media_type="movie",
            media_full_id="tt1234567",
            media_only_id="tt1234567",
            title="Movie",
            year=2026,
            year_end=None,
            season=None,
            episode=None,
            aliases={},
            remove_adult_content=False,
        )
        with patch.object(orchestration, "database", self.database):
            cached_rows = await manager._fetch_cached_rows("tt1234567")
        self.assertEqual(
            {row["info_hash"] for row in cached_rows},
            {"a" * 40},
        )
        with patch.object(torznab, "database", self.database):
            candidate_row = await torznab._candidate_torrent_row("tt1234567")
        self.assertEqual(candidate_row["info_hash"], "a" * 40)
        stored = await self.database.fetch_one(
            """
            SELECT candidate_id, attributes_json
            FROM release_candidates
            LIMIT 1
            """,
            force_primary=True,
        )
        attributes = orjson.loads(stored["attributes_json"])
        attributes["transport_stats"]["tracker_sources"] = [None]
        await self.database.execute(
            """
            UPDATE release_candidates
            SET attributes_json = :attributes_json
            WHERE candidate_id = :candidate_id
            """,
            {
                "candidate_id": stored["candidate_id"],
                "attributes_json": orjson.dumps(attributes).decode(),
            },
            force_primary=True,
        )
        with self.assertRaisesRegex(RuntimeError, "tracker sources"):
            await repository.find_context("a" * 40)

    async def test_final_migration_backfills_public_and_removes_legacy_table(self):
        await self._insert(
            media_id="tt1234567",
            info_hash="a" * 40,
            title="Movie.2026.1080p.WEB-DL-GROUP",
        )
        await self._insert(
            media_id="tt1234567",
            info_hash="b" * 40,
            title="Private.2026.1080p.WEB-DL-GROUP",
            tracker="DebridAccount|realdebrid|digest",
        )

        await _migration_remove_legacy_torrent_storage(self.context)
        await _migration_remove_legacy_torrent_storage(self.context)

        legacy_table = await self.database.fetch_one(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'torrents'
            """,
            force_primary=True,
        )
        repository = TorrentReleaseRepository(self.database)
        self.assertIsNone(legacy_table)
        self.assertEqual(
            await repository.existing_hashes(("a" * 40, "b" * 40)),
            {"a" * 40},
        )

    async def test_generic_torrent_cleanup_is_bounded_to_stale_public_rows(self):
        await _migration_remove_legacy_torrent_storage(self.context)
        old = torrent_manager._construct_torrent_update(
            media_id="tt1234567",
            info_hash="a" * 40,
            season=None,
            episode=None,
            file_index=1,
            title="Old.2026.1080p.WEB-DL-GROUP",
            seeders=1,
            size=1_000_000_000,
            tracker="Indexer",
            sources=[],
            parsed=parse("Old.2026.1080p.WEB-DL-GROUP").model_dump(),
            from_cometnet=False,
        )
        current = torrent_manager._construct_torrent_update(
            media_id="tt1234567",
            info_hash="b" * 40,
            season=None,
            episode=None,
            file_index=2,
            title="Current.2026.1080p.WEB-DL-GROUP",
            seeders=2,
            size=2_000_000_000,
            tracker="Indexer",
            sources=[],
            parsed=parse("Current.2026.1080p.WEB-DL-GROUP").model_dump(),
            from_cometnet=False,
        )
        with patch.object(torrent_manager, "database", self.database):
            await torrent_manager._execute_batched_upsert([old], updated_at=10)
            await torrent_manager._execute_batched_upsert([current], updated_at=20)

        repository = TorrentReleaseRepository(self.database)
        self.assertEqual(
            await repository.delete_stale_public(before_ms=15_000),
            1,
        )
        self.assertEqual(
            await repository.existing_hashes(("a" * 40, "b" * 40)),
            {"b" * 40},
        )


class FreshGenericSchemaTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    async def _schema_signature(database):
        rows = await database.fetch_all(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE type IN ('table', 'index')
              AND name NOT LIKE 'sqlite_%%'
            ORDER BY type, name
            """,
            force_primary=True,
        )
        return [tuple(row.values()) for row in rows]

    async def test_full_migration_chain_omits_removed_storage(self):
        with TemporaryDirectory() as temporary_directory:
            database = ReplicaAwareDatabase(
                Database(f"sqlite+aiosqlite:///{temporary_directory}/fresh-schema.db")
            )
            await database.connect()
            try:
                await run_schema_migrations(
                    database,
                    is_sqlite=True,
                    is_postgres=False,
                )
                removed_tables = await database.fetch_all(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'table'
                      AND name IN (
                          'torrents',
                          'candidate_evidence',
                          'content_verdicts',
                          'candidate_content_aliases'
                      )
                    ORDER BY name
                    """,
                    force_primary=True,
                )
                locator_columns = await database.fetch_all(
                    "PRAGMA table_info(release_locators)",
                    force_primary=True,
                )
                latest = await database.fetch_one(
                    """
                    SELECT version
                    FROM schema_migrations
                    ORDER BY version DESC
                    LIMIT 1
                    """,
                    force_primary=True,
                )
                self.assertEqual(removed_tables, [])
                self.assertFalse(
                    {"provider_configuration_id", "last_rendered_at_ms"}
                    & {row["name"] for row in locator_columns}
                )
                self.assertEqual(
                    latest["version"],
                    schema_migrations.MIGRATIONS[-1][0],
                )
            finally:
                await database.disconnect()

    async def test_latest_development_upgrade_preserves_data_and_is_idempotent(self):
        usenet_migration_index = next(
            index
            for index, (version, _migration) in enumerate(schema_migrations.MIGRATIONS)
            if version == USENET_RELEASE_SCHEMA_MIGRATION
        )
        self.assertEqual(
            schema_migrations.MIGRATIONS[usenet_migration_index],
            (
                USENET_RELEASE_SCHEMA_MIGRATION,
                schema_migrations._migration_usenet_release_schema,
            ),
        )
        self.assertFalse(
            any(
                "usenet" in version
                for version, _migration in schema_migrations.MIGRATIONS[
                    :usenet_migration_index
                ]
            )
        )

        with TemporaryDirectory() as temporary_directory:
            upgraded = ReplicaAwareDatabase(
                Database(f"sqlite+aiosqlite:///{temporary_directory}/upgraded.db")
            )
            await upgraded.connect()
            try:
                baseline = schema_migrations.MIGRATIONS[:usenet_migration_index]
                self.assertEqual(
                    tuple(version for version, _migration in baseline),
                    DEVELOPMENT_MIGRATIONS,
                    "the public migration history must remain stable",
                )
                with patch.object(schema_migrations, "MIGRATIONS", baseline):
                    await run_schema_migrations(
                        upgraded,
                        is_sqlite=True,
                        is_postgres=False,
                    )
                await upgraded.execute("DROP TABLE download_links_cache")
                await upgraded.execute(
                    """
                    CREATE TABLE download_links_cache (
                        debrid_service TEXT NOT NULL,
                        account_key_hash TEXT NOT NULL,
                        info_hash TEXT NOT NULL,
                        season INTEGER,
                        episode INTEGER,
                        season_norm INTEGER NOT NULL DEFAULT -1,
                        episode_norm INTEGER NOT NULL DEFAULT -1,
                        download_url TEXT NOT NULL,
                        updated_at DOUBLE PRECISION NOT NULL,
                        CHECK (
                            (season IS NULL AND season_norm = -1)
                            OR season = season_norm
                        ),
                        CHECK (
                            (episode IS NULL AND episode_norm = -1)
                            OR episode = episode_norm
                        )
                    )
                    """
                )
                await upgraded.execute(
                    """
                    CREATE UNIQUE INDEX unq_download_links_scope_v3
                    ON download_links_cache (
                        debrid_service, account_key_hash, info_hash,
                        season_norm, episode_norm
                    )
                    """
                )
                await upgraded.execute(
                    """
                    INSERT INTO download_links_cache VALUES (
                        'realdebrid', 'account', 'hash', NULL, NULL,
                        -1, -1, 'https://download.example/video', 10
                    )
                    """
                )
                await upgraded.execute(
                    """
                    INSERT INTO active_connections (
                        id, ip, content, started_at
                    ) VALUES ('connection', '127.0.0.1', 'video', 10)
                    """
                )
                parsed_json = orjson.dumps(
                    parse("Movie.2026.1080p.WEB-DL-GROUP").model_dump()
                ).decode()
                await upgraded.execute_many(
                    """
                    INSERT INTO torrents (
                        media_id, info_hash, season, episode,
                        season_norm, episode_norm, file_index, title,
                        seeders, size, tracker, sources_json,
                        parsed_json, updated_at
                    ) VALUES (
                        :media_id, :info_hash, NULL, NULL, -1, -1, 0, :title,
                        5, 1000000000, :tracker, '[]', :parsed_json, 10
                    )
                    """,
                    [
                        {
                            "media_id": "tt1234567",
                            "info_hash": "a" * 40,
                            "title": "Movie.2026.1080p.WEB-DL-GROUP",
                            "tracker": "Indexer",
                            "parsed_json": parsed_json,
                        },
                        {
                            "media_id": "tt1234567",
                            "info_hash": "b" * 40,
                            "title": "Private.2026.1080p.WEB-DL-GROUP",
                            "tracker": "DebridAccount|realdebrid|digest",
                            "parsed_json": parsed_json,
                        },
                        {
                            "media_id": "tt7654321",
                            "info_hash": "invalid-hash",
                            "title": "Irrecoverable cache row",
                            "tracker": "Indexer",
                            "parsed_json": "{}",
                        },
                    ],
                    force_primary=True,
                )
                await upgraded.execute(
                    """
                    INSERT INTO imdb_title_lookup (
                        query_key, imdb_id, media_type, year, updated_at
                    ) VALUES ('movie:2026', 'tt1234567', 'movie', 2026, 10)
                    """,
                    force_primary=True,
                )

                await run_schema_migrations(
                    upgraded,
                    is_sqlite=True,
                    is_postgres=False,
                )
                signature = await self._schema_signature(upgraded)
                candidate_count = await upgraded.fetch_val(
                    "SELECT COUNT(*) FROM release_candidates",
                    force_primary=True,
                )
                await run_schema_migrations(
                    upgraded,
                    is_sqlite=True,
                    is_postgres=False,
                )

                self.assertEqual(signature, await self._schema_signature(upgraded))
                self.assertEqual(candidate_count, 1)
                self.assertEqual(
                    await upgraded.fetch_val(
                        "SELECT COUNT(*) FROM release_candidates",
                        force_primary=True,
                    ),
                    1,
                )
                self.assertEqual(
                    await upgraded.fetch_val(
                        """
                        SELECT imdb_id
                        FROM imdb_title_lookup
                        WHERE query_key = 'movie:2026'
                        """,
                        force_primary=True,
                    ),
                    "tt1234567",
                )
                cached_link = await upgraded.fetch_one(
                    """
                    SELECT download_url, selection_key, client_scope
                    FROM download_links_cache
                    """,
                    force_primary=True,
                )
                self.assertEqual(
                    dict(cached_link),
                    {
                        "download_url": "https://download.example/video",
                        "selection_key": "",
                        "client_scope": "",
                    },
                )
                connection = await upgraded.fetch_one(
                    """
                    SELECT id, service, instance_id, process_id,
                           bytes_transferred, cancel_requested
                    FROM active_connections
                    """,
                    force_primary=True,
                )
                self.assertEqual(
                    dict(connection),
                    {
                        "id": "connection",
                        "service": "unknown",
                        "instance_id": "",
                        "process_id": 0,
                        "bytes_transferred": 0,
                        "cancel_requested": 0,
                    },
                )
                indexes = {
                    row["name"]
                    for row in await upgraded.fetch_all(
                        """
                        SELECT name
                        FROM sqlite_master
                        WHERE type = 'index'
                          AND tbl_name = 'download_links_cache'
                        """,
                        force_primary=True,
                    )
                }
                self.assertIn("unq_download_links_scope_v4", indexes)
                self.assertNotIn("unq_download_links_scope_v3", indexes)
                versions = await upgraded.fetch_all(
                    "SELECT version FROM schema_migrations ORDER BY version",
                    force_primary=True,
                )
                self.assertEqual(
                    [row["version"] for row in versions],
                    sorted(
                        version for version, _migration in schema_migrations.MIGRATIONS
                    ),
                )
            finally:
                await upgraded.disconnect()
