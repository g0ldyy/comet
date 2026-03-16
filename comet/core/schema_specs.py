from dataclasses import dataclass

NULL_SCOPE_SENTINEL = -1
LEGACY_STORAGE_CLEANUP_MIGRATION = "2026030905_cleanup_legacy_storage"


@dataclass(frozen=True, slots=True)
class LegacyColumnMigration:
    column_name: str
    column_sql: str
    legacy_name: str | None = None
    backfill_expression: str | None = None


@dataclass(frozen=True, slots=True)
class ManagedTableSpec:
    table_name: str
    create_sql: str
    legacy_columns: tuple[LegacyColumnMigration, ...] = ()
    index_sql: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class UniqueIndexSpec:
    table_name: str
    index_name: str
    index_sql: str
    partition_columns: tuple[str, ...]
    order_by_sql: str


LEGACY_INDEX_NAMES = [
    "torrents_series_both_idx",
    "torrents_season_only_idx",
    "torrents_episode_only_idx",
    "torrents_no_season_episode_idx",
    "idx_torrents_media_cache_lookup",
    "idx_torrents_tracker_analytics",
    "idx_torrents_size_filter",
    "idx_torrents_seeders_desc",
    "idx_torrents_quality_cache",
    "idx_torrents_media_season_episode",
    "torrents_cache_lookup_idx",
    "idx_torrents_timestamp",
    "torrents_seeders_idx",
    "unq_torrents_series",
    "unq_torrents_season",
    "unq_torrents_episode",
    "unq_torrents_movie",
    "idx_torrents_lookup",
    "idx_torrents_info_hash",
    "debrid_series_both_idx",
    "debrid_season_only_idx",
    "debrid_episode_only_idx",
    "debrid_no_season_episode_idx",
    "idx_debrid_service_hash_cache",
    "idx_debrid_season_episode_filter",
    "idx_debrid_service_timestamp",
    "idx_debrid_title_filter",
    "idx_debrid_comprehensive",
    "idx_debrid_info_hash_season_episode",
    "idx_debrid_timestamp",
    "unq_debrid_series",
    "unq_debrid_season",
    "unq_debrid_episode",
    "unq_debrid_movie",
    "idx_debrid_lookup",
    "idx_debrid_info_hash",
    "idx_debrid_hash_season_episode",
    "download_links_series_both_idx",
    "download_links_season_only_idx",
    "download_links_episode_only_idx",
    "download_links_no_season_episode_idx",
    "download_links_series_both_v2_idx",
    "download_links_season_only_v2_idx",
    "download_links_episode_only_v2_idx",
    "download_links_no_season_episode_v2_idx",
    "idx_download_links_playback",
    "idx_download_links_playback_v2",
    "idx_download_links_cleanup",
    "idx_first_searches_cleanup",
    "idx_metadata_title_search",
    "idx_metadata_cache_lookup",
    "idx_digital_release_timestamp",
    "idx_anime_ids_entry_id",
    "idx_scrape_locks_expires_at",
    "idx_scrape_locks_lock_key",
    "idx_scrape_locks_instance",
    "idx_debrid_account_lookup",
    "idx_debrid_account_cleanup",
    "idx_connections_timestamp_desc",
    "idx_connections_ip_filter",
    "idx_connections_content_monitoring",
    "idx_kodi_setup_codes_expires",
    "idx_bg_items_media_retry_priority",
    "idx_bg_items_status",
    "idx_bg_items_plan_window",
    "idx_bg_episodes_series_retry",
    "idx_bg_episodes_plan_window",
    "idx_bg_runs_started",
    "idx_bg_runs_status",
    "idx_anime_ids_entry_provider",
    "idx_dmm_parsed_title",
    "idx_dmm_parsed_year",
    "idx_series_episode_air_date_lookup_v1",
]


DB_MAINTENANCE_TABLE_SPEC = ManagedTableSpec(
    table_name="db_maintenance",
    create_sql="""
        CREATE TABLE {table_name} (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_startup_cleanup_at REAL
        )
    """,
    legacy_columns=(
        LegacyColumnMigration(
            column_name="last_startup_cleanup_at",
            column_sql="last_startup_cleanup_at REAL",
            legacy_name="last_startup_cleanup",
            backfill_expression="COALESCE(last_startup_cleanup_at, last_startup_cleanup)",
        ),
    ),
)

