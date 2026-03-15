import time
from dataclasses import dataclass

from comet.core.logger import logger
from comet.core.schema_specs import (ACTIVE_CONNECTIONS_TABLE_SPEC,
                                     ANIME_ENTRIES_TABLE_SPEC,
                                     ANIME_IDS_COPY_SQL, ANIME_IDS_TABLE_SPEC,
                                     ANIME_MAPPING_STATE_TABLE_SPEC,
                                     ANIME_PROVIDER_OVERRIDES_TABLE_SPEC,
                                     BACKGROUND_SCRAPER_EPISODES_COPY_SQL,
                                     BACKGROUND_SCRAPER_EPISODES_TABLE_SPEC,
                                     BACKGROUND_SCRAPER_ITEMS_TABLE_SPEC,
                                     BACKGROUND_SCRAPER_RUNS_COPY_SQL,
                                     BACKGROUND_SCRAPER_RUNS_TABLE_SPEC,
                                     BANDWIDTH_STATS_TABLE_SPEC,
                                     CURRENT_NON_UNIQUE_INDEX_SPECS,
                                     DB_MAINTENANCE_TABLE_SPEC,
                                     DEBRID_ACCOUNT_MAGNETS_TABLE_SPEC,
                                     DEBRID_ACCOUNT_SYNC_STATE_TABLE_SPEC,
                                     DEBRID_AVAILABILITY_TABLE_SPEC,
                                     DMM_ENTRIES_TABLE_SPEC,
                                     DMM_INGESTED_FILES_TABLE_SPEC,
                                     DOWNLOAD_LINKS_CACHE_TABLE_SPEC,
                                     KODI_SETUP_CODES_TABLE_SPEC,
                                     LEGACY_INDEX_NAMES,
                                     LEGACY_STORAGE_CLEANUP_MIGRATION,
                                     LEGACY_STORAGE_COLUMN_CLEANUP,
                                     MEDIA_DEMAND_TABLE_SPEC,
                                     MEDIA_METADATA_CACHE_TABLE_SPEC,
                                     METRICS_CACHE_TABLE_SPEC,
                                     NULL_SCOPE_SENTINEL,
                                     SCRAPE_LOCKS_TABLE_SPEC,
                                     TORRENTS_TABLE_SPEC, UNIQUE_INDEX_SPECS,
                                     LegacyColumnMigration, ManagedTableSpec)


@dataclass(slots=True)
class MigrationContext:
    database: object
    is_sqlite: bool
    is_postgres: bool
    sqlite_journal_mode: str | None = None


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
        applied_migration = await migration(ctx)
        await _checkpoint_sqlite(ctx)
        if applied_migration is False:
            logger.log(
                "DATABASE",
                (
                    f"Deferred schema migration {version}; "
                    "a later startup is still required to finish it."
                ),
            )
            break
        await _record_schema_migration(ctx, version)
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


async def _record_schema_migration(ctx: MigrationContext, version: str):
    insert_sql = """
        INSERT INTO schema_migrations (version, applied_at)
        VALUES (:version, :applied_at)
        ON CONFLICT (version) DO NOTHING
    """
    await ctx.database.execute(
        insert_sql,
        {"version": version, "applied_at": time.time()},
        force_primary=True,
    )


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
        sql = f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {column_sql}"
    else:
        sql = f"ALTER TABLE {table_name} ADD COLUMN {column_sql}"
    await ctx.database.execute(sql)


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


def _render_table_sql(sql: str, table_name: str) -> str:
    return sql.format(table_name=table_name)


def _render_index_sql(spec: ManagedTableSpec) -> tuple[str, ...]:
    return tuple(
        _render_table_sql(index_sql, spec.table_name) for index_sql in spec.index_sql
    )


async def _apply_legacy_column_migrations(
    ctx: MigrationContext,
    table_name: str,
    legacy_columns: tuple[LegacyColumnMigration, ...],
):
    for column in legacy_columns:
        if column.legacy_name is not None:
            await _rename_column_if_missing(
                ctx,
                table_name,
                column.legacy_name,
                column.column_name,
            )

        await _add_column_if_missing(
            ctx,
            table_name,
            column.column_name,
            column.column_sql,
        )

        if (
            column.legacy_name is not None
            and column.backfill_expression is not None
            and await _column_exists(ctx, table_name, column.legacy_name)
        ):
            await ctx.database.execute(
                f"""
                UPDATE {table_name}
                SET {column.column_name} = {column.backfill_expression}
                """
            )


