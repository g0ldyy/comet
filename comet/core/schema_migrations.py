import time
from dataclasses import dataclass

from comet.core.logger import logger

NULL_SCOPE_SENTINEL = -1


@dataclass(slots=True)
class MigrationContext:
    database: object
    is_sqlite: bool
    is_postgres: bool


async def run_schema_migrations(database, *, is_sqlite: bool, is_postgres: bool):
    ctx = MigrationContext(
        database=database,
        is_sqlite=is_sqlite,
        is_postgres=is_postgres,
    )

    await _ensure_schema_migrations_table(ctx)
    applied = await _get_applied_migrations(ctx)

    for version, migration in MIGRATIONS:
        if version in applied:
            continue

        logger.log("DATABASE", f"Applying schema migration {version}")
        await migration(ctx)
        await ctx.database.execute(
            """
            INSERT INTO schema_migrations (version, applied_at)
            VALUES (:version, :applied_at)
            """,
            {"version": version, "applied_at": time.time()},
            force_primary=True,
        )
        logger.log("DATABASE", f"Applied schema migration {version}")


async def _ensure_schema_migrations_table(ctx: MigrationContext):
    await ctx.database.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at REAL NOT NULL
        )
        """
    )


async def _get_applied_migrations(ctx: MigrationContext) -> set[str]:
    rows = await ctx.database.fetch_all(
        "SELECT version FROM schema_migrations",
        force_primary=True,
    )
    return {row["version"] for row in rows}


async def _table_exists(ctx: MigrationContext, table_name: str) -> bool:
    if ctx.is_sqlite:
        row = await ctx.database.fetch_one(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = :table_name
            """,
            {"table_name": table_name},
            force_primary=True,
        )
    else:
        row = await ctx.database.fetch_one(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = current_schema()
              AND table_name = :table_name
            """,
            {"table_name": table_name},
            force_primary=True,
        )
    return row is not None


async def _column_exists(
    ctx: MigrationContext, table_name: str, column_name: str
) -> bool:
    if not await _table_exists(ctx, table_name):
        return False

    if ctx.is_sqlite:
        rows = await ctx.database.fetch_all(
            f"PRAGMA table_info({table_name})",
            force_primary=True,
        )
        return any(row["name"] == column_name for row in rows)

    row = await ctx.database.fetch_one(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = :table_name
          AND column_name = :column_name
        """,
        {"table_name": table_name, "column_name": column_name},
        force_primary=True,
    )
    return row is not None