SCRAPE_LOCKS_TABLE_SPEC = ManagedTableSpec(
    table_name="scrape_locks",
    create_sql="""
        CREATE TABLE {table_name} (
            lock_key TEXT PRIMARY KEY,
            instance_id TEXT NOT NULL,
            updated_at REAL NOT NULL,
            expires_at REAL NOT NULL
        )
    """,
    legacy_columns=(
        LegacyColumnMigration(
            column_name="updated_at",
            column_sql="updated_at REAL",
            legacy_name="timestamp",
            backfill_expression="COALESCE(updated_at, timestamp, expires_at)",
        ),
    ),
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_scrape_locks_expires_v2
            ON {table_name} (expires_at)
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_scrape_locks_instance_updated_v2
            ON {table_name} (instance_id, updated_at)
        """,
    ),
)

KODI_SETUP_CODES_TABLE_SPEC = ManagedTableSpec(
    table_name="kodi_setup_codes",
    create_sql="""
        CREATE TABLE {table_name} (
            code TEXT PRIMARY KEY,
            config_b64 TEXT,
            expires_at REAL NOT NULL
        )
    """,
    legacy_columns=(
        LegacyColumnMigration(
            column_name="config_b64",
            column_sql="config_b64 TEXT",
            legacy_name="b64config",
            backfill_expression="COALESCE(config_b64, b64config)",
        ),
    ),
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_kodi_setup_codes_expires_v2
            ON {table_name} (expires_at)
        """,
    ),
)

MEDIA_METADATA_CACHE_TABLE_SPEC = ManagedTableSpec(
    table_name="media_metadata_cache",
    create_sql="""
        CREATE TABLE {table_name} (
            media_id TEXT PRIMARY KEY,
            title TEXT,
            year INTEGER,
            year_end INTEGER,
            aliases_json TEXT,
            metadata_updated_at REAL,
            release_date BIGINT,
            release_updated_at REAL
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_media_metadata_updated_at_v1
            ON {table_name} (metadata_updated_at)
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_media_metadata_release_updated_at_v1
            ON {table_name} (release_updated_at)
        """,
    ),
)

SERIES_EPISODE_INDEX_TABLE_SPEC = ManagedTableSpec(
    table_name="series_episode_index",
    create_sql="""
        CREATE TABLE {table_name} (
            series_id TEXT NOT NULL,
            season INTEGER NOT NULL,
            episode INTEGER NOT NULL,
            air_date TEXT NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (series_id, season, episode)
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_series_episode_updated_at_v1
            ON {table_name} (updated_at)
        """,
    ),
)

MEDIA_DEMAND_TABLE_SPEC = ManagedTableSpec(
    table_name="media_demand",
    create_sql="""
        CREATE TABLE {table_name} (
            media_id TEXT PRIMARY KEY,
            first_seen_at REAL NOT NULL,
            last_seen_at REAL NOT NULL
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_media_demand_last_seen_v1
            ON {table_name} (last_seen_at)
        """,
    ),
)

