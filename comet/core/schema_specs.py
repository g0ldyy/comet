from dataclasses import dataclass

from comet.usenet.limits import MAX_NZB_METADATA_BYTES

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


def _lower_hex_check(column: str, length: int) -> str:
    stripped = column
    for character in "0123456789abcdef":
        stripped = f"replace({stripped}, '{character}', '')"
    return (
        f"length({column}) = {length} AND lower({column}) = {column} "
        f"AND length({stripped}) = 0"
    )


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
            last_startup_cleanup_at DOUBLE PRECISION
        )
    """,
    legacy_columns=(
        LegacyColumnMigration(
            column_name="last_startup_cleanup_at",
            column_sql="last_startup_cleanup_at DOUBLE PRECISION",
            legacy_name="last_startup_cleanup",
            backfill_expression="COALESCE(last_startup_cleanup_at, last_startup_cleanup)",
        ),
    ),
)

OPERATOR_SETTINGS_TABLE_SPEC = ManagedTableSpec(
    table_name="operator_settings",
    create_sql="""
        CREATE TABLE {table_name} (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            revision BIGINT NOT NULL CHECK (revision > 0),
            updated_at DOUBLE PRECISION NOT NULL,
            updated_by TEXT NOT NULL
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_operator_settings_revision_v1
            ON {table_name} (revision)
        """,
    ),
)

OPERATOR_SETTINGS_STATE_TABLE_SPEC = ManagedTableSpec(
    table_name="operator_settings_state",
    create_sql="""
        CREATE TABLE {table_name} (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            current_revision BIGINT NOT NULL CHECK (current_revision >= 0)
        )
    """,
)

OPERATOR_SETTINGS_REVISIONS_TABLE_SPEC = ManagedTableSpec(
    table_name="operator_settings_revisions",
    create_sql="""
        CREATE TABLE {table_name} (
            revision BIGINT PRIMARY KEY CHECK (revision > 0),
            created_at DOUBLE PRECISION NOT NULL,
            created_by TEXT NOT NULL,
            changed_keys_json TEXT NOT NULL
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_operator_revisions_created_v1
            ON {table_name} (created_at DESC)
        """,
    ),
)

OPERATOR_SETTINGS_AUDIT_TABLE_SPEC = ManagedTableSpec(
    table_name="operator_settings_audit",
    create_sql="""
        CREATE TABLE {table_name} (
            id TEXT PRIMARY KEY,
            revision BIGINT,
            key TEXT NOT NULL,
            action TEXT NOT NULL,
            previous_source TEXT,
            next_source TEXT,
            changed_at DOUBLE PRECISION NOT NULL,
            changed_by TEXT NOT NULL
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_operator_audit_changed_v1
            ON {table_name} (changed_at DESC, id)
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_operator_audit_revision_v1
            ON {table_name} (revision, key)
        """,
    ),
)

OPERATOR_GENERATED_SECRETS_TABLE_SPEC = ManagedTableSpec(
    table_name="operator_generated_secrets",
    create_sql="""
        CREATE TABLE {table_name} (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            created_at DOUBLE PRECISION NOT NULL
        )
    """,
)

OPERATOR_SESSION_REVOCATIONS_TABLE_SPEC = ManagedTableSpec(
    table_name="operator_session_revocations",
    create_sql="""
        CREATE TABLE {table_name} (
            token_hash TEXT PRIMARY KEY,
            scope TEXT NOT NULL,
            expires_at DOUBLE PRECISION NOT NULL,
            revoked_at DOUBLE PRECISION NOT NULL
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_operator_session_expiry_v1
            ON {table_name} (expires_at)
        """,
    ),
)

RUNTIME_INSTANCES_TABLE_SPEC = ManagedTableSpec(
    table_name="runtime_instances",
    create_sql="""
        CREATE TABLE {table_name} (
            instance_id TEXT PRIMARY KEY,
            alias TEXT,
            hostname TEXT NOT NULL,
            started_at DOUBLE PRECISION NOT NULL,
            last_heartbeat DOUBLE PRECISION NOT NULL,
            commit_hash TEXT,
            branch TEXT NOT NULL,
            build_date TEXT,
            applied_revision BIGINT NOT NULL,
            pending_restart_keys_json TEXT NOT NULL DEFAULT '[]',
            readiness_json TEXT NOT NULL,
            restart_capable INTEGER NOT NULL CHECK (
                restart_capable IN (0, 1)
            )
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_runtime_instances_heartbeat_v1
            ON {table_name} (last_heartbeat DESC)
        """,
    ),
)

RUNTIME_PROCESSES_TABLE_SPEC = ManagedTableSpec(
    table_name="runtime_processes",
    create_sql="""
        CREATE TABLE {table_name} (
            instance_id TEXT NOT NULL,
            process_id BIGINT NOT NULL CHECK (process_id > 0),
            role TEXT NOT NULL,
            started_at DOUBLE PRECISION NOT NULL,
            last_heartbeat DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (instance_id, process_id),
            FOREIGN KEY (instance_id) REFERENCES runtime_instances(instance_id)
                ON DELETE CASCADE
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_runtime_processes_heartbeat_v1
            ON {table_name} (last_heartbeat DESC)
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_runtime_processes_role_v1
            ON {table_name} (role, last_heartbeat DESC)
        """,
    ),
)

OPERATIONAL_EVENT_STATE_TABLE_SPEC = ManagedTableSpec(
    table_name="operational_event_state",
    create_sql="""
        CREATE TABLE {table_name} (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            current_event_id BIGINT NOT NULL CHECK (current_event_id >= 0),
            dropped_events BIGINT NOT NULL CHECK (dropped_events >= 0)
        )
    """,
)

OPERATIONAL_EVENTS_TABLE_SPEC = ManagedTableSpec(
    table_name="operational_events",
    create_sql="""
        CREATE TABLE {table_name} (
            id BIGINT PRIMARY KEY CHECK (id > 0),
            created_at DOUBLE PRECISION NOT NULL,
            instance_id TEXT NOT NULL,
            process_id BIGINT NOT NULL CHECK (process_id > 0),
            role TEXT NOT NULL,
            level TEXT NOT NULL,
            category TEXT NOT NULL,
            event TEXT NOT NULL,
            message TEXT NOT NULL,
            request_id TEXT,
            run_id TEXT,
            connection_id TEXT,
            media_type TEXT,
            provider_name TEXT,
            outcome TEXT,
            error_code TEXT,
            details_json TEXT NOT NULL
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_operational_events_created_v1
            ON {table_name} (created_at DESC, id DESC)
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_operational_events_category_v1
            ON {table_name} (category, id DESC)
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_operational_events_runtime_v1
            ON {table_name} (instance_id, role, id DESC)
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_operational_events_request_v1
            ON {table_name} (request_id, id)
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_operational_events_run_v1
            ON {table_name} (run_id, id)
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_operational_events_connection_v1
            ON {table_name} (connection_id, id)
        """,
    ),
)

SCRAPE_LOCKS_TABLE_SPEC = ManagedTableSpec(
    table_name="scrape_locks",
    create_sql="""
        CREATE TABLE {table_name} (
            lock_key TEXT PRIMARY KEY,
            instance_id TEXT NOT NULL,
            updated_at DOUBLE PRECISION NOT NULL,
            expires_at DOUBLE PRECISION NOT NULL
        )
    """,
    legacy_columns=(
        LegacyColumnMigration(
            column_name="updated_at",
            column_sql="updated_at DOUBLE PRECISION",
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
            expires_at DOUBLE PRECISION NOT NULL
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
            metadata_updated_at DOUBLE PRECISION,
            aliases_updated_at DOUBLE PRECISION,
            release_date BIGINT,
            release_updated_at DOUBLE PRECISION
        )
    """,
    legacy_columns=(
        LegacyColumnMigration(
            column_name="aliases_updated_at",
            column_sql="aliases_updated_at DOUBLE PRECISION",
        ),
    ),
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_media_metadata_updated_at_v1
            ON {table_name} (metadata_updated_at)
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_media_metadata_aliases_updated_at_v1
            ON {table_name} (aliases_updated_at)
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_media_metadata_release_updated_at_v1
            ON {table_name} (release_updated_at)
        """,
    ),
)

IMDB_TITLE_LOOKUP_TABLE_SPEC = ManagedTableSpec(
    table_name="imdb_title_lookup",
    create_sql="""
        CREATE TABLE {table_name} (
            query_key TEXT PRIMARY KEY,
            imdb_id TEXT NOT NULL,
            media_type TEXT NOT NULL,
            year INTEGER,
            updated_at DOUBLE PRECISION NOT NULL
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_imdb_title_lookup_updated_at_v1
            ON {table_name} (updated_at)
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
            updated_at DOUBLE PRECISION NOT NULL,
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

SERIES_EPISODE_INDEX_REFRESH_TABLE_SPEC = ManagedTableSpec(
    table_name="series_episode_index_refresh",
    create_sql="""
        CREATE TABLE {table_name} (
            series_id TEXT PRIMARY KEY,
            refreshed_at DOUBLE PRECISION NOT NULL
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_series_episode_refresh_refreshed_at_v1
            ON {table_name} (refreshed_at)
        """,
    ),
)

NZB_ARTIFACTS_TABLE_SPEC = ManagedTableSpec(
    table_name="nzb_artifacts",
    create_sql="""
        CREATE TABLE {table_name} (
            artifact_sha256 CHAR(64) PRIMARY KEY,
            byte_size BIGINT NOT NULL CHECK (byte_size > 0),
            storage_kind TEXT NOT NULL DEFAULT 'nzb'
                CHECK (storage_kind IN ('nzb', 'materialized_asset')),
            relative_path TEXT NOT NULL,
            publication_state TEXT NOT NULL DEFAULT 'published'
                CHECK (publication_state IN ('publishing', 'published', 'tombstoned')),
            refcount BIGINT NOT NULL DEFAULT 0 CHECK (refcount >= 0),
            source_manifest_identity VARCHAR(68) CHECK (
                source_manifest_identity IS NULL
                OR (
                    length(source_manifest_identity) = 68
                    AND substr(source_manifest_identity, 1, 4) = 'nm1:'
                )
            ),
            selected_asset_id CHAR(64),
            strong_asset_revision CHAR(64),
            logical_length BIGINT CHECK (logical_length > 0),
            created_at DOUBLE PRECISION NOT NULL,
            last_used_at DOUBLE PRECISION NOT NULL,
            tombstoned_at DOUBLE PRECISION
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_nzb_artifacts_last_used_v1
            ON {table_name} (last_used_at)
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_nzb_artifacts_gc_v1
            ON {table_name} (publication_state, refcount, last_used_at)
        """,
    ),
)

NZB_CONTENTS_TABLE_SPEC = ManagedTableSpec(
    table_name="nzb_contents",
    create_sql=f"""
        CREATE TABLE {{table_name}} (
            manifest_identity VARCHAR(68) NOT NULL CHECK (
                length(manifest_identity) = 68
                AND substr(manifest_identity, 1, 4) = 'nm1:'
                AND {_lower_hex_check("substr(manifest_identity, 5)", 64)}
            ),
            parser_version INTEGER NOT NULL CHECK (
                parser_version BETWEEN 1 AND 65535
            ),
            posting_set_identity VARCHAR(44) NOT NULL CHECK (
                length(posting_set_identity) = 44
                AND substr(posting_set_identity, 1, 4) = 'nh1:'
                AND {_lower_hex_check("substr(posting_set_identity, 5)", 40)}
            ),
            artifact_sha256 CHAR(64) NOT NULL
                REFERENCES nzb_artifacts(artifact_sha256) ON DELETE RESTRICT
                CHECK ({_lower_hex_check("artifact_sha256", 64)}),
            manifest_json TEXT NOT NULL CHECK (
                length(manifest_json) BETWEEN 2 AND {MAX_NZB_METADATA_BYTES}
            ),
            inspection_state VARCHAR(16) NOT NULL DEFAULT 'parsed' CHECK (
                inspection_state IN ('parsed', 'inspected', 'failed')
            ),
            created_at DOUBLE PRECISION NOT NULL CHECK (created_at >= 0),
            last_used_at DOUBLE PRECISION NOT NULL CHECK (last_used_at >= 0),
            PRIMARY KEY (manifest_identity, parser_version),
            UNIQUE (artifact_sha256, parser_version)
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_nzb_contents_posting_set_v1
            ON {table_name} (posting_set_identity, parser_version)
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_nzb_contents_last_used_v1
            ON {table_name} (last_used_at)
        """,
    ),
)

NZB_ARTIFACT_GRANTS_TABLE_SPEC = ManagedTableSpec(
    table_name="nzb_artifact_grants",
    create_sql="""
        CREATE TABLE {table_name} (
            grant_id CHAR(36) PRIMARY KEY,
            artifact_sha256 CHAR(64) NOT NULL REFERENCES nzb_artifacts(artifact_sha256) ON DELETE RESTRICT,
            owner_configuration_partition CHAR(64) NOT NULL,
            created_at DOUBLE PRECISION NOT NULL,
            last_used_at DOUBLE PRECISION NOT NULL,
            expires_at DOUBLE PRECISION NOT NULL,
            UNIQUE (artifact_sha256, owner_configuration_partition)
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_nzb_artifact_grants_expiry_v1
            ON {table_name} (expires_at)
        """,
    ),
)

ARTIFACT_READER_LEASES_TABLE_SPEC = ManagedTableSpec(
    table_name="artifact_reader_leases",
    create_sql="""
        CREATE TABLE {table_name} (
            lease_id CHAR(36) PRIMARY KEY,
            artifact_sha256 CHAR(64) NOT NULL REFERENCES nzb_artifacts(artifact_sha256) ON DELETE RESTRICT,
            runtime_owner CHAR(36) NOT NULL,
            acquired_at DOUBLE PRECISION NOT NULL,
            heartbeat_at DOUBLE PRECISION NOT NULL,
            expires_at DOUBLE PRECISION NOT NULL,
            CHECK (expires_at > acquired_at)
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_artifact_reader_leases_artifact_expiry_v1
            ON {table_name} (artifact_sha256, expires_at)
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_artifact_reader_leases_expiry_v1
            ON {table_name} (expires_at)
        """,
    ),
)

ARTIFACT_PUBLICATION_LEASES_TABLE_SPEC = ManagedTableSpec(
    table_name="artifact_publication_leases",
    create_sql="""
        CREATE TABLE {table_name} (
            lease_id CHAR(36) PRIMARY KEY,
            preparation_id CHAR(36) NOT NULL,
            runtime_owner CHAR(36) NOT NULL,
            acquired_at DOUBLE PRECISION NOT NULL,
            heartbeat_at DOUBLE PRECISION NOT NULL,
            expires_at DOUBLE PRECISION NOT NULL,
            CHECK (expires_at > acquired_at)
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_artifact_publication_leases_expiry_v1
            ON {table_name} (expires_at)
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_artifact_publication_leases_preparation_v1
            ON {table_name} (preparation_id, expires_at)
        """,
    ),
)

NZB_PROVIDER_EXPORTS_TABLE_SPEC = ManagedTableSpec(
    table_name="nzb_provider_exports",
    create_sql="""
        CREATE TABLE {table_name} (
            export_token CHAR(32) PRIMARY KEY,
            owner_configuration_partition CHAR(64) NOT NULL,
            grant_id CHAR(36) NOT NULL REFERENCES nzb_artifact_grants(grant_id) ON DELETE RESTRICT,
            provider_configuration_id CHAR(36) NOT NULL,
            credential_fingerprint CHAR(64) NOT NULL,
            audience TEXT NOT NULL CHECK (audience = 'stremthru-newz'),
            active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at DOUBLE PRECISION NOT NULL,
            last_used_at DOUBLE PRECISION NOT NULL,
            request_count BIGINT NOT NULL DEFAULT 0 CHECK (request_count >= 0),
            byte_count BIGINT NOT NULL DEFAULT 0 CHECK (byte_count >= 0),
            revoked_at DOUBLE PRECISION,
            revocation_reason TEXT
        )
    """,
    index_sql=(
        """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_nzb_provider_exports_active_binding_v1
            ON {table_name} (
                owner_configuration_partition, grant_id, provider_configuration_id,
                credential_fingerprint, audience
            ) WHERE active = TRUE
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_nzb_provider_exports_last_used_v1
            ON {table_name} (last_used_at)
        """,
    ),
)

PROVIDER_PREPARATIONS_TABLE_SPEC = ManagedTableSpec(
    table_name="provider_preparations",
    create_sql="""
        CREATE TABLE {table_name} (
            preparation_id CHAR(36) PRIMARY KEY,
            owner_configuration_partition CHAR(64) NOT NULL,
            provider_configuration_id CHAR(36) NOT NULL,
            credential_fingerprint CHAR(64) NOT NULL,
            provider_kind TEXT NOT NULL,
            candidate_id CHAR(36) NOT NULL REFERENCES rendered_release_candidates(candidate_id) ON DELETE CASCADE,
            locator_id CHAR(36) NOT NULL REFERENCES rendered_release_locators(locator_id) ON DELETE CASCADE,
            artifact_grant_id CHAR(36) REFERENCES nzb_artifact_grants(grant_id) ON DELETE RESTRICT,
            selection_json TEXT NOT NULL,
            mutation_idempotency_key CHAR(64) NOT NULL UNIQUE,
            provider_payload_json TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('mutation_pending', 'submitted', 'terminal')),
            refcount BIGINT NOT NULL DEFAULT 0 CHECK (refcount >= 0),
            cleanup_state TEXT NOT NULL DEFAULT 'not_required' CHECK (
                cleanup_state IN (
                    'ownership_pending', 'not_required', 'required',
                    'in_progress', 'complete'
                )
            ),
            created_at DOUBLE PRECISION NOT NULL,
            updated_at DOUBLE PRECISION NOT NULL,
            last_polled_at DOUBLE PRECISION,
            terminal_at DOUBLE PRECISION,
            gc_after_at DOUBLE PRECISION
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_provider_preparations_status_v1
            ON {table_name} (provider_kind, provider_configuration_id, state)
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_provider_preparations_owner_v1
            ON {table_name} (owner_configuration_partition, preparation_id)
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_provider_preparations_candidate_v1
            ON {table_name} (candidate_id)
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_provider_preparations_gc_v1
            ON {table_name} (gc_after_at, preparation_id)
            WHERE gc_after_at IS NOT NULL
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_provider_preparations_unref_gc_v1
            ON {table_name} (gc_after_at, preparation_id)
            WHERE refcount = 0 AND gc_after_at IS NOT NULL
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_provider_preparations_cleanup_v1
            ON {table_name} (
                owner_configuration_partition, provider_configuration_id,
                credential_fingerprint, cleanup_state, updated_at
            ) WHERE provider_kind = 'torbox_usenet'
        """,
    ),
)

CAPABILITY_VALIDATION_STATES_TABLE_SPEC = ManagedTableSpec(
    table_name="capability_validation_states",
    create_sql="""
        CREATE TABLE {table_name} (
            binding_fingerprint CHAR(64) PRIMARY KEY,
            binding_kind TEXT NOT NULL CHECK (
                length(binding_kind) BETWEEN 1 AND 64
            ),
            schema_version INTEGER NOT NULL CHECK (
                schema_version BETWEEN 1 AND 65535
            ),
            validator_version TEXT NOT NULL CHECK (
                length(validator_version) BETWEEN 1 AND 64
            ),
            state TEXT NOT NULL CHECK (state IN (
                'pending_validation', 'valid', 'transiently_unreachable',
                'auth_failed', 'plan_incompatible'
            )),
            last_success_at DOUBLE PRECISION,
            observed_at DOUBLE PRECISION NOT NULL,
            fresh_until DOUBLE PRECISION NOT NULL,
            last_known_good_until DOUBLE PRECISION NOT NULL,
            next_refresh_at DOUBLE PRECISION NOT NULL,
            error_code TEXT CHECK (
                error_code IS NULL OR length(error_code) BETWEEN 1 AND 64
            ),
            retry_after DOUBLE PRECISION CHECK (retry_after IS NULL OR retry_after >= 0)
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_capability_validation_lkg_v1
            ON {table_name} (last_known_good_until)
        """,
    ),
)