async def _ensure_managed_table(
    ctx: MigrationContext,
    spec: ManagedTableSpec,
    *,
    ensure_indexes: bool = True,
) -> bool:
    table_exists = await _table_exists(ctx, spec.table_name)
    await _ensure_table(
        ctx,
        spec.table_name,
        _render_table_sql(spec.create_sql, spec.table_name),
    )
    await _apply_legacy_column_migrations(ctx, spec.table_name, spec.legacy_columns)
    if ensure_indexes:
        for index_sql in spec.index_sql:
            await _ensure_index(ctx, _render_table_sql(index_sql, spec.table_name))
    return table_exists


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


async def _rename_column_if_missing(
    ctx: MigrationContext,
    table_name: str,
    old_name: str,
    new_name: str,
) -> bool:
    if not await _column_exists(ctx, table_name, old_name):
        return False
    if await _column_exists(ctx, table_name, new_name):
        return False

    await ctx.database.execute(
        f"ALTER TABLE {table_name} RENAME COLUMN {old_name} TO {new_name}"
    )
    return True


SQLITE_LARGE_MUTATION_BATCH_SIZE = 25000
SQLITE_LARGE_MUTATION_CHECKPOINT_INTERVAL = 8


def _is_duplicate_data_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "unique constraint failed" in message
        or "duplicate key value violates unique constraint" in message
        or "could not create unique index" in message
    )


async def _get_sqlite_journal_mode(ctx: MigrationContext) -> str | None:
    if not ctx.is_sqlite:
        return None
    if ctx.sqlite_journal_mode is not None:
        return ctx.sqlite_journal_mode

    row = await ctx.database.fetch_one("PRAGMA journal_mode", force_primary=True)
    if not row:
        return None

    row_mapping = dict(row)
    value = row_mapping.get("journal_mode")
    ctx.sqlite_journal_mode = None if value is None else str(value).lower()
    return ctx.sqlite_journal_mode


async def _checkpoint_sqlite(ctx: MigrationContext):
    if not ctx.is_sqlite:
        return
    if await _get_sqlite_journal_mode(ctx) != "wal":
        return
    await ctx.database.execute("PRAGMA wal_checkpoint(TRUNCATE)", force_primary=True)


async def _drop_legacy_indexes(ctx: MigrationContext):
    for index_name in LEGACY_INDEX_NAMES:
        await _drop_index_if_exists(ctx, index_name)


async def _execute_large_table_update(
    ctx: MigrationContext,
    *,
    table_name: str,
    set_clauses: list[str],
    where_clauses: list[str],
    params: dict[str, object] | None = None,
):
    params = {} if params is None else dict(params)
    set_sql = ", ".join(set_clauses)
    where_sql = " OR ".join(where_clauses)

    if not ctx.is_sqlite:
        await ctx.database.execute(
            f"""
            UPDATE {table_name}
            SET {set_sql}
            WHERE {where_sql}
            """,
            params,
        )
        return

    last_rowid = 0
    batches = 0

    while True:
        batch_params = {
            **params,
            "batch_size": SQLITE_LARGE_MUTATION_BATCH_SIZE,
            "last_rowid": last_rowid,
        }
        batch_row = await ctx.database.fetch_one(
            f"""
            SELECT COUNT(*) AS row_count, MAX(rowid) AS max_rowid
            FROM (
                SELECT rowid
                FROM {table_name}
                WHERE rowid > :last_rowid
                  AND ({where_sql})
                ORDER BY rowid
                LIMIT :batch_size
            ) AS migration_batch
            """,
            batch_params,
            force_primary=True,
        )

        if not batch_row or not batch_row["row_count"]:
            break

        await ctx.database.execute(
            f"""
            UPDATE {table_name}
            SET {set_sql}
            WHERE rowid IN (
                SELECT rowid
                FROM {table_name}
                WHERE rowid > :last_rowid
                  AND ({where_sql})
                ORDER BY rowid
                LIMIT :batch_size
            )
            """,
            batch_params,
        )

        last_rowid = int(batch_row["max_rowid"])
        batches += 1
        if batches % SQLITE_LARGE_MUTATION_CHECKPOINT_INTERVAL == 0:
            await _checkpoint_sqlite(ctx)

    await _checkpoint_sqlite(ctx)