async def _index_exists(ctx: MigrationContext, index_name: str) -> bool:
    if ctx.is_sqlite:
        row = await ctx.database.fetch_one(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'index' AND name = :index_name
            """,
            {"index_name": index_name},
            force_primary=True,
        )
    else:
        row = await ctx.database.fetch_one(
            """
            SELECT 1
            FROM pg_indexes
            WHERE schemaname = current_schema()
              AND indexname = :index_name
            """,
            {"index_name": index_name},
            force_primary=True,
        )
    return row is not None


async def _add_column_if_missing(
    ctx: MigrationContext,
    table_name: str,
    column_name: str,
    column_sql: str,
):
    if await _column_exists(ctx, table_name, column_name):
        return

    if ctx.is_postgres:
        await ctx.database.execute(
            f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {column_sql}"
        )
        return

    try:
        await ctx.database.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_sql}")
    except Exception as exc:
        if "duplicate column name" not in str(exc).lower():
            raise


async def _drop_index_if_exists(ctx: MigrationContext, index_name: str):
    if not await _index_exists(ctx, index_name):
        return
    await ctx.database.execute(f"DROP INDEX IF EXISTS {index_name}")


async def _ensure_index(ctx: MigrationContext, index_sql: str):
    await ctx.database.execute(index_sql)


async def _ensure_table(ctx: MigrationContext, table_name: str, create_sql: str):
    if await _table_exists(ctx, table_name):
        return
    await ctx.database.execute(create_sql)


async def _drop_table_if_exists(ctx: MigrationContext, table_name: str):
    if not await _table_exists(ctx, table_name):
        return False

    await ctx.database.execute(f"DROP TABLE IF EXISTS {table_name}")
    return True


async def _drop_column_if_exists(
    ctx: MigrationContext,
    table_name: str,
    column_name: str,
):
    if not await _column_exists(ctx, table_name, column_name):
        return False

    await ctx.database.execute(f"ALTER TABLE {table_name} DROP COLUMN {column_name}")
    return True


async def _migration_foundation(ctx: MigrationContext):
    await _ensure_table(
        ctx,
        "db_maintenance",
        """
        CREATE TABLE db_maintenance (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_startup_cleanup_at REAL
        )
        """,
    )
    await _add_column_if_missing(
        ctx,
        "db_maintenance",
        "last_startup_cleanup_at",
        "last_startup_cleanup_at REAL",
    )
    if await _column_exists(ctx, "db_maintenance", "last_startup_cleanup"):
        await ctx.database.execute(
            """
            UPDATE db_maintenance
            SET last_startup_cleanup_at = COALESCE(
                last_startup_cleanup_at,
                last_startup_cleanup
            )
            """
        )

    await _ensure_table(
        ctx,
        "scrape_locks",
        """
        CREATE TABLE scrape_locks (
            lock_key TEXT PRIMARY KEY,
            instance_id TEXT NOT NULL,
            updated_at REAL NOT NULL,
            expires_at REAL NOT NULL
        )
        """,
    )
    await _add_column_if_missing(
        ctx,
        "scrape_locks",
        "updated_at",
        "updated_at REAL",
    )
    if await _column_exists(ctx, "scrape_locks", "timestamp"):
        await ctx.database.execute(
            """
            UPDATE scrape_locks
            SET updated_at = COALESCE(updated_at, timestamp, expires_at)
            """
        )

    await _ensure_table(
        ctx,
        "kodi_setup_codes",
        """
        CREATE TABLE kodi_setup_codes (
            code TEXT PRIMARY KEY,
            nonce TEXT NOT NULL,
            config_b64 TEXT,
            expires_at REAL NOT NULL,
            consumed_at REAL
        )
        """,
    )
    await _add_column_if_missing(
        ctx,
        "kodi_setup_codes",
        "config_b64",
        "config_b64 TEXT",
    )
    if await _column_exists(ctx, "kodi_setup_codes", "b64config"):
        await ctx.database.execute(
            """
            UPDATE kodi_setup_codes
            SET config_b64 = COALESCE(config_b64, b64config)
            """
        )

    await _ensure_table(
        ctx,
        "media_metadata_cache",
        """
        CREATE TABLE media_metadata_cache (
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
    )

    await _ensure_table(
        ctx,
        "media_demand",
        """
        CREATE TABLE media_demand (
            media_id TEXT PRIMARY KEY,
            first_seen_at REAL NOT NULL,
            last_seen_at REAL NOT NULL
        )
        """,
    )

    await _ensure_table(
        ctx,
        "torrents",
        """
        CREATE TABLE torrents (
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
    )
    await _add_column_if_missing(
        ctx,
        "torrents",
        "season_norm",
        "season_norm INTEGER NOT NULL DEFAULT -1",
    )
    await _add_column_if_missing(
        ctx,
        "torrents",
        "episode_norm",
        "episode_norm INTEGER NOT NULL DEFAULT -1",
    )
    await _add_column_if_missing(
        ctx,
        "torrents",
        "sources_json",
        "sources_json TEXT",
    )
    await _add_column_if_missing(
        ctx,
        "torrents",
        "parsed_json",
        "parsed_json TEXT",
    )
    await _add_column_if_missing(
        ctx,
        "torrents",
        "updated_at",
        "updated_at REAL",
    )
    if await _column_exists(ctx, "torrents", "sources"):
        await ctx.database.execute(
            """
            UPDATE torrents
            SET sources_json = COALESCE(sources_json, sources, '[]')
            """
        )
    else:
        await ctx.database.execute(
            """
            UPDATE torrents
            SET sources_json = COALESCE(sources_json, '[]')
            """
        )
    if await _column_exists(ctx, "torrents", "parsed"):
        await ctx.database.execute(
            """
            UPDATE torrents
            SET parsed_json = COALESCE(parsed_json, parsed)
            """
        )
    if await _column_exists(ctx, "torrents", "timestamp"):
        await ctx.database.execute(
            """
            UPDATE torrents
            SET season_norm = COALESCE(season, :null_sentinel),
                episode_norm = COALESCE(episode, :null_sentinel),
                updated_at = COALESCE(updated_at, timestamp)
            """,
            {"null_sentinel": NULL_SCOPE_SENTINEL},
        )
    else:
        await ctx.database.execute(
            """
            UPDATE torrents
            SET season_norm = COALESCE(season, :null_sentinel),
                episode_norm = COALESCE(episode, :null_sentinel)
            """,
            {"null_sentinel": NULL_SCOPE_SENTINEL},
        )

    await _ensure_table(
        ctx,
        "debrid_availability",
        """
        CREATE TABLE debrid_availability (
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
    )
    await _add_column_if_missing(
        ctx,
        "debrid_availability",
        "season_norm",
        "season_norm INTEGER NOT NULL DEFAULT -1",
    )
    await _add_column_if_missing(
        ctx,
        "debrid_availability",
        "episode_norm",
        "episode_norm INTEGER NOT NULL DEFAULT -1",
    )
    await _add_column_if_missing(
        ctx,
        "debrid_availability",
        "parsed_json",
        "parsed_json TEXT",
    )
    await _add_column_if_missing(
        ctx,
        "debrid_availability",
        "updated_at",
        "updated_at REAL",
    )
    if await _column_exists(ctx, "debrid_availability", "parsed"):
        await ctx.database.execute(
            """
            UPDATE debrid_availability
            SET parsed_json = COALESCE(parsed_json, parsed)
            """
        )
    if await _column_exists(ctx, "debrid_availability", "timestamp"):
        await ctx.database.execute(
            """
            UPDATE debrid_availability
            SET season_norm = COALESCE(season, :null_sentinel),
                episode_norm = COALESCE(episode, :null_sentinel),
                updated_at = COALESCE(updated_at, timestamp)
            """,
            {"null_sentinel": NULL_SCOPE_SENTINEL},
        )
    else:
        await ctx.database.execute(
            """
            UPDATE debrid_availability
            SET season_norm = COALESCE(season, :null_sentinel),
                episode_norm = COALESCE(episode, :null_sentinel)
            """,
            {"null_sentinel": NULL_SCOPE_SENTINEL},
        )

    await _ensure_table(
        ctx,
        "download_links_cache",
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
            updated_at REAL NOT NULL,
            CHECK ((season IS NULL AND season_norm = -1) OR season = season_norm),
            CHECK ((episode IS NULL AND episode_norm = -1) OR episode = episode_norm)
        )
        """,
    )
    await _add_column_if_missing(
        ctx,
        "download_links_cache",
        "debrid_service",
        "debrid_service TEXT",
    )
    await _add_column_if_missing(
        ctx,
        "download_links_cache",
        "account_key_hash",
        "account_key_hash TEXT",
    )
    await _add_column_if_missing(
        ctx,
        "download_links_cache",
        "season_norm",
        "season_norm INTEGER NOT NULL DEFAULT -1",
    )
    await _add_column_if_missing(
        ctx,
        "download_links_cache",
        "episode_norm",
        "episode_norm INTEGER NOT NULL DEFAULT -1",
    )
    await _add_column_if_missing(
        ctx,
        "download_links_cache",
        "updated_at",
        "updated_at REAL",
    )
    if await _table_exists(ctx, "download_links_cache"):
        await ctx.database.execute("DELETE FROM download_links_cache")

    await _ensure_table(
        ctx,
        "debrid_account_magnets",
        """
        CREATE TABLE debrid_account_magnets (
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
    )
    await _add_column_if_missing(
        ctx,
        "debrid_account_magnets",
        "synced_at",
        "synced_at REAL",
    )
    if await _column_exists(ctx, "debrid_account_magnets", "timestamp"):
        await ctx.database.execute(
            """
            UPDATE debrid_account_magnets
            SET synced_at = COALESCE(synced_at, timestamp)
            """
        )

    await _ensure_table(
        ctx,
        "debrid_account_sync_state",
        """
        CREATE TABLE debrid_account_sync_state (
            debrid_service TEXT NOT NULL,
            account_key_hash TEXT NOT NULL,
            last_sync_at REAL NOT NULL,
            PRIMARY KEY (debrid_service, account_key_hash)
        )
        """,
    )
    await _add_column_if_missing(
        ctx,
        "debrid_account_sync_state",
        "last_sync_at",
        "last_sync_at REAL",
    )
    if await _column_exists(ctx, "debrid_account_sync_state", "last_sync"):
        await ctx.database.execute(
            """
            UPDATE debrid_account_sync_state
            SET last_sync_at = COALESCE(last_sync_at, last_sync)
            """
        )

    await _ensure_table(
        ctx,
        "active_connections",
        """
        CREATE TABLE active_connections (
            id TEXT PRIMARY KEY,
            ip TEXT NOT NULL,
            content TEXT NOT NULL,
            started_at REAL NOT NULL
        )
        """,
    )
    await _add_column_if_missing(
        ctx,
        "active_connections",
        "started_at",
        "started_at REAL",
    )
    if await _column_exists(ctx, "active_connections", "timestamp"):
        await ctx.database.execute(
            """
            UPDATE active_connections
            SET started_at = COALESCE(started_at, timestamp)
            """
        )

    await _ensure_table(
        ctx,
        "bandwidth_stats",
        """
        CREATE TABLE bandwidth_stats (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            total_bytes BIGINT NOT NULL,
            updated_at REAL
        )
        """,
    )
    await _add_column_if_missing(
        ctx,
        "bandwidth_stats",
        "updated_at",
        "updated_at REAL",
    )
    if await _column_exists(ctx, "bandwidth_stats", "last_updated"):
        await ctx.database.execute(
            """
            UPDATE bandwidth_stats
            SET updated_at = COALESCE(updated_at, last_updated)
            """
        )

    await _ensure_table(
        ctx,
        "background_scraper_items",
        """
        CREATE TABLE background_scraper_items (
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
    )

    await _ensure_table(
        ctx,
        "metrics_cache",
        """
        CREATE TABLE metrics_cache (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            payload_json TEXT NOT NULL,
            refreshed_at REAL NOT NULL
        )
        """,
    )
    await _add_column_if_missing(
        ctx,
        "metrics_cache",
        "payload_json",
        "payload_json TEXT",
    )
    await _add_column_if_missing(
        ctx,
        "metrics_cache",
        "refreshed_at",
        "refreshed_at REAL",
    )
    if await _column_exists(ctx, "metrics_cache", "data"):
        await ctx.database.execute(
            """
            UPDATE metrics_cache
            SET payload_json = COALESCE(payload_json, data)
            """
        )
    if await _column_exists(ctx, "metrics_cache", "timestamp"):
        await ctx.database.execute(
            """
            UPDATE metrics_cache
            SET refreshed_at = COALESCE(refreshed_at, timestamp)
            """
        )

    await _ensure_table(
        ctx,
        "anime_entries",
        """
        CREATE TABLE anime_entries (
            id INTEGER PRIMARY KEY,
            data_json TEXT NOT NULL
        )
        """,
    )
    await _add_column_if_missing(
        ctx,
        "anime_entries",
        "data_json",
        "data_json TEXT",
    )
    if await _column_exists(ctx, "anime_entries", "data"):
        await ctx.database.execute(
            """
            UPDATE anime_entries
            SET data_json = COALESCE(data_json, data)
            """
        )

    await _ensure_table(
        ctx,
        "anime_mapping_state",
        """
        CREATE TABLE anime_mapping_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            refreshed_at REAL NOT NULL
        )
        """,
    )

    await _ensure_table(
        ctx,
        "anime_provider_overrides",
        """
        CREATE TABLE anime_provider_overrides (
            source_provider TEXT NOT NULL,
            source_id TEXT NOT NULL,
            target_provider TEXT NOT NULL,
            target_id TEXT NOT NULL,
            from_season INTEGER,
            from_episode INTEGER,
            PRIMARY KEY (source_provider, source_id, target_provider)
        )
        """,
    )

    await _ensure_table(
        ctx,
        "dmm_entries",
        """
        CREATE TABLE dmm_entries (
            info_hash TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            size BIGINT,
            parsed_title TEXT,
            parsed_year INTEGER
        )
        """,
    )
    await _ensure_table(
        ctx,
        "dmm_ingested_files",
        """
        CREATE TABLE dmm_ingested_files (
            filename TEXT PRIMARY KEY
        )
        """,
    )


async def _migration_backfill_canonical_tables(ctx: MigrationContext):
    if await _table_exists(ctx, "metadata_cache"):
        rows = await ctx.database.fetch_all(
            """
            SELECT media_id, title, year, year_end, aliases, timestamp
            FROM metadata_cache
            """,
            force_primary=True,
        )
        if rows:
            await ctx.database.execute_many(
                """
                INSERT INTO media_metadata_cache (
                    media_id,
                    title,
                    year,
                    year_end,
                    aliases_json,
                    metadata_updated_at
                )
                VALUES (
                    :media_id,
                    :title,
                    :year,
                    :year_end,
                    :aliases_json,
                    :metadata_updated_at
                )
                ON CONFLICT (media_id) DO UPDATE SET
                    title = EXCLUDED.title,
                    year = EXCLUDED.year,
                    year_end = EXCLUDED.year_end,
                    aliases_json = EXCLUDED.aliases_json,
                    metadata_updated_at = EXCLUDED.metadata_updated_at
                """,
                [
                    {
                        "media_id": row["media_id"],
                        "title": row["title"],
                        "year": row["year"],
                        "year_end": row["year_end"],
                        "aliases_json": row["aliases"],
                        "metadata_updated_at": row["timestamp"],
                    }
                    for row in rows
                ],
            )

    if await _table_exists(ctx, "digital_release_cache"):
        rows = await ctx.database.fetch_all(
            """
            SELECT media_id, release_date, timestamp
            FROM digital_release_cache
            """,
            force_primary=True,
        )
        if rows:
            await ctx.database.execute_many(
                """
                INSERT INTO media_metadata_cache (
                    media_id,
                    release_date,
                    release_updated_at
                )
                VALUES (:media_id, :release_date, :release_updated_at)
                ON CONFLICT (media_id) DO UPDATE SET
                    release_date = EXCLUDED.release_date,
                    release_updated_at = EXCLUDED.release_updated_at
                """,
                [
                    {
                        "media_id": row["media_id"],
                        "release_date": row["release_date"],
                        "release_updated_at": row["timestamp"],
                    }
                    for row in rows
                ],
            )

    if await _table_exists(ctx, "first_searches"):
        rows = await ctx.database.fetch_all(
            """
            SELECT media_id, timestamp
            FROM first_searches
            """,
            force_primary=True,
        )
        if rows:
            await ctx.database.execute_many(
                """
                INSERT INTO media_demand (
                    media_id,
                    first_seen_at,
                    last_seen_at
                )
                VALUES (:media_id, :first_seen_at, :last_seen_at)
                ON CONFLICT (media_id) DO UPDATE SET
                    first_seen_at = CASE
                        WHEN media_demand.first_seen_at IS NULL
                          OR media_demand.first_seen_at > EXCLUDED.first_seen_at
                        THEN EXCLUDED.first_seen_at
                        ELSE media_demand.first_seen_at
                    END,
                    last_seen_at = CASE
                        WHEN media_demand.last_seen_at < EXCLUDED.last_seen_at
                        THEN EXCLUDED.last_seen_at
                        ELSE media_demand.last_seen_at
                    END
                """,
                [
                    {
                        "media_id": row["media_id"],
                        "first_seen_at": row["timestamp"],
                        "last_seen_at": row["timestamp"],
                    }
                    for row in rows
                ],
            )

    if await _table_exists(ctx, "kitsu_imdb_mapping"):
        rows = await ctx.database.fetch_all(
            """
            SELECT kitsu_id, imdb_id, from_season, from_episode
            FROM kitsu_imdb_mapping
            WHERE imdb_id IS NOT NULL
            """,
            force_primary=True,
        )
        if rows:
            await ctx.database.execute_many(
                """
                INSERT INTO anime_provider_overrides (
                    source_provider,
                    source_id,
                    target_provider,
                    target_id,
                    from_season,
                    from_episode
                )
                VALUES (
                    'kitsu',
                    :source_id,
                    'imdb',
                    :target_id,
                    :from_season,
                    :from_episode
                )
                ON CONFLICT (source_provider, source_id, target_provider) DO UPDATE SET
                    target_id = EXCLUDED.target_id,
                    from_season = EXCLUDED.from_season,
                    from_episode = EXCLUDED.from_episode
                """,
                [
                    {
                        "source_id": row["kitsu_id"],
                        "target_id": row["imdb_id"],
                        "from_season": row["from_season"],
                        "from_episode": row["from_episode"],
                    }
                    for row in rows
                ],
            )


async def _replace_table(
    ctx: MigrationContext,
    table_name: str,
    create_sql: str,
    copy_sql: str,
    index_sql: list[str] | None = None,
):
    temp_name = f"{table_name}__new"
    await ctx.database.execute(f"DROP TABLE IF EXISTS {temp_name}")

    async with ctx.database.transaction():
        await ctx.database.execute(create_sql.format(table_name=temp_name))
        await ctx.database.execute(copy_sql.format(table_name=temp_name))
        await ctx.database.execute(f"DROP TABLE {table_name}")
        await ctx.database.execute(f"ALTER TABLE {temp_name} RENAME TO {table_name}")
        for statement in index_sql or []:
            await ctx.database.execute(statement)


async def _migration_integrity_rollout(ctx: MigrationContext):
    await _ensure_background_scraper_runs_table(ctx)
    await _ensure_anime_ids_table(ctx)
    await _ensure_background_scraper_episodes_table(ctx)


async def _ensure_background_scraper_runs_table(ctx: MigrationContext):
    if not await _table_exists(ctx, "background_scraper_runs"):
        await ctx.database.execute(
            """
            CREATE TABLE background_scraper_runs (
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
            """
        )
        return

    await _add_column_if_missing(
        ctx,
        "background_scraper_runs",
        "processed_count",
        "processed_count INTEGER NOT NULL DEFAULT 0",
    )
    await _add_column_if_missing(
        ctx,
        "background_scraper_runs",
        "success_count",
        "success_count INTEGER NOT NULL DEFAULT 0",
    )
    await _add_column_if_missing(
        ctx,
        "background_scraper_runs",
        "failed_count",
        "failed_count INTEGER NOT NULL DEFAULT 0",
    )
    await _add_column_if_missing(
        ctx,
        "background_scraper_runs",
        "torrents_found_count",
        "torrents_found_count INTEGER NOT NULL DEFAULT 0",
    )

    if await _column_exists(ctx, "background_scraper_runs", "processed"):
        await ctx.database.execute(
            """
            UPDATE background_scraper_runs
            SET processed_count = COALESCE(processed, processed_count, 0),
                success_count = COALESCE(success, success_count, 0),
                failed_count = COALESCE(failed, failed_count, 0),
                torrents_found_count = COALESCE(torrents_found, torrents_found_count, 0)
            """
        )

    needs_rebuild = await _column_exists(
        ctx, "background_scraper_runs", "config_snapshot"
    )
    if not needs_rebuild:
        return

    await _replace_table(
        ctx,
        "background_scraper_runs",
        """
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
        """
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
        """,
        index_sql=[
            """
            CREATE INDEX IF NOT EXISTS idx_bg_runs_started_v2
            ON background_scraper_runs (started_at DESC)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_bg_runs_status_started_v2
            ON background_scraper_runs (status, started_at DESC)
            """,
        ],
    )


async def _ensure_anime_ids_table(ctx: MigrationContext):
    if not await _table_exists(ctx, "anime_ids"):
        await ctx.database.execute(
            """
            CREATE TABLE anime_ids (
                provider TEXT NOT NULL,
                provider_id TEXT NOT NULL,
                entry_id INTEGER NOT NULL,
                PRIMARY KEY (provider, provider_id),
                FOREIGN KEY (entry_id) REFERENCES anime_entries(id) ON DELETE CASCADE
            )
            """
        )
        await ctx.database.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_anime_ids_entry_provider_v2
            ON anime_ids (entry_id, provider, provider_id)
            """
        )
        return

    await ctx.database.execute(
        """
        DELETE FROM anime_ids
        WHERE entry_id NOT IN (SELECT id FROM anime_entries)
        """
    )

    await _replace_table(
        ctx,
        "anime_ids",
        """
        CREATE TABLE {table_name} (
            provider TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            entry_id INTEGER NOT NULL,
            PRIMARY KEY (provider, provider_id),
            FOREIGN KEY (entry_id) REFERENCES anime_entries(id) ON DELETE CASCADE
        )
        """,
        """
        INSERT INTO {table_name} (provider, provider_id, entry_id)
        SELECT provider, provider_id, entry_id
        FROM anime_ids
        """,
        index_sql=[
            """
            CREATE INDEX IF NOT EXISTS idx_anime_ids_entry_provider_v2
            ON anime_ids (entry_id, provider, provider_id)
            """
        ],
    )


async def _ensure_background_scraper_episodes_table(ctx: MigrationContext):
    if not await _table_exists(ctx, "background_scraper_episodes"):
        await ctx.database.execute(
            """
            CREATE TABLE background_scraper_episodes (
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
            """
        )
        await ctx.database.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_bg_episodes_plan_window_v2
            ON background_scraper_episodes
            (series_id, next_retry_at, last_success_at, status, consecutive_failures, season, episode)
            """
        )
        return

    await ctx.database.execute(
        """
        DELETE FROM background_scraper_episodes
        WHERE season IS NULL
           OR episode IS NULL
           OR season < 1
           OR episode < 1
        """
    )
    await ctx.database.execute(
        """
        DELETE FROM background_scraper_episodes
        WHERE series_id NOT IN (SELECT media_id FROM background_scraper_items)
        """
    )
    await ctx.database.execute(
        """
        DELETE FROM background_scraper_episodes
        WHERE episode_media_id IN (
            SELECT episode_media_id
            FROM (
                SELECT
                    episode_media_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY series_id, season, episode
                        ORDER BY COALESCE(updated_at, created_at, 0) DESC, episode_media_id DESC
                    ) AS row_number
                FROM background_scraper_episodes
            )
            WHERE row_number > 1
        )
        """
    )

    await _replace_table(
        ctx,
        "background_scraper_episodes",
        """
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
        """
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
        """,
        index_sql=[
            """
            CREATE INDEX IF NOT EXISTS idx_bg_episodes_plan_window_v2
            ON background_scraper_episodes
            (series_id, next_retry_at, last_success_at, status, consecutive_failures, season, episode)
            """
        ],
    )


async def _migration_indexes(ctx: MigrationContext):
    index_statements = [
        """
        CREATE INDEX IF NOT EXISTS idx_scrape_locks_expires_v2
        ON scrape_locks (expires_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_scrape_locks_instance_updated_v2
        ON scrape_locks (instance_id, updated_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_media_metadata_updated_at_v1
        ON media_metadata_cache (metadata_updated_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_media_metadata_release_updated_at_v1
        ON media_metadata_cache (release_updated_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_media_demand_last_seen_v1
        ON media_demand (last_seen_at)
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS unq_torrents_scope_v3
        ON torrents (media_id, info_hash, season_norm, episode_norm)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_torrents_lookup_v3
        ON torrents (media_id, season, episode, updated_at DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_torrents_info_hash_v3
        ON torrents (info_hash)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_torrents_updated_at_v1
        ON torrents (updated_at)
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS unq_debrid_scope_v3
        ON debrid_availability (debrid_service, info_hash, season_norm, episode_norm)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_debrid_lookup_v3
        ON debrid_availability (debrid_service, info_hash, updated_at DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_debrid_info_hash_v3
        ON debrid_availability (info_hash)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_debrid_scope_lookup_v3
        ON debrid_availability (info_hash, season, episode, updated_at DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_debrid_updated_at_v1
        ON debrid_availability (updated_at)
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS unq_download_links_scope_v3
        ON download_links_cache (
            debrid_service,
            account_key_hash,
            info_hash,
            season_norm,
            episode_norm
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_download_links_lookup_v3
        ON download_links_cache (
            debrid_service,
            account_key_hash,
            info_hash,
            season_norm,
            episode_norm,
            updated_at DESC
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_download_links_updated_at_v1
        ON download_links_cache (updated_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_debrid_account_lookup_v2
        ON debrid_account_magnets (debrid_service, account_key_hash, synced_at, added_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_debrid_account_synced_at_v1
        ON debrid_account_magnets (synced_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_connections_started_at_desc_v2
        ON active_connections (started_at DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_connections_ip_started_at_v2
        ON active_connections (ip, started_at DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_kodi_setup_codes_expires_v2
        ON kodi_setup_codes (expires_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_bg_items_status_v2
        ON background_scraper_items (status, updated_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_bg_items_plan_window_v2
        ON background_scraper_items
        (media_type, next_retry_at, last_success_at, status, consecutive_failures, priority_score DESC, last_scraped_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_bg_runs_started_v2
        ON background_scraper_runs (started_at DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_bg_runs_status_started_v2
        ON background_scraper_runs (status, started_at DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_anime_ids_entry_provider_v2
        ON anime_ids (entry_id, provider, provider_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_anime_overrides_target_v1
        ON anime_provider_overrides (target_provider, target_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_dmm_parsed_year_v2
        ON dmm_entries (parsed_year)
        """,
    ]

    for statement in index_statements:
        await _ensure_index(ctx, statement)


async def _migration_cleanup_legacy_storage(ctx: MigrationContext):
    # Legacy indexes may still reference renamed columns and block DROP COLUMN.
    for index_name in [
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
    ]:
        await _drop_index_if_exists(ctx, index_name)

    dropped_any = False

    for table_name in [
        "db_version",
        "metadata_cache",
        "digital_release_cache",
        "first_searches",
        "kitsu_imdb_mapping",
    ]:
        dropped_any = await _drop_table_if_exists(ctx, table_name) or dropped_any

    for table_name, columns in [
        ("db_maintenance", ["last_startup_cleanup"]),
        ("scrape_locks", ["timestamp"]),
        ("kodi_setup_codes", ["b64config"]),
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
    ]:
        for column_name in columns:
            dropped_any = (
                await _drop_column_if_exists(ctx, table_name, column_name)
                or dropped_any
            )

    if dropped_any and ctx.is_sqlite:
        logger.log(
            "DATABASE",
            "Legacy schema storage cleanup completed. Run VACUUM once to reclaim SQLite file space.",
        )


MIGRATIONS = [
    ("2026030901_foundation", _migration_foundation),
    ("2026030902_backfill_canonical_tables", _migration_backfill_canonical_tables),
    ("2026030903_integrity_rollout", _migration_integrity_rollout),
    ("2026030904_indexes", _migration_indexes),
    ("2026030905_cleanup_legacy_storage", _migration_cleanup_legacy_storage),
]