SEARCH_COVERAGE_TABLE_SPEC = ManagedTableSpec(
    table_name="search_coverage",
    create_sql=f"""
        CREATE TABLE {{table_name}} (
            query_fingerprint CHAR(64) NOT NULL CHECK (
                {_lower_hex_check("query_fingerprint", 64)}
            ),
            branch_fingerprint CHAR(64) NOT NULL CHECK (
                {_lower_hex_check("branch_fingerprint", 64)}
            ),
            freshness_state TEXT NOT NULL CHECK (
                freshness_state IN (
                    'fresh', 'failed', 'failed_with_results'
                )
            ),
            next_refresh_at DOUBLE PRECISION NOT NULL CHECK (
                next_refresh_at >= 0
            ),
            PRIMARY KEY (
                query_fingerprint, branch_fingerprint
            )
        )
    """,
)

RELEASE_CANDIDATES_TABLE_SPEC = ManagedTableSpec(
    table_name="release_candidates",
    create_sql=f"""
        CREATE TABLE {{table_name}} (
            candidate_id VARCHAR(36) PRIMARY KEY CHECK (
                length(candidate_id) = 36
            ),
            visibility_partition CHAR(64) NOT NULL CHECK (
                {_lower_hex_check("visibility_partition", 64)}
            ),
            media_id TEXT NOT NULL CHECK (
                transport = 'bittorrent'
                OR length(media_id) BETWEEN 1 AND 255
            ),
            transport VARCHAR(16) NOT NULL CHECK (
                transport IN ('bittorrent', 'usenet')
            ),
            release_key TEXT NOT NULL CHECK (
                transport = 'bittorrent'
                OR length(release_key) BETWEEN 1 AND 128
            ),
            scope VARCHAR(32) NOT NULL CHECK (scope IN (
                'movie', 'episode', 'season_pack', 'series_pack',
                'daily_episode', 'anime_episode'
            )),
            season_norm INTEGER NOT NULL,
            episode_norm INTEGER NOT NULL,
            daily_date VARCHAR(10),
            title TEXT NOT NULL CHECK (
                transport = 'bittorrent'
                OR length(title) BETWEEN 1 AND 1024
            ),
            byte_size BIGINT CHECK (
                transport = 'bittorrent'
                OR byte_size IS NULL
                OR byte_size > 0
            ),
            published_at_ms BIGINT CHECK (
                transport = 'bittorrent'
                OR published_at_ms IS NULL
                OR published_at_ms >= 0
            ),
            parsed_json TEXT NOT NULL CHECK (
                transport = 'bittorrent' OR length(parsed_json) <= 65536
            ),
            attributes_json TEXT NOT NULL CHECK (
                transport = 'bittorrent' OR length(attributes_json) <= 65536
            ),
            created_at_ms BIGINT NOT NULL CHECK (created_at_ms >= 0),
            updated_at_ms BIGINT NOT NULL CHECK (updated_at_ms >= 0),
            last_seen_at_ms BIGINT NOT NULL CHECK (last_seen_at_ms >= 0),
            CHECK (
                (scope = 'daily_episode' AND daily_date IS NOT NULL)
                OR (scope <> 'daily_episode' AND daily_date IS NULL)
            ),
            UNIQUE (
                visibility_partition, media_id, transport, release_key,
                scope, season_norm, episode_norm
            )
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_release_candidates_media_scope_v1
            ON {table_name} (
                visibility_partition, media_id, scope,
                season_norm, episode_norm, transport
            )
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_release_candidates_last_seen_v1
            ON {table_name} (last_seen_at_ms)
        """,
    ),
)