async def _ensure_unique_index_with_dedupe(
    ctx: MigrationContext,
    *,
    table_name: str,
    index_name: str,
    index_sql: str,
    partition_columns: tuple[str, ...],
    order_by_sql: str,
):
    if await _index_exists(ctx, index_name):
        return

    try:
        await _ensure_index(ctx, index_sql)
        return
    except Exception as exc:
        if not _is_duplicate_data_error(exc):
            raise

    logger.log(
        "DATABASE",
        f"Detected duplicate rows while building {index_name}; deduplicating {table_name}",
    )
    await _dedupe_scope_rows(ctx, table_name, partition_columns, order_by_sql)
    await _checkpoint_sqlite(ctx)
    await _ensure_index(ctx, index_sql)


async def _backfill_scope_foundation_columns(
    ctx: MigrationContext,
    *,
    table_name: str,
    extra_set_clauses: list[str] | None = None,
    extra_where_clauses: list[str] | None = None,
):
    has_parsed = await _column_exists(ctx, table_name, "parsed")
    has_timestamp = await _column_exists(ctx, table_name, "timestamp")

    set_clauses = list(extra_set_clauses or [])
    set_clauses.extend(
        [
            "season_norm = COALESCE(season, :null_sentinel)",
            "episode_norm = COALESCE(episode, :null_sentinel)",
        ]
    )
    where_clauses = list(extra_where_clauses or [])
    where_clauses.extend(
        [
            "season_norm != COALESCE(season, :null_sentinel)",
            "episode_norm != COALESCE(episode, :null_sentinel)",
        ]
    )

    if has_parsed:
        set_clauses.append("parsed_json = COALESCE(parsed_json, parsed)")
        where_clauses.append("parsed_json IS NULL AND parsed IS NOT NULL")

    if has_timestamp:
        set_clauses.append("updated_at = COALESCE(updated_at, timestamp)")
        where_clauses.append("updated_at IS NULL AND timestamp IS NOT NULL")

    await _execute_large_table_update(
        ctx,
        table_name=table_name,
        set_clauses=set_clauses,
        where_clauses=where_clauses,
        params={"null_sentinel": NULL_SCOPE_SENTINEL},
    )


async def _backfill_torrents_foundation_columns(ctx: MigrationContext):
    has_sources = await _column_exists(ctx, "torrents", "sources")
    await _backfill_scope_foundation_columns(
        ctx,
        table_name="torrents",
        extra_set_clauses=[
            (
                "sources_json = COALESCE(sources_json, sources, '[]')"
                if has_sources
                else "sources_json = COALESCE(sources_json, '[]')"
            )
        ],
        extra_where_clauses=["sources_json IS NULL"],
    )


async def _backfill_debrid_foundation_columns(ctx: MigrationContext):
    await _backfill_scope_foundation_columns(
        ctx,
        table_name="debrid_availability",
    )


async def _dedupe_scope_rows(
    ctx: MigrationContext,
    table_name: str,
    partition_columns: tuple[str, ...],
    order_by_sql: str,
):
    row_identity = "ctid" if ctx.is_postgres else "rowid"
    partition_sql = ", ".join(partition_columns)

    await ctx.database.execute(
        f"""
        DELETE FROM {table_name}
        WHERE {row_identity} IN (
            SELECT {row_identity}
            FROM (
                SELECT
                    {row_identity},
                    ROW_NUMBER() OVER (
                        PARTITION BY {partition_sql}
                        ORDER BY {order_by_sql}
                    ) AS duplicate_rank
                FROM {table_name}
            ) AS ranked_rows
            WHERE duplicate_rank > 1
        )
        """
    )