TORRENTS_TABLE_SPEC = ManagedTableSpec(
    table_name="torrents",
    create_sql="""
        CREATE TABLE {table_name} (
            media_id TEXT NOT NULL,
            info_hash TEXT NOT NULL,
            season INTEGER,
            episode INTEGER,
            season_norm INTEGER NOT NULL DEFAULT -1,
            episode_norm INTEGER NOT NULL DEFAULT -1,
            file_index INTEGER,
            title TEXT NOT NULL,
            seeders INTEGER,
            size BIGINT,
            tracker TEXT,
            sources_json TEXT NOT NULL DEFAULT '[]',
            parsed_json TEXT NOT NULL,
            updated_at REAL NOT NULL,
            CHECK ((season IS NULL AND season_norm = -1) OR season = season_norm),
            CHECK ((episode IS NULL AND episode_norm = -1) OR episode = episode_norm)
        )
    """,
    legacy_columns=(
        LegacyColumnMigration("season_norm", "season_norm INTEGER NOT NULL DEFAULT -1"),
        LegacyColumnMigration(
            "episode_norm", "episode_norm INTEGER NOT NULL DEFAULT -1"
        ),
        LegacyColumnMigration(
            column_name="sources_json",
            column_sql="sources_json TEXT",
            legacy_name="sources",
        ),
        LegacyColumnMigration(
            column_name="parsed_json",
            column_sql="parsed_json TEXT",
            legacy_name="parsed",
        ),
        LegacyColumnMigration(
            column_name="updated_at",
            column_sql="updated_at REAL",
            legacy_name="timestamp",
        ),
    ),
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_torrents_lookup_v3
            ON {table_name} (media_id, season, episode)
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_torrents_info_hash_v3
            ON {table_name} (info_hash)
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_torrents_updated_at_v1
            ON {table_name} (updated_at)
        """,
    ),
)

DEBRID_AVAILABILITY_TABLE_SPEC = ManagedTableSpec(
    table_name="debrid_availability",
    create_sql="""
        CREATE TABLE {table_name} (
            debrid_service TEXT NOT NULL,
            info_hash TEXT NOT NULL,
            season INTEGER,
            episode INTEGER,
            season_norm INTEGER NOT NULL DEFAULT -1,
            episode_norm INTEGER NOT NULL DEFAULT -1,
            file_index TEXT,
            title TEXT,
            size BIGINT,
            parsed_json TEXT,
            updated_at REAL NOT NULL,
            CHECK ((season IS NULL AND season_norm = -1) OR season = season_norm),
            CHECK ((episode IS NULL AND episode_norm = -1) OR episode = episode_norm)
        )
    """,
    legacy_columns=(
        LegacyColumnMigration("season_norm", "season_norm INTEGER NOT NULL DEFAULT -1"),
        LegacyColumnMigration(
            "episode_norm", "episode_norm INTEGER NOT NULL DEFAULT -1"
        ),
        LegacyColumnMigration(
            column_name="parsed_json",
            column_sql="parsed_json TEXT",
            legacy_name="parsed",
        ),
        LegacyColumnMigration(
            column_name="updated_at",
            column_sql="updated_at REAL",
            legacy_name="timestamp",
        ),
    ),
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_debrid_scope_lookup_v3
            ON {table_name} (info_hash, season_norm, episode_norm, updated_at DESC)
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_debrid_updated_at_v1
            ON {table_name} (updated_at)
        """,
    ),
)

DOWNLOAD_LINKS_CACHE_TABLE_SPEC = ManagedTableSpec(
    table_name="download_links_cache",
    create_sql="""
        CREATE TABLE {table_name} (
            debrid_service TEXT NOT NULL,
            account_key_hash TEXT NOT NULL,
            info_hash TEXT NOT NULL,
            season INTEGER,
            episode INTEGER,
            season_norm INTEGER NOT NULL DEFAULT -1,
            episode_norm INTEGER NOT NULL DEFAULT -1,
            download_url TEXT NOT NULL,
            updated_at REAL NOT NULL,
            CHECK ((season IS NULL AND season_norm = -1) OR season = season_norm),
            CHECK ((episode IS NULL AND episode_norm = -1) OR episode = episode_norm)
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_download_links_updated_at_v1
            ON {table_name} (updated_at)
        """,
    ),
)

DEBRID_ACCOUNT_MAGNETS_TABLE_SPEC = ManagedTableSpec(
    table_name="debrid_account_magnets",
    create_sql="""
        CREATE TABLE {table_name} (
            debrid_service TEXT NOT NULL,
            account_key_hash TEXT NOT NULL,
            magnet_id TEXT NOT NULL,
            info_hash TEXT NOT NULL,
            name TEXT NOT NULL,
            size BIGINT,
            status TEXT NOT NULL,
            added_at REAL NOT NULL,
            synced_at REAL NOT NULL,
            PRIMARY KEY (debrid_service, account_key_hash, magnet_id)
        )
    """,
    legacy_columns=(
        LegacyColumnMigration(
            column_name="synced_at",
            column_sql="synced_at REAL",
            legacy_name="timestamp",
            backfill_expression="COALESCE(synced_at, timestamp)",
        ),
    ),
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_debrid_account_lookup_v2
            ON {table_name} (debrid_service, account_key_hash, synced_at, added_at)
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_debrid_account_synced_at_v1
            ON {table_name} (synced_at)
        """,
    ),
)

DEBRID_ACCOUNT_SYNC_STATE_TABLE_SPEC = ManagedTableSpec(
    table_name="debrid_account_sync_state",
    create_sql="""
        CREATE TABLE {table_name} (
            debrid_service TEXT NOT NULL,
            account_key_hash TEXT NOT NULL,
            last_sync_at REAL NOT NULL,
            PRIMARY KEY (debrid_service, account_key_hash)
        )
    """,
    legacy_columns=(
        LegacyColumnMigration(
            column_name="last_sync_at",
            column_sql="last_sync_at REAL",
            legacy_name="last_sync",
            backfill_expression="COALESCE(last_sync_at, last_sync)",
        ),
    ),
)

ACTIVE_CONNECTIONS_TABLE_SPEC = ManagedTableSpec(
    table_name="active_connections",
    create_sql="""
        CREATE TABLE {table_name} (
            id TEXT PRIMARY KEY,
            ip TEXT NOT NULL,
            content TEXT NOT NULL,
            started_at REAL NOT NULL
        )
    """,
    legacy_columns=(
        LegacyColumnMigration(
            column_name="started_at",
            column_sql="started_at REAL",
            legacy_name="timestamp",
            backfill_expression="COALESCE(started_at, timestamp)",
        ),
    ),
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_connections_started_at_desc_v2
            ON {table_name} (started_at DESC)
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_connections_ip_started_at_v2
            ON {table_name} (ip, started_at DESC)
        """,
    ),
)