CANDIDATE_IDENTITIES_TABLE_SPEC = ManagedTableSpec(
    table_name="candidate_identities",
    create_sql=f"""
        CREATE TABLE {{table_name}} (
            candidate_id VARCHAR(36) NOT NULL
                REFERENCES release_candidates(candidate_id) ON DELETE CASCADE,
            visibility_partition CHAR(64) NOT NULL CHECK (
                {_lower_hex_check("visibility_partition", 64)}
            ),
            transport VARCHAR(16) NOT NULL CHECK (
                transport IN ('bittorrent', 'usenet')
            ),
            identity_scheme VARCHAR(16) NOT NULL CHECK (
                identity_scheme IN ('btih', 'nm1')
            ),
            identity_value TEXT NOT NULL CHECK (
                transport = 'bittorrent'
                OR length(identity_value) BETWEEN 1 AND 256
            ),
            created_at_ms BIGINT NOT NULL CHECK (created_at_ms >= 0),
            updated_at_ms BIGINT NOT NULL CHECK (updated_at_ms >= 0),
            PRIMARY KEY (candidate_id, identity_scheme, identity_value)
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_candidate_identities_exact_v1
            ON {table_name} (
                visibility_partition, transport,
                identity_scheme, identity_value
            )
        """,
    ),
)

