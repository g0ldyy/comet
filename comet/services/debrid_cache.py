import time

import orjson

from comet.core.database import (IS_SQLITE, JSON_FUNC,
                                 build_scope_lookup_params, build_scope_params)
from comet.core.models import database, settings
from comet.utils.parsing import default_dump

DEBRID_UPDATE_INTERVAL = (
    settings.DEBRID_CACHE_TTL // 2 if settings.DEBRID_CACHE_TTL > 0 else 31536000
)

CONDITIONAL_UPDATE = """
        DO UPDATE SET
            title = EXCLUDED.title,
            file_index = EXCLUDED.file_index,
            size = EXCLUDED.size,
            parsed_json = EXCLUDED.parsed_json,
            updated_at = EXCLUDED.updated_at
        WHERE
            debrid_availability.title IS DISTINCT FROM EXCLUDED.title
            OR debrid_availability.file_index IS DISTINCT FROM EXCLUDED.file_index
            OR debrid_availability.size IS DISTINCT FROM EXCLUDED.size
            OR debrid_availability.parsed_json IS DISTINCT FROM EXCLUDED.parsed_json
            OR COALESCE(debrid_availability.updated_at, 0) < (EXCLUDED.updated_at - :update_interval)
"""


async def cache_availability(debrid_service: str, availability: list):
    current_time = time.time()

    values = [
        {
            "debrid_service": debrid_service,
            "info_hash": file["info_hash"],
            "file_index": str(file["index"]) if file["index"] is not None else None,
            "title": file["title"],
            "season": file["season"],
            "episode": file["episode"],
            **build_scope_params(file["season"], file["episode"]),
            "size": file["size"] if file["index"] is not None else None,
            "parsed_json": orjson.dumps(file["parsed"], default_dump).decode("utf-8")
            if file["parsed"] is not None
            else None,
            "updated_at": current_time,
            "update_interval": DEBRID_UPDATE_INTERVAL,
        }
        for file in availability
    ]

    if IS_SQLITE:
        query = """
            INSERT OR REPLACE
            INTO debrid_availability (
                debrid_service,
                info_hash,
                season,
                episode,
                season_norm,
                episode_norm,
                file_index,
                title,
                size,
                parsed_json,
                updated_at
            )
            VALUES (
                :debrid_service,
                :info_hash,
                :season,
                :episode,
                :season_norm,
                :episode_norm,
                :file_index,
                :title,
                :size,
                :parsed_json,
                :updated_at
            )
        """
        sqlite_values = [
            {k: v for k, v in val.items() if k != "update_interval"} for val in values
        ]
        await database.execute_many(query, sqlite_values)
    else:
        query = f"""
            INSERT INTO debrid_availability (
                debrid_service,
                info_hash,
                season,
                episode,
                season_norm,
                episode_norm,
                file_index,
                title,
                size,
                parsed_json,
                updated_at
            )
            VALUES (
                :debrid_service,
                :info_hash,
                :season,
                :episode,
                :season_norm,
                :episode_norm,
                :file_index,
                :title,
                :size,
                :parsed_json,
                :updated_at
            )
            ON CONFLICT (debrid_service, info_hash, season_norm, episode_norm)
            {CONDITIONAL_UPDATE}
        """
        await database.execute_many(query, values)


async def get_cached_availability(
    debrid_service: str, info_hashes: list, season: int = None, episode: int = None
):
    select_clause = "SELECT info_hash, file_index, title, size, parsed_json AS parsed"

    min_timestamp = time.time() - settings.DEBRID_CACHE_TTL
    base_from_where = f"""
        FROM debrid_availability
        WHERE info_hash IN (SELECT CAST(value as TEXT) FROM {JSON_FUNC}(:info_hashes))
        AND updated_at >= :min_timestamp
    """

    params = {
        "info_hashes": orjson.dumps(info_hashes).decode("utf-8"),
        "min_timestamp": min_timestamp,
        **build_scope_lookup_params(season, episode),
    }

    base_from_where += " AND debrid_service = :debrid_service"
    params["debrid_service"] = debrid_service

    if debrid_service == "offcloud":
        season_episode_filter = """
            AND season_norm = :season_norm
            AND episode_norm = :episode_norm
        """
        query = select_clause + base_from_where + season_episode_filter

        results = await database.fetch_all(query, params)

        found_hashes = {r["info_hash"] for r in results}
        remaining_hashes = [h for h in info_hashes if h not in found_hashes]

        if remaining_hashes:
            null_title_params = {
                "info_hashes": orjson.dumps(remaining_hashes).decode("utf-8"),
                "min_timestamp": min_timestamp,
            }
            if debrid_service != "torrent":
                null_title_params["debrid_service"] = debrid_service

            null_title_query = select_clause + base_from_where + " AND title IS NULL"
            null_results = await database.fetch_all(null_title_query, null_title_params)
            results.extend(null_results)
    else:
        query = (
            select_clause
            + base_from_where
            + """
            AND season_norm = :season_norm
            AND episode_norm = :episode_norm
        """
        )
        results = await database.fetch_all(query, params)

    return results


async def get_cached_availability_any_service(
    info_hashes: list, season: int = None, episode: int = None
):
    min_timestamp = time.time() - settings.DEBRID_CACHE_TTL
    base_from_where = f"""
        FROM debrid_availability
        WHERE info_hash IN (SELECT CAST(value as TEXT) FROM {JSON_FUNC}(:info_hashes))
        AND updated_at >= :min_timestamp
        AND season_norm = :season_norm
        AND episode_norm = :episode_norm
    """

    params = {
        "info_hashes": orjson.dumps(info_hashes).decode("utf-8"),
        "min_timestamp": min_timestamp,
        **build_scope_lookup_params(season, episode),
    }

    if IS_SQLITE:
        query = f"""
            SELECT info_hash, file_index, title, size, parsed
            FROM (
                SELECT info_hash, file_index, title, size, parsed_json AS parsed,
                       ROW_NUMBER() OVER (PARTITION BY info_hash ORDER BY updated_at DESC) AS rn
                {base_from_where}
            ) WHERE rn = 1
        """
    else:
        query = (
            "SELECT DISTINCT ON (info_hash) info_hash, file_index, title, size, parsed_json AS parsed "
            + base_from_where
            + " ORDER BY info_hash, updated_at DESC"
        )

    return await database.fetch_all(query, params)