BANDWIDTH_STATS_TABLE_SPEC = ManagedTableSpec(
    table_name="bandwidth_stats",
    create_sql="""
        CREATE TABLE {table_name} (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            total_bytes BIGINT NOT NULL,
            updated_at REAL
        )
    """,
    legacy_columns=(
        LegacyColumnMigration(
            column_name="updated_at",
            column_sql="updated_at REAL",
            legacy_name="last_updated",
            backfill_expression="COALESCE(updated_at, last_updated)",
        ),
    ),
)

BACKGROUND_SCRAPER_ITEMS_TABLE_SPEC = ManagedTableSpec(
    table_name="background_scraper_items",
    create_sql="""
        CREATE TABLE {table_name} (
            media_id TEXT PRIMARY KEY,
            media_type TEXT NOT NULL,
            title TEXT NOT NULL,
            year INTEGER NOT NULL,
            year_end INTEGER,
            priority_score REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'discovered',
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            last_scraped_at REAL,
            last_success_at REAL,
            last_failure_at REAL,
            next_retry_at REAL,
            total_torrents_found INTEGER NOT NULL DEFAULT 0,
            created_at REAL,
            updated_at REAL
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_bg_items_status_v2
            ON {table_name} (status, updated_at)
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_bg_items_plan_window_v2
            ON {table_name}
            (media_type, next_retry_at, last_success_at, status, consecutive_failures, priority_score DESC, last_scraped_at)
        """,
    ),
)

METRICS_CACHE_TABLE_SPEC = ManagedTableSpec(
    table_name="metrics_cache",
    create_sql="""
        CREATE TABLE {table_name} (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            payload_json TEXT NOT NULL,
            refreshed_at REAL NOT NULL
        )
    """,
    legacy_columns=(
        LegacyColumnMigration(
            column_name="payload_json",
            column_sql="payload_json TEXT",
            legacy_name="data",
            backfill_expression="COALESCE(payload_json, data)",
        ),
        LegacyColumnMigration(
            column_name="refreshed_at",
            column_sql="refreshed_at REAL",
            legacy_name="timestamp",
            backfill_expression="COALESCE(refreshed_at, timestamp)",
        ),
    ),
)

ANIME_ENTRIES_TABLE_SPEC = ManagedTableSpec(
    table_name="anime_entries",
    create_sql="""
        CREATE TABLE {table_name} (
            id INTEGER PRIMARY KEY,
            data_json TEXT NOT NULL
        )
    """,
    legacy_columns=(
        LegacyColumnMigration(
            column_name="data_json",
            column_sql="data_json TEXT",
            legacy_name="data",
            backfill_expression="COALESCE(data_json, data)",
        ),
    ),
)

ANIME_MAPPING_STATE_TABLE_SPEC = ManagedTableSpec(
    table_name="anime_mapping_state",
    create_sql="""
        CREATE TABLE {table_name} (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            refreshed_at REAL NOT NULL
        )
    """,
)

ANIME_PROVIDER_OVERRIDES_TABLE_SPEC = ManagedTableSpec(
    table_name="anime_provider_overrides",
    create_sql="""
        CREATE TABLE {table_name} (
            source_provider TEXT NOT NULL,
            source_id TEXT NOT NULL,
            target_provider TEXT NOT NULL,
            target_id TEXT NOT NULL,
            from_season INTEGER,
            from_episode INTEGER,
            PRIMARY KEY (source_provider, source_id, target_provider)
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_anime_overrides_target_v1
            ON {table_name} (target_provider, target_id)
        """,
    ),
)