async def _migration_foundation(ctx: MigrationContext):
    await _drop_legacy_indexes(ctx)

    for spec in (
        DB_MAINTENANCE_TABLE_SPEC,
        SCRAPE_LOCKS_TABLE_SPEC,
        KODI_SETUP_CODES_TABLE_SPEC,
        MEDIA_METADATA_CACHE_TABLE_SPEC,
        MEDIA_DEMAND_TABLE_SPEC,
    ):
        await _ensure_managed_table(ctx, spec, ensure_indexes=False)

    await _ensure_managed_table(ctx, TORRENTS_TABLE_SPEC, ensure_indexes=False)
    await _backfill_torrents_foundation_columns(ctx)

    await _ensure_managed_table(
        ctx, DEBRID_AVAILABILITY_TABLE_SPEC, ensure_indexes=False
    )
    await _backfill_debrid_foundation_columns(ctx)

    if await _table_exists(ctx, "download_links_cache"):
        await ctx.database.execute("DROP TABLE IF EXISTS download_links_cache")
    await _ensure_table(
        ctx,
        DOWNLOAD_LINKS_CACHE_TABLE_SPEC.table_name,
        _render_table_sql(
            DOWNLOAD_LINKS_CACHE_TABLE_SPEC.create_sql,
            DOWNLOAD_LINKS_CACHE_TABLE_SPEC.table_name,
        ),
    )

    for spec in (
        DEBRID_ACCOUNT_MAGNETS_TABLE_SPEC,
        DEBRID_ACCOUNT_SYNC_STATE_TABLE_SPEC,
        ACTIVE_CONNECTIONS_TABLE_SPEC,
        BANDWIDTH_STATS_TABLE_SPEC,
        BACKGROUND_SCRAPER_ITEMS_TABLE_SPEC,
        METRICS_CACHE_TABLE_SPEC,
        ANIME_ENTRIES_TABLE_SPEC,
        ANIME_MAPPING_STATE_TABLE_SPEC,
        ANIME_PROVIDER_OVERRIDES_TABLE_SPEC,
        DMM_ENTRIES_TABLE_SPEC,
        DMM_INGESTED_FILES_TABLE_SPEC,
    ):
        await _ensure_managed_table(ctx, spec, ensure_indexes=False)