CANDIDATE_IDENTITIES_COPY_SQL = """
    INSERT INTO {table_name} (
        candidate_id, visibility_partition, transport,
        identity_scheme, identity_value, created_at_ms, updated_at_ms
    )
    SELECT
        candidate_id, visibility_partition, transport,
        identity_scheme, identity_value, created_at_ms, updated_at_ms
    FROM candidate_identities
"""

CANDIDATE_REDIRECTS_TABLE_SPEC = ManagedTableSpec(
    table_name="candidate_redirects",
    create_sql="""
        CREATE TABLE {table_name} (
            redirected_candidate_id VARCHAR(36) PRIMARY KEY
                REFERENCES release_candidates(candidate_id) ON DELETE RESTRICT,
            canonical_candidate_id VARCHAR(36) NOT NULL
                REFERENCES release_candidates(candidate_id) ON DELETE RESTRICT,
            created_at_ms BIGINT NOT NULL CHECK (created_at_ms >= 0),
            updated_at_ms BIGINT NOT NULL CHECK (updated_at_ms >= 0),
            CHECK (redirected_candidate_id <> canonical_candidate_id)
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_candidate_redirects_canonical_v1
            ON {table_name} (canonical_candidate_id)
        """,
    ),
)

RELEASE_LOCATORS_TABLE_SPEC = ManagedTableSpec(
    table_name="release_locators",
    create_sql=f"""
        CREATE TABLE {{table_name}} (
            locator_id VARCHAR(36) PRIMARY KEY CHECK (
                length(locator_id) = 36
            ),
            candidate_id VARCHAR(36) NOT NULL
                REFERENCES release_candidates(candidate_id) ON DELETE CASCADE,
            origin_kind VARCHAR(32) NOT NULL CHECK (origin_kind IN (
                'discovery', 'manual_upload', 'manual_url'
            )),
            discovery_configuration_id VARCHAR(36),
            owner_configuration_partition CHAR(64) NOT NULL CHECK (
                {_lower_hex_check("owner_configuration_partition", 64)}
            ),
            account_partition CHAR(64) CHECK (
                account_partition IS NULL OR (
                    {_lower_hex_check("account_partition", 64)}
                )
            ),
            locator_kind VARCHAR(32) NOT NULL CHECK (locator_kind IN (
                'torrent', 'real_nzb', 'nzb_artifact', 'easynews_http'
            )),
            locator_json TEXT NOT NULL CHECK (
                locator_kind = 'torrent'
                OR length(locator_json) BETWEEN 2 AND 65536
            ),
            policy_json TEXT NOT NULL CHECK (
                locator_kind = 'torrent'
                OR length(policy_json) BETWEEN 2 AND 16384
            ),
            content_key VARCHAR(256) NOT NULL CHECK (
                length(content_key) BETWEEN 1 AND 256
            ),
            source_expires_at_ms BIGINT CHECK (
                source_expires_at_ms IS NULL OR source_expires_at_ms >= 0
            ),
            updated_at_ms BIGINT NOT NULL CHECK (updated_at_ms >= 0),
            tombstoned_at_ms BIGINT CHECK (
                tombstoned_at_ms IS NULL OR tombstoned_at_ms >= 0
            ),
            CHECK (
                (origin_kind = 'discovery'
                 AND discovery_configuration_id IS NOT NULL)
                OR
                (origin_kind IN ('manual_upload', 'manual_url')
                 AND discovery_configuration_id IS NULL)
            ),
            UNIQUE (
                candidate_id, locator_kind, content_key,
                owner_configuration_partition
            )
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_release_locators_candidate_kind_v1
            ON {table_name} (candidate_id, locator_kind)
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_release_locators_discovery_v1
            ON {table_name} (
                discovery_configuration_id, candidate_id, tombstoned_at_ms
            )
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_release_locators_expiry_v1
            ON {table_name} (source_expires_at_ms)
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_release_locators_tombstone_v1
            ON {table_name} (tombstoned_at_ms)
        """,
    ),
)