DMM_ENTRIES_TABLE_SPEC = ManagedTableSpec(
    table_name="dmm_entries",
    create_sql="""
        CREATE TABLE {table_name} (
            info_hash TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            size BIGINT,
            parsed_title TEXT,
            parsed_year INTEGER
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_dmm_parsed_year_v2
            ON {table_name} (parsed_year)
        """,
    ),
)

DMM_INGESTED_FILES_TABLE_SPEC = ManagedTableSpec(
    table_name="dmm_ingested_files",
    create_sql="""
        CREATE TABLE {table_name} (
            filename TEXT PRIMARY KEY
        )
    """,
)

BACKGROUND_SCRAPER_RUNS_INDEX_SQL = (
    """
        CREATE INDEX IF NOT EXISTS idx_bg_runs_started_v2
        ON {table_name} (started_at DESC)
    """,
    """
        CREATE INDEX IF NOT EXISTS idx_bg_runs_status_started_v2
        ON {table_name} (status, started_at DESC)
    """,
)

BACKGROUND_SCRAPER_RUNS_TABLE_SPEC = ManagedTableSpec(
    table_name="background_scraper_runs",
    create_sql="""
        CREATE TABLE {table_name} (
            run_id TEXT PRIMARY KEY,
            started_at REAL NOT NULL,
            finished_at REAL,
            status TEXT NOT NULL,
            processed_count INTEGER NOT NULL DEFAULT 0,
            success_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0,
            torrents_found_count INTEGER NOT NULL DEFAULT 0,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            worker_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT
        )
    """,
    legacy_columns=(
        LegacyColumnMigration(
            "processed_count",
            "processed_count INTEGER NOT NULL DEFAULT 0",
        ),
        LegacyColumnMigration(
            "success_count",
            "success_count INTEGER NOT NULL DEFAULT 0",
        ),
        LegacyColumnMigration(
            "failed_count", "failed_count INTEGER NOT NULL DEFAULT 0"
        ),
        LegacyColumnMigration(
            "torrents_found_count",
            "torrents_found_count INTEGER NOT NULL DEFAULT 0",
        ),
    ),
    index_sql=BACKGROUND_SCRAPER_RUNS_INDEX_SQL,
)

BACKGROUND_SCRAPER_RUNS_COPY_SQL = """
    INSERT INTO {table_name} (
        run_id,
        started_at,
        finished_at,
        status,
        processed_count,
        success_count,
        failed_count,
        torrents_found_count,
        duration_ms,
        worker_count,
        last_error
    )
    SELECT
        run_id,
        started_at,
        finished_at,
        status,
        COALESCE(processed_count, 0),
        COALESCE(success_count, 0),
        COALESCE(failed_count, 0),
        COALESCE(torrents_found_count, 0),
        COALESCE(duration_ms, 0),
        COALESCE(worker_count, 0),
        last_error
    FROM background_scraper_runs
"""

ANIME_IDS_TABLE_SPEC = ManagedTableSpec(
    table_name="anime_ids",
    create_sql="""
        CREATE TABLE {table_name} (
            provider TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            entry_id INTEGER NOT NULL,
            PRIMARY KEY (provider, provider_id),
            FOREIGN KEY (entry_id) REFERENCES anime_entries(id) ON DELETE CASCADE
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_anime_ids_entry_provider_v2
            ON {table_name} (entry_id, provider, provider_id)
        """,
    ),
)

ANIME_IDS_COPY_SQL = """
    INSERT INTO {table_name} (provider, provider_id, entry_id)
    SELECT provider, provider_id, entry_id
    FROM anime_ids
"""

BACKGROUND_SCRAPER_EPISODES_TABLE_SPEC = ManagedTableSpec(
    table_name="background_scraper_episodes",
    create_sql="""
        CREATE TABLE {table_name} (
            episode_media_id TEXT PRIMARY KEY,
            series_id TEXT NOT NULL,
            season INTEGER NOT NULL,
            episode INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'discovered',
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            last_scraped_at REAL,
            last_success_at REAL,
            last_failure_at REAL,
            next_retry_at REAL,
            total_torrents_found INTEGER NOT NULL DEFAULT 0,
            created_at REAL,
            updated_at REAL,
            FOREIGN KEY (series_id) REFERENCES background_scraper_items(media_id) ON DELETE CASCADE,
            UNIQUE (series_id, season, episode)
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_bg_episodes_plan_window_v2
            ON {table_name}
            (series_id, next_retry_at, last_success_at, status, consecutive_failures, season, episode)
        """,
    ),
)