async def _migration_backfill_canonical_tables(ctx: MigrationContext):
    if await _table_exists(ctx, "metadata_cache"):
        aliases_select = (
            "COALESCE(metadata_cache.aliases::text, '[]')"
            if ctx.is_postgres
            else "COALESCE(metadata_cache.aliases, '[]')"
        )
        await ctx.database.execute(
            f"""
            INSERT INTO media_metadata_cache (
                media_id,
                title,
                year,
                year_end,
                aliases_json,
                metadata_updated_at
            )
            SELECT
                media_id,
                title,
                year,
                year_end,
                {aliases_select} AS aliases_json,
                timestamp AS metadata_updated_at
            FROM metadata_cache
            ON CONFLICT (media_id) DO UPDATE SET
                title = EXCLUDED.title,
                year = EXCLUDED.year,
                year_end = EXCLUDED.year_end,
                aliases_json = EXCLUDED.aliases_json,
                metadata_updated_at = EXCLUDED.metadata_updated_at
            """
        )

    if await _table_exists(ctx, "digital_release_cache"):
        await ctx.database.execute(
            """
            INSERT INTO media_metadata_cache (
                media_id,
                release_date,
                release_updated_at
            )
            SELECT
                media_id,
                release_date,
                timestamp AS release_updated_at
            FROM digital_release_cache
            ON CONFLICT (media_id) DO UPDATE SET
                release_date = EXCLUDED.release_date,
                release_updated_at = EXCLUDED.release_updated_at
            """,
        )

    if await _table_exists(ctx, "first_searches"):
        await ctx.database.execute(
            """
            INSERT INTO media_demand (
                media_id,
                first_seen_at,
                last_seen_at
            )
            SELECT
                media_id,
                timestamp AS first_seen_at,
                timestamp AS last_seen_at
            FROM first_searches
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
            """
        )

    if await _table_exists(ctx, "kitsu_imdb_mapping"):
        await ctx.database.execute(
            """
            INSERT INTO anime_provider_overrides (
                source_provider,
                source_id,
                target_provider,
                target_id,
                from_season,
                from_episode
            )
            SELECT
                'kitsu',
                kitsu_id,
                'imdb',
                imdb_id,
                from_season,
                from_episode
            FROM kitsu_imdb_mapping
            WHERE imdb_id IS NOT NULL
            ON CONFLICT (source_provider, source_id, target_provider) DO UPDATE SET
                target_id = EXCLUDED.target_id,
                from_season = EXCLUDED.from_season,
                from_episode = EXCLUDED.from_episode
            """
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


async def _replace_managed_table(
    ctx: MigrationContext,
    spec: ManagedTableSpec,
    copy_sql: str,
):
    await _replace_table(
        ctx,
        spec.table_name,
        spec.create_sql,
        copy_sql,
        index_sql=list(_render_index_sql(spec)),
    )


async def _migration_integrity_rollout(ctx: MigrationContext):
    await _ensure_background_scraper_runs_table(ctx)
    await _ensure_anime_ids_table(ctx)
    await _ensure_background_scraper_episodes_table(ctx)


async def _ensure_background_scraper_runs_table(ctx: MigrationContext):
    if not await _ensure_managed_table(ctx, BACKGROUND_SCRAPER_RUNS_TABLE_SPEC):
        return

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

    await _replace_managed_table(
        ctx,
        BACKGROUND_SCRAPER_RUNS_TABLE_SPEC,
        BACKGROUND_SCRAPER_RUNS_COPY_SQL,
    )


async def _ensure_anime_ids_table(ctx: MigrationContext):
    if not await _ensure_managed_table(ctx, ANIME_IDS_TABLE_SPEC):
        return

    await ctx.database.execute(
        """
        DELETE FROM anime_ids
        WHERE entry_id NOT IN (SELECT id FROM anime_entries)
        """
    )

    await _replace_managed_table(ctx, ANIME_IDS_TABLE_SPEC, ANIME_IDS_COPY_SQL)


async def _ensure_background_scraper_episodes_table(ctx: MigrationContext):
    if not await _ensure_managed_table(ctx, BACKGROUND_SCRAPER_EPISODES_TABLE_SPEC):
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
            ) AS ranked_episodes
            WHERE row_number > 1
        )
        """
    )

    await _replace_managed_table(
        ctx,
        BACKGROUND_SCRAPER_EPISODES_TABLE_SPEC,
        BACKGROUND_SCRAPER_EPISODES_COPY_SQL,
    )


async def _migration_indexes(ctx: MigrationContext):
    await _drop_legacy_indexes(ctx)

    for spec in UNIQUE_INDEX_SPECS:
        await _ensure_unique_index_with_dedupe(
            ctx,
            table_name=spec.table_name,
            index_name=spec.index_name,
            index_sql=spec.index_sql,
            partition_columns=spec.partition_columns,
            order_by_sql=spec.order_by_sql,
        )

    for spec in CURRENT_NON_UNIQUE_INDEX_SPECS:
        for statement in _render_index_sql(spec):
            await _ensure_index(ctx, statement)


async def _cleanup_legacy_storage_columns(ctx: MigrationContext):
    dropped_any = False
    for table_name, columns in LEGACY_STORAGE_COLUMN_CLEANUP:
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


async def _migration_cleanup_legacy_storage(ctx: MigrationContext):
    await _drop_legacy_indexes(ctx)

    for table_name in [
        "db_version",
        "metadata_cache",
        "digital_release_cache",
        "first_searches",
        "kitsu_imdb_mapping",
    ]:
        await _drop_table_if_exists(ctx, table_name)

    await _cleanup_legacy_storage_columns(ctx)
    return True


async def _migration_remove_dead_kodi_columns(ctx: MigrationContext):
    dropped_any = False
    for column_name in ("nonce", "consumed_at"):
        dropped_any = (
            await _drop_column_if_exists(ctx, "kodi_setup_codes", column_name)
            or dropped_any
        )

    if dropped_any and ctx.is_sqlite:
        await _checkpoint_sqlite(ctx)
    return True


MIGRATIONS = [
    ("2026030901_foundation", _migration_foundation),
    ("2026030902_backfill_canonical_tables", _migration_backfill_canonical_tables),
    ("2026030903_integrity_rollout", _migration_integrity_rollout),
    ("2026030904_indexes", _migration_indexes),
    (LEGACY_STORAGE_CLEANUP_MIGRATION, _migration_cleanup_legacy_storage),
    ("2026031201_remove_dead_kodi_columns", _migration_remove_dead_kodi_columns),
]
