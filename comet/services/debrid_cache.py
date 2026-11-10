import asyncio
import time
from functools import partial

from comet.core.database import (
    build_changed_rows_upsert,
    build_json_list_membership_predicate,
    build_scope_lookup_params,
    build_scope_params,
    encode_json_param,
)
from comet.core.models import database, settings
from comet.metadata.media_info import media_info_to_json
from comet.observability import log
from comet.observability.context import create_detached_task
from comet.utils.parsing import MediaScope, default_dump

CACHE_AVAILABILITY_QUERY = build_changed_rows_upsert(
    "debrid_availability",
    columns={
        "debrid_service": "TEXT",
        "info_hash": "TEXT",
        "season": "INTEGER",
        "episode": "INTEGER",
        "season_norm": "INTEGER",
        "episode_norm": "INTEGER",
        "file_index": "TEXT",
        "title": "TEXT",
        "size": "BIGINT",
        "parsed_json": "TEXT",
        "media_info_json": "TEXT",
        "updated_at": "DOUBLE PRECISION",
    },
    conflict_columns=("debrid_service", "info_hash", "season_norm", "episode_norm"),
    compared_columns=("title", "file_index", "size", "parsed_json"),
    refreshed_column="updated_at",
    unchanged_conditions=(
        """(
            CAST(:media_info_json AS TEXT) IS NULL
            OR current.media_info_json IS NOT DISTINCT FROM CAST(:media_info_json AS TEXT)
        )""",
    ),
    update_expressions={
        "media_info_json": """CASE
            WHEN EXCLUDED.media_info_json IS NOT NULL
                THEN EXCLUDED.media_info_json
            WHEN debrid_availability.file_index IS NOT DISTINCT FROM EXCLUDED.file_index
                AND debrid_availability.title IS NOT DISTINCT FROM EXCLUDED.title
                THEN debrid_availability.media_info_json
            ELSE NULL
        END""",
    },
)
INFO_HASH_MEMBERSHIP_SQL = build_json_list_membership_predicate(
    "info_hash", "info_hashes"
)
SEASON_SCOPE_FILTER_SQL = "season_norm = :season_norm"
EXACT_SCOPE_FILTER_SQL = f"{SEASON_SCOPE_FILTER_SQL}\nAND episode_norm = :episode_norm"
_cache_write_tasks: set[asyncio.Task] = set()


def _handle_cache_write_done(debrid_service: str, task: asyncio.Task) -> None:
    _cache_write_tasks.discard(task)
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        log.warning(
            "debrid.cache.persist_failed",
            "Debrid cache persistence failed",
            debrid_service=debrid_service,
            exc=error,
        )


def schedule_cache_availability(debrid_service: str, availability: list):
    task = create_detached_task(
        cache_availability(debrid_service, availability),
        name="debrid-cache-write",
    )
    _cache_write_tasks.add(task)
    task.add_done_callback(partial(_handle_cache_write_done, debrid_service))
    return task


async def schedule_cache_availability_after_response(
    debrid_service: str,
    availability: list,
) -> None:
    schedule_cache_availability(debrid_service, availability)


async def shutdown_cache_writes() -> None:
    if not _cache_write_tasks:
        return
    await asyncio.gather(*tuple(_cache_write_tasks), return_exceptions=True)


def _build_scope_lookup(
    media_scope: MediaScope,
    season: int | None,
    episode: int | None,
) -> tuple[str | None, dict[str, int]]:
    if media_scope is MediaScope.SERIES:
        return None, {}

    params = build_scope_lookup_params(season, episode)
    if media_scope is MediaScope.SEASON:
        params.pop("episode_norm")
        return SEASON_SCOPE_FILTER_SQL, params
    return EXACT_SCOPE_FILTER_SQL, params