BACKGROUND_SCRAPER_EPISODES_COPY_SQL = """
    INSERT INTO {table_name} (
        episode_media_id,
        series_id,
        season,
        episode,
        status,
        consecutive_failures,
        last_scraped_at,
        last_success_at,
        last_failure_at,
        next_retry_at,
        total_torrents_found,
        created_at,
        updated_at
    )
    SELECT
        episode_media_id,
        series_id,
        season,
        episode,
        COALESCE(status, 'discovered'),
        COALESCE(consecutive_failures, 0),
        last_scraped_at,
        last_success_at,
        last_failure_at,
        next_retry_at,
        COALESCE(total_torrents_found, 0),
        created_at,
        updated_at
    FROM background_scraper_episodes
"""

CURRENT_NON_UNIQUE_INDEX_SPECS = (
    SCRAPE_LOCKS_TABLE_SPEC,
    KODI_SETUP_CODES_TABLE_SPEC,
    MEDIA_METADATA_CACHE_TABLE_SPEC,
    SERIES_EPISODE_INDEX_TABLE_SPEC,
    MEDIA_DEMAND_TABLE_SPEC,
    TORRENTS_TABLE_SPEC,
    DEBRID_AVAILABILITY_TABLE_SPEC,
    DOWNLOAD_LINKS_CACHE_TABLE_SPEC,
    DEBRID_ACCOUNT_MAGNETS_TABLE_SPEC,
    ACTIVE_CONNECTIONS_TABLE_SPEC,
    BACKGROUND_SCRAPER_ITEMS_TABLE_SPEC,
    BACKGROUND_SCRAPER_RUNS_TABLE_SPEC,
    ANIME_IDS_TABLE_SPEC,
    BACKGROUND_SCRAPER_EPISODES_TABLE_SPEC,
    ANIME_PROVIDER_OVERRIDES_TABLE_SPEC,
    DMM_ENTRIES_TABLE_SPEC,
)

UNIQUE_INDEX_SPECS = (
    UniqueIndexSpec(
        table_name="torrents",
        index_name="unq_torrents_scope_v3",
        index_sql="""
            CREATE UNIQUE INDEX IF NOT EXISTS unq_torrents_scope_v3
            ON torrents (media_id, info_hash, season_norm, episode_norm)
        """,
        partition_columns=("media_id", "info_hash", "season_norm", "episode_norm"),
        order_by_sql=(
            "COALESCE(updated_at, 0) DESC, COALESCE(seeders, -1) DESC, title DESC"
        ),
    ),
    UniqueIndexSpec(
        table_name="debrid_availability",
        index_name="unq_debrid_scope_v3",
        index_sql="""
            CREATE UNIQUE INDEX IF NOT EXISTS unq_debrid_scope_v3
            ON debrid_availability (debrid_service, info_hash, season_norm, episode_norm)
        """,
        partition_columns=(
            "debrid_service",
            "info_hash",
            "season_norm",
            "episode_norm",
        ),
        order_by_sql=(
            "COALESCE(updated_at, 0) DESC, "
            "COALESCE(size, -1) DESC, COALESCE(title, '') DESC"
        ),
    ),
    UniqueIndexSpec(
        table_name="download_links_cache",
        index_name="unq_download_links_scope_v3",
        index_sql="""
            CREATE UNIQUE INDEX IF NOT EXISTS unq_download_links_scope_v3
            ON download_links_cache (
                debrid_service,
                account_key_hash,
                info_hash,
                season_norm,
                episode_norm
            )
        """,
        partition_columns=(
            "debrid_service",
            "account_key_hash",
            "info_hash",
            "season_norm",
            "episode_norm",
        ),
        order_by_sql="COALESCE(updated_at, 0) DESC, download_url DESC",
    ),
)

LEGACY_STORAGE_COLUMN_CLEANUP = [
    ("db_maintenance", ["last_startup_cleanup"]),
    ("scrape_locks", ["timestamp"]),
    ("kodi_setup_codes", ["b64config", "created_at"]),
    ("torrents", ["sources", "parsed", "timestamp"]),
    ("debrid_availability", ["parsed", "timestamp"]),
    ("download_links_cache", ["timestamp"]),
    ("debrid_account_magnets", ["timestamp"]),
    ("debrid_account_sync_state", ["last_sync"]),
    ("active_connections", ["timestamp"]),
    ("bandwidth_stats", ["last_updated"]),
    ("background_scraper_items", ["source"]),
    ("metrics_cache", ["data", "timestamp"]),
    ("anime_entries", ["data"]),
    ("dmm_ingested_files", ["timestamp"]),
]