RELEASE_LOCATOR_COVERAGE_TABLE_SPEC = ManagedTableSpec(
    table_name="release_locator_coverage",
    create_sql=f"""
        CREATE TABLE {{table_name}} (
            query_fingerprint CHAR(64) NOT NULL CHECK (
                {_lower_hex_check("query_fingerprint", 64)}
            ),
            branch_fingerprint CHAR(64) NOT NULL CHECK (
                {_lower_hex_check("branch_fingerprint", 64)}
            ),
            locator_id VARCHAR(36) NOT NULL
                REFERENCES release_locators(locator_id) ON DELETE CASCADE,
            last_seen_at_ms BIGINT NOT NULL CHECK (last_seen_at_ms >= 0),
            tombstoned_at_ms BIGINT CHECK (
                tombstoned_at_ms IS NULL OR tombstoned_at_ms >= 0
            ),
            PRIMARY KEY (
                query_fingerprint, branch_fingerprint, locator_id
            )
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_release_locator_coverage_lookup_v1
            ON {table_name} (
                query_fingerprint, branch_fingerprint, tombstoned_at_ms
            )
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_release_locator_coverage_locator_v1
            ON {table_name} (locator_id, tombstoned_at_ms)
        """,
    ),
)

PROVIDER_GOVERNOR_WINDOWS_TABLE_SPEC = ManagedTableSpec(
    table_name="provider_governor_windows",
    create_sql=f"""
        CREATE TABLE {{table_name}} (
            scope_key CHAR(64) NOT NULL CHECK (
                {_lower_hex_check("scope_key", 64)}
            ),
            operation VARCHAR(64) NOT NULL CHECK (
                length(operation) BETWEEN 1 AND 64
            ),
            window_start_ms BIGINT NOT NULL CHECK (window_start_ms >= 0),
            window_duration_ms BIGINT NOT NULL CHECK (
                window_duration_ms > 0
            ),
            limit_count INTEGER NOT NULL CHECK (limit_count > 0),
            used_count INTEGER NOT NULL CHECK (used_count >= 0),
            updated_at_ms BIGINT NOT NULL CHECK (updated_at_ms >= 0),
            expires_at_ms BIGINT NOT NULL CHECK (
                expires_at_ms >= window_start_ms + window_duration_ms
            ),
            PRIMARY KEY (
                scope_key, operation, window_start_ms, window_duration_ms
            )
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_provider_governor_windows_expiry_v1
            ON {table_name} (expires_at_ms)
        """,
    ),
)

PROVIDER_GOVERNOR_LEASES_TABLE_SPEC = ManagedTableSpec(
    table_name="provider_governor_leases",
    create_sql=f"""
        CREATE TABLE {{table_name}} (
            lease_id VARCHAR(36) PRIMARY KEY CHECK (length(lease_id) = 36),
            scope_key CHAR(64) NOT NULL CHECK (
                {_lower_hex_check("scope_key", 64)}
            ),
            operation VARCHAR(64) NOT NULL CHECK (
                length(operation) BETWEEN 1 AND 64
            ),
            slot INTEGER NOT NULL CHECK (slot BETWEEN 0 AND 255),
            owner_request_id VARCHAR(128) NOT NULL CHECK (
                length(owner_request_id) BETWEEN 1 AND 128
            ),
            acquired_at_ms BIGINT NOT NULL CHECK (acquired_at_ms >= 0),
            expires_at_ms BIGINT NOT NULL CHECK (
                expires_at_ms > acquired_at_ms
            ),
            UNIQUE (scope_key, operation, slot)
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_provider_governor_leases_scope_v1
            ON {table_name} (scope_key, operation, expires_at_ms)
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_provider_governor_leases_expiry_v1
            ON {table_name} (expires_at_ms)
        """,
    ),
)

RENDERED_RELEASE_CANDIDATES_TABLE_SPEC = ManagedTableSpec(
    table_name="rendered_release_candidates",
    create_sql="""
        CREATE TABLE {table_name} (
            candidate_id CHAR(36) PRIMARY KEY,
            owner_configuration_partition CHAR(64) NOT NULL,
            external_candidate_id TEXT NOT NULL,
            media_id TEXT NOT NULL,
            transport TEXT NOT NULL CHECK (transport IN ('bittorrent', 'usenet')),
            title TEXT NOT NULL,
            byte_size BIGINT,
            parsed_json TEXT NOT NULL,
            created_at DOUBLE PRECISION NOT NULL,
            updated_at DOUBLE PRECISION NOT NULL,
            last_rendered_at DOUBLE PRECISION NOT NULL,
            UNIQUE (owner_configuration_partition, external_candidate_id)
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_rendered_release_candidates_media_v1
            ON {table_name} (owner_configuration_partition, media_id, transport)
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_rendered_release_candidates_gc_v1
            ON {table_name} (last_rendered_at, candidate_id)
        """,
    ),
)

RENDERED_RELEASE_LOCATORS_TABLE_SPEC = ManagedTableSpec(
    table_name="rendered_release_locators",
    create_sql="""
        CREATE TABLE {table_name} (
            locator_id CHAR(36) PRIMARY KEY,
            candidate_id CHAR(36) NOT NULL REFERENCES rendered_release_candidates(candidate_id) ON DELETE CASCADE,
            external_locator_id TEXT NOT NULL,
            locator_kind TEXT NOT NULL,
            locator_json TEXT NOT NULL,
            policy_json TEXT NOT NULL,
            created_at DOUBLE PRECISION NOT NULL,
            updated_at DOUBLE PRECISION NOT NULL,
            last_rendered_at DOUBLE PRECISION NOT NULL,
            UNIQUE (candidate_id, external_locator_id)
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_rendered_release_locators_candidate_v1
            ON {table_name} (candidate_id, locator_kind)
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_rendered_release_locators_gc_v1
            ON {table_name} (last_rendered_at, locator_id)
        """,
    ),
)