async def cache_availability(debrid_service: str, availability: list):
    current_time = time.time()

    values_by_scope = {}
    for file in availability:
        scope = build_scope_params(file["season"], file["episode"])
        value = {
            "debrid_service": debrid_service,
            "info_hash": file["info_hash"],
            "file_index": str(file["index"]) if file["index"] is not None else None,
            "title": file["title"],
            "season": file["season"],
            "episode": file["episode"],
            **scope,
            "size": file["size"] if file["index"] is not None else None,
            "parsed_json": (
                encode_json_param(file["parsed"], default=default_dump)
                if file["parsed"] is not None
                else None
            ),
            "media_info_json": media_info_to_json(file.get("media_info")),
            "updated_at": current_time,
            "refresh_before": current_time
            - (
                settings.DEBRID_CACHE_TTL // 2
                if settings.DEBRID_CACHE_TTL > 0
                else 31_536_000
            ),
        }
        values_by_scope[
            (file["info_hash"], scope["season_norm"], scope["episode_norm"])
        ] = value

    if values_by_scope:
        await database.execute_many(
            CACHE_AVAILABILITY_QUERY,
            list(values_by_scope.values()),
        )
        log.info(
            "debrid.cache.persisted",
            "Debrid cache persisted",
            debrid_service=debrid_service,
            candidate_count=len(values_by_scope),
        )


async def get_cached_availability(
    debrid_service: str,
    info_hashes: list[str],
    media_scope: MediaScope,
    season: int | None = None,
    episode: int | None = None,
):
    select_clause = """
        SELECT info_hash, file_index, title, size,
               parsed_json AS parsed, media_info_json AS media_info
    """
    scope_filter_sql, scope_params = _build_scope_lookup(media_scope, season, episode)

    min_timestamp = time.time() - settings.DEBRID_CACHE_TTL
    base_from_where = f"""
        FROM debrid_availability
        WHERE {INFO_HASH_MEMBERSHIP_SQL}
        AND updated_at >= :min_timestamp
    """

    params = {
        "info_hashes": encode_json_param(info_hashes),
        "min_timestamp": min_timestamp,
        **scope_params,
    }

    base_from_where += " AND debrid_service = :debrid_service"
    params["debrid_service"] = debrid_service

    if media_scope.is_aggregate:
        scope_condition = ""
        if scope_filter_sql is not None:
            offcloud_fallback = (
                " OR title IS NULL" if debrid_service == "offcloud" else ""
            )
            scope_condition = f"""
                AND (
                    ({scope_filter_sql})
                    {offcloud_fallback}
                )
            """
        query = f"""
            SELECT DISTINCT info_hash
            {base_from_where}
            {scope_condition}
        """
        results = await database.fetch_all(query, params)
    elif debrid_service == "offcloud":
        query = f"""
            SELECT info_hash, file_index, title, size, parsed, media_info
            FROM (
                SELECT
                    info_hash,
                    file_index,
                    title,
                    size,
                    parsed_json AS parsed,
                    media_info_json AS media_info,
                    ROW_NUMBER() OVER (
                        PARTITION BY info_hash
                        ORDER BY
                            CASE WHEN {scope_filter_sql} THEN 0 ELSE 1 END,
                            updated_at DESC
                    ) AS row_number
                {base_from_where}
                AND (
                    ({scope_filter_sql})
                    OR title IS NULL
                )
            ) ranked_offcloud_availability
            WHERE row_number = 1
        """
        results = await database.fetch_all(query, params)
    else:
        query = f"""
            {select_clause}
            {base_from_where}
            AND {scope_filter_sql}
        """
        results = await database.fetch_all(query, params)

    return results


async def get_cached_availability_any_service(
    info_hashes: list, season: int | None = None, episode: int | None = None
):
    min_timestamp = time.time() - settings.DEBRID_CACHE_TTL
    base_from_where = f"""
        FROM debrid_availability
        WHERE {INFO_HASH_MEMBERSHIP_SQL}
        AND updated_at >= :min_timestamp
        AND {EXACT_SCOPE_FILTER_SQL}
    """

    params = {
        "info_hashes": encode_json_param(info_hashes),
        "min_timestamp": min_timestamp,
        **build_scope_lookup_params(season, episode),
    }

    query = f"""
        SELECT info_hash, file_index, title, size,
               parsed_json AS parsed, media_info_json AS media_info
        {base_from_where}
        ORDER BY updated_at DESC
    """

    return await database.fetch_all(query, params)