ASSET_PREPARATIONS_TABLE_SPEC = ManagedTableSpec(
    table_name="asset_preparations",
    create_sql="""
        CREATE TABLE {table_name} (
            preparation_id CHAR(36) PRIMARY KEY,
            owner_configuration_partition CHAR(64) NOT NULL,
            preparation_intent_key CHAR(64) NOT NULL UNIQUE,
            candidate_id CHAR(36) NOT NULL REFERENCES rendered_release_candidates(candidate_id) ON DELETE CASCADE,
            provider_configuration_id CHAR(36) NOT NULL,
            provider_kind TEXT NOT NULL,
            locator_ids_json TEXT NOT NULL,
            artifact_grant_id CHAR(36)
                REFERENCES nzb_artifact_grants(grant_id) ON DELETE RESTRICT,
            manifest_identity CHAR(68),
            provider_preparation_id CHAR(36)
                REFERENCES provider_preparations(preparation_id) ON DELETE RESTRICT,
            selection_intent_json TEXT NOT NULL,
            selection_intent_version INTEGER NOT NULL CHECK (
                selection_intent_version BETWEEN 1 AND 65535
            ),
            parser_version INTEGER NOT NULL CHECK (
                parser_version BETWEEN 1 AND 65535
            ),
            selector_version INTEGER NOT NULL CHECK (
                selector_version BETWEEN 1 AND 65535
            ),
            archive_plan_version INTEGER NOT NULL CHECK (
                archive_plan_version BETWEEN 1 AND 65535
            ),
            client TEXT NOT NULL CHECK (client IN ('stremio', 'kodi', 'chilllink')),
            state TEXT NOT NULL CHECK (state IN ('pending', 'ready', 'failed')),
            target_kind TEXT,
            reconstruction_blueprint_json TEXT CHECK (
                reconstruction_blueprint_json IS NULL OR
                LENGTH(reconstruction_blueprint_json) BETWEEN 2 AND 32768
            ),
            refcount BIGINT NOT NULL DEFAULT 0 CHECK (refcount >= 0),
            created_at DOUBLE PRECISION NOT NULL,
            last_used_at DOUBLE PRECISION NOT NULL,
            idle_expires_at DOUBLE PRECISION NOT NULL,
            absolute_expires_at DOUBLE PRECISION NOT NULL
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_asset_preparations_expiry_v1
            ON {table_name} (absolute_expires_at, idle_expires_at)
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_asset_preparations_readiness_v1
            ON {table_name} (
                owner_configuration_partition, state, selection_intent_json,
                absolute_expires_at, candidate_id, provider_configuration_id
            )
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_asset_preparations_provider_ref_v1
            ON {table_name} (provider_preparation_id)
            WHERE provider_preparation_id IS NOT NULL
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_asset_preparations_candidate_v1
            ON {table_name} (candidate_id, absolute_expires_at)
        """,
    ),
)

PROVIDER_RESOLUTION_CACHE_TABLE_SPEC = ManagedTableSpec(
    table_name="provider_resolution_cache",
    create_sql=f"""
        CREATE TABLE {{table_name}} (
            resolution_id VARCHAR(36) PRIMARY KEY CHECK (
                length(resolution_id) = 36
            ),
            resolution_key CHAR(64) NOT NULL UNIQUE CHECK (
                {_lower_hex_check("resolution_key", 64)}
            ),
            provider_kind VARCHAR(64) NOT NULL CHECK (
                length(provider_kind) BETWEEN 1 AND 64
            ),
            provider_configuration_id VARCHAR(36) NOT NULL CHECK (
                length(provider_configuration_id) = 36
            ),
            account_partition CHAR(64) NOT NULL CHECK (
                {_lower_hex_check("account_partition", 64)}
            ),
            candidate_id VARCHAR(36) NOT NULL
                REFERENCES release_candidates(candidate_id) ON DELETE CASCADE,
            selection_intent_json TEXT NOT NULL CHECK (
                length(selection_intent_json) BETWEEN 3 AND 4096
            ),
            selected_asset_id CHAR(64) CHECK (
                selected_asset_id IS NULL OR (
                    {_lower_hex_check("selected_asset_id", 64)}
                )
            ),
            client VARCHAR(16) NOT NULL CHECK (
                client IN ('stremio', 'kodi', 'chilllink')
            ),
            target_kind VARCHAR(16) NOT NULL CHECK (
                target_kind IN ('cloud', 'relay', 'native')
            ),
            resolution_version INTEGER NOT NULL CHECK (
                resolution_version BETWEEN 1 AND 65535
            ),
            exact_logical_length BIGINT CHECK (
                exact_logical_length IS NULL OR exact_logical_length > 0
            ),
            strong_asset_revision CHAR(64) CHECK (
                strong_asset_revision IS NULL OR (
                    {_lower_hex_check("strong_asset_revision", 64)}
                )
            ),
            etag_strength VARCHAR(16) NOT NULL CHECK (
                etag_strength IN ('weak', 'strong')
            ),
            observed_at_ms BIGINT NOT NULL CHECK (observed_at_ms >= 0),
            last_used_at_ms BIGINT NOT NULL CHECK (
                last_used_at_ms >= observed_at_ms
            ),
            expires_at_ms BIGINT NOT NULL CHECK (
                expires_at_ms > observed_at_ms
            ),
            CHECK (
                etag_strength = 'weak'
                OR strong_asset_revision IS NOT NULL
            ),
            CHECK (
                selected_asset_id IS NOT NULL
                OR exact_logical_length IS NOT NULL
                OR strong_asset_revision IS NOT NULL
            )
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_provider_resolution_lookup_v1
            ON {table_name} (
                candidate_id, provider_configuration_id,
                account_partition, selection_intent_json,
                client, expires_at_ms
            )
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_provider_resolution_expiry_v1
            ON {table_name} (expires_at_ms, last_used_at_ms)
        """,
    ),
)

ASSET_PREPARATION_ARTIFACTS_TABLE_SPEC = ManagedTableSpec(
    table_name="asset_preparation_artifacts",
    create_sql="""
        CREATE TABLE {table_name} (
            preparation_id CHAR(36) NOT NULL
                REFERENCES asset_preparations(preparation_id) ON DELETE CASCADE,
            artifact_sha256 CHAR(64) NOT NULL
                REFERENCES nzb_artifacts(artifact_sha256) ON DELETE RESTRICT,
            created_at DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (preparation_id, artifact_sha256)
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_asset_preparation_artifacts_artifact_v1
            ON {table_name} (artifact_sha256, preparation_id)
        """,
    ),
)

MEDIA_DEMAND_TABLE_SPEC = ManagedTableSpec(
    table_name="media_demand",
    create_sql="""
        CREATE TABLE {table_name} (
            media_id TEXT PRIMARY KEY,
            first_seen_at DOUBLE PRECISION NOT NULL,
            last_seen_at DOUBLE PRECISION NOT NULL,
            last_scraped_at DOUBLE PRECISION
        )
    """,
    legacy_columns=(
        LegacyColumnMigration(
            column_name="last_scraped_at",
            column_sql="last_scraped_at DOUBLE PRECISION",
        ),
    ),
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_media_demand_last_seen_v1
            ON {table_name} (last_seen_at)
        """,
    ),
)

DEBRID_ACCOUNT_TRACKER_PREDICATE = "substr(tracker, 1, 14) = 'DebridAccount|'"


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
            updated_at DOUBLE PRECISION NOT NULL,
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
            column_sql="updated_at DOUBLE PRECISION",
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
        f"""
            CREATE INDEX IF NOT EXISTS idx_torrents_debrid_account_media_v1
            ON {{table_name}} (media_id, info_hash)
            WHERE {DEBRID_ACCOUNT_TRACKER_PREDICATE}
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
            updated_at DOUBLE PRECISION NOT NULL,
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
            column_sql="updated_at DOUBLE PRECISION",
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
            selection_key TEXT NOT NULL DEFAULT '',
            client_scope TEXT NOT NULL DEFAULT '',
            download_url TEXT NOT NULL,
            updated_at DOUBLE PRECISION NOT NULL,
            CHECK ((season IS NULL AND season_norm = -1) OR season = season_norm),
            CHECK ((episode IS NULL AND episode_norm = -1) OR episode = episode_norm)
        )
    """,
    legacy_columns=(
        LegacyColumnMigration(
            "selection_key", "selection_key TEXT NOT NULL DEFAULT ''"
        ),
        LegacyColumnMigration("client_scope", "client_scope TEXT NOT NULL DEFAULT ''"),
    ),
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_download_links_updated_at_v1
            ON {table_name} (updated_at)
        """,
        """
            CREATE UNIQUE INDEX IF NOT EXISTS unq_download_links_scope_v4
            ON {table_name} (
                debrid_service,
                account_key_hash,
                info_hash,
                season_norm,
                episode_norm,
                selection_key,
                client_scope
            )
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
            added_at DOUBLE PRECISION NOT NULL,
            synced_at DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (debrid_service, account_key_hash, magnet_id)
        )
    """,
    legacy_columns=(
        LegacyColumnMigration(
            column_name="synced_at",
            column_sql="synced_at DOUBLE PRECISION",
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
            last_sync_at DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (debrid_service, account_key_hash)
        )
    """,
    legacy_columns=(
        LegacyColumnMigration(
            column_name="last_sync_at",
            column_sql="last_sync_at DOUBLE PRECISION",
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
            started_at DOUBLE PRECISION NOT NULL
        )
    """,
    legacy_columns=(
        LegacyColumnMigration(
            column_name="started_at",
            column_sql="started_at DOUBLE PRECISION",
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


PROXY_ACTIVE_CONNECTIONS_TABLE_SPEC = ManagedTableSpec(
    table_name="active_connections",
    create_sql="""
        CREATE TABLE {table_name} (
            id TEXT PRIMARY KEY,
            ip TEXT NOT NULL,
            content TEXT NOT NULL,
            service TEXT NOT NULL DEFAULT 'unknown',
            instance_id TEXT NOT NULL DEFAULT '',
            process_id INTEGER NOT NULL DEFAULT 0,
            started_at DOUBLE PRECISION NOT NULL,
            updated_at DOUBLE PRECISION NOT NULL DEFAULT 0,
            bytes_transferred BIGINT NOT NULL DEFAULT 0,
            current_speed DOUBLE PRECISION NOT NULL DEFAULT 0,
            peak_speed DOUBLE PRECISION NOT NULL DEFAULT 0,
            cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (
                cancel_requested IN (0, 1)
            )
        )
    """,
    legacy_columns=(
        LegacyColumnMigration(
            column_name="service",
            column_sql="service TEXT NOT NULL DEFAULT 'unknown'",
        ),
        LegacyColumnMigration(
            column_name="instance_id",
            column_sql="instance_id TEXT NOT NULL DEFAULT ''",
        ),
        LegacyColumnMigration(
            column_name="process_id",
            column_sql="process_id INTEGER NOT NULL DEFAULT 0",
        ),
        LegacyColumnMigration(
            column_name="updated_at",
            column_sql="updated_at DOUBLE PRECISION NOT NULL DEFAULT 0",
        ),
        LegacyColumnMigration(
            column_name="bytes_transferred",
            column_sql="bytes_transferred BIGINT NOT NULL DEFAULT 0",
        ),
        LegacyColumnMigration(
            column_name="current_speed",
            column_sql="current_speed DOUBLE PRECISION NOT NULL DEFAULT 0",
        ),
        LegacyColumnMigration(
            column_name="peak_speed",
            column_sql="peak_speed DOUBLE PRECISION NOT NULL DEFAULT 0",
        ),
        LegacyColumnMigration(
            column_name="cancel_requested",
            column_sql=(
                "cancel_requested INTEGER NOT NULL DEFAULT 0 "
                "CHECK (cancel_requested IN (0, 1))"
            ),
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
        """
            CREATE INDEX IF NOT EXISTS idx_connections_owner_v1
            ON {table_name} (instance_id, process_id, cancel_requested)
        """,
    ),
)


PROXY_CONNECTION_HISTORY_TABLE_SPEC = ManagedTableSpec(
    table_name="proxy_connection_history",
    create_sql="""
        CREATE TABLE {table_name} (
            id TEXT PRIMARY KEY,
            ip TEXT NOT NULL,
            content TEXT NOT NULL,
            service TEXT NOT NULL,
            instance_id TEXT NOT NULL,
            process_id INTEGER NOT NULL,
            started_at DOUBLE PRECISION NOT NULL,
            finished_at DOUBLE PRECISION NOT NULL,
            duration DOUBLE PRECISION NOT NULL,
            bytes_transferred BIGINT NOT NULL,
            average_speed DOUBLE PRECISION NOT NULL,
            peak_speed DOUBLE PRECISION NOT NULL,
            outcome TEXT NOT NULL,
            error_code TEXT
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_proxy_history_finished_v1
            ON {table_name} (finished_at DESC, id)
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_proxy_history_outcome_v1
            ON {table_name} (outcome, finished_at DESC)
        """,
    ),
)


PROXY_TRAFFIC_SAMPLES_TABLE_SPEC = ManagedTableSpec(
    table_name="proxy_traffic_samples",
    create_sql="""
        CREATE TABLE {table_name} (
            instance_id TEXT NOT NULL,
            process_id INTEGER NOT NULL,
            sampled_at DOUBLE PRECISION NOT NULL,
            active_connections INTEGER NOT NULL,
            current_speed DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (instance_id, process_id, sampled_at)
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_proxy_samples_time_v1
            ON {table_name} (sampled_at)
        """,
    ),
)


OPERATOR_COMMANDS_TABLE_SPEC = ManagedTableSpec(
    table_name="operator_commands",
    create_sql="""
        CREATE TABLE {table_name} (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            resource_id TEXT,
            target_instance_id TEXT NOT NULL,
            target_process_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            created_at DOUBLE PRECISION NOT NULL,
            acknowledged_at DOUBLE PRECISION,
            finished_at DOUBLE PRECISION,
            outcome TEXT,
            error_code TEXT
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_operator_commands_target_v1
            ON {table_name} (
                target_instance_id, target_process_id, status, created_at
            )
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_operator_commands_finished_v1
            ON {table_name} (finished_at)
        """,
    ),
)


BACKGROUND_SCRAPER_RUNTIMES_TABLE_SPEC = ManagedTableSpec(
    table_name="background_scraper_runtimes",
    create_sql="""
        CREATE TABLE {table_name} (
            instance_id TEXT NOT NULL,
            process_id INTEGER NOT NULL,
            state TEXT NOT NULL,
            draining INTEGER NOT NULL DEFAULT 0 CHECK (draining IN (0, 1)),
            run_id TEXT,
            started_at DOUBLE PRECISION,
            processed INTEGER NOT NULL DEFAULT 0,
            success INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0,
            torrents_found INTEGER NOT NULL DEFAULT 0,
            discovered_items INTEGER NOT NULL DEFAULT 0,
            errors INTEGER NOT NULL DEFAULT 0,
            last_heartbeat DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (instance_id, process_id)
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_background_runtimes_heartbeat_v1
            ON {table_name} (last_heartbeat DESC)
        """,
    ),
)

USENET_ENGINE_RUNTIMES_TABLE_SPEC = ManagedTableSpec(
    table_name="usenet_engine_runtimes",
    create_sql="""
        CREATE TABLE {table_name} (
            instance_id TEXT PRIMARY KEY,
            process_id INTEGER NOT NULL,
            healthy INTEGER NOT NULL CHECK (healthy IN (0, 1)),
            mode TEXT NOT NULL,
            stats_json TEXT,
            collected_at DOUBLE PRECISION NOT NULL
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_usenet_engine_runtimes_collected_v1
            ON {table_name} (collected_at DESC)
        """,
    ),
)


USENET_ACTIVE_OPERATIONS_TABLE_SPEC = ManagedTableSpec(
    table_name="usenet_active_operations",
    create_sql="""
        CREATE TABLE {table_name} (
            id TEXT PRIMARY KEY,
            instance_id TEXT NOT NULL,
            process_id INTEGER NOT NULL,
            client_ip TEXT NOT NULL,
            content_id TEXT NOT NULL,
            title TEXT NOT NULL,
            member_path TEXT NOT NULL,
            source_kind TEXT NOT NULL CHECK (
                source_kind IN ('session', 'raw_composite')
            ),
            started_at DOUBLE PRECISION NOT NULL,
            updated_at DOUBLE PRECISION NOT NULL,
            total_bytes BIGINT NOT NULL,
            bytes_transferred BIGINT NOT NULL DEFAULT 0,
            cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (
                cancel_requested IN (0, 1)
            )
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_usenet_active_owner_v1
            ON {table_name} (instance_id, process_id, cancel_requested)
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_usenet_active_started_v1
            ON {table_name} (started_at DESC)
        """,
    ),
)


USENET_OPERATION_HISTORY_TABLE_SPEC = ManagedTableSpec(
    table_name="usenet_operation_history",
    create_sql="""
        CREATE TABLE {table_name} (
            id TEXT PRIMARY KEY,
            instance_id TEXT NOT NULL,
            process_id INTEGER NOT NULL,
            client_ip TEXT NOT NULL,
            content_id TEXT NOT NULL,
            title TEXT NOT NULL,
            member_path TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            started_at DOUBLE PRECISION NOT NULL,
            finished_at DOUBLE PRECISION NOT NULL,
            duration DOUBLE PRECISION NOT NULL,
            total_bytes BIGINT NOT NULL,
            bytes_transferred BIGINT NOT NULL,
            outcome TEXT NOT NULL,
            error_code TEXT
        )
    """,
    index_sql=(
        """
            CREATE INDEX IF NOT EXISTS idx_usenet_history_finished_v1
            ON {table_name} (finished_at DESC, id)
        """,
        """
            CREATE INDEX IF NOT EXISTS idx_usenet_history_outcome_v1
            ON {table_name} (outcome, finished_at DESC)
        """,
    ),
)

BANDWIDTH_STATS_TABLE_SPEC = ManagedTableSpec(
    table_name="bandwidth_stats",
    create_sql="""
        CREATE TABLE {table_name} (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            total_bytes BIGINT NOT NULL,
            updated_at DOUBLE PRECISION
        )
    """,
    legacy_columns=(
        LegacyColumnMigration(
            column_name="updated_at",
            column_sql="updated_at DOUBLE PRECISION",
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
            priority_score DOUBLE PRECISION NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'discovered',
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            last_scraped_at DOUBLE PRECISION,
            last_success_at DOUBLE PRECISION,
            last_failure_at DOUBLE PRECISION,
            next_retry_at DOUBLE PRECISION,
            total_torrents_found INTEGER NOT NULL DEFAULT 0,
            created_at DOUBLE PRECISION,
            updated_at DOUBLE PRECISION
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
            refreshed_at DOUBLE PRECISION NOT NULL
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
            column_sql="refreshed_at DOUBLE PRECISION",
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
            refreshed_at DOUBLE PRECISION NOT NULL
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
            started_at DOUBLE PRECISION NOT NULL,
            finished_at DOUBLE PRECISION,
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
            last_scraped_at DOUBLE PRECISION,
            last_success_at DOUBLE PRECISION,
            last_failure_at DOUBLE PRECISION,
            next_retry_at DOUBLE PRECISION,
            total_torrents_found INTEGER NOT NULL DEFAULT 0,
            created_at DOUBLE PRECISION,
            updated_at DOUBLE PRECISION,
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
    CAPABILITY_VALIDATION_STATES_TABLE_SPEC,
    SEARCH_COVERAGE_TABLE_SPEC,
    RELEASE_CANDIDATES_TABLE_SPEC,
    CANDIDATE_IDENTITIES_TABLE_SPEC,
    CANDIDATE_REDIRECTS_TABLE_SPEC,
    RELEASE_LOCATORS_TABLE_SPEC,
    SCRAPE_LOCKS_TABLE_SPEC,
    KODI_SETUP_CODES_TABLE_SPEC,
    MEDIA_METADATA_CACHE_TABLE_SPEC,
    IMDB_TITLE_LOOKUP_TABLE_SPEC,
    SERIES_EPISODE_INDEX_TABLE_SPEC,
    SERIES_EPISODE_INDEX_REFRESH_TABLE_SPEC,
    MEDIA_DEMAND_TABLE_SPEC,
    TORRENTS_TABLE_SPEC,
    DEBRID_AVAILABILITY_TABLE_SPEC,
    DOWNLOAD_LINKS_CACHE_TABLE_SPEC,
    DEBRID_ACCOUNT_MAGNETS_TABLE_SPEC,
    ACTIVE_CONNECTIONS_TABLE_SPEC,
    PROXY_CONNECTION_HISTORY_TABLE_SPEC,
    PROXY_TRAFFIC_SAMPLES_TABLE_SPEC,
    OPERATOR_COMMANDS_TABLE_SPEC,
    BACKGROUND_SCRAPER_RUNTIMES_TABLE_SPEC,
    USENET_ENGINE_RUNTIMES_TABLE_SPEC,
    USENET_ACTIVE_OPERATIONS_TABLE_SPEC,
    USENET_OPERATION_HISTORY_TABLE_SPEC,
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
