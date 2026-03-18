import time

from comet.core.database import (build_distinct_from_predicate,
                                 build_json_list_membership_predicate,
                                 build_scope_lookup_params, build_scope_params,
                                 build_upsert_assignments, encode_json_param)
from comet.core.models import database, settings
from comet.utils.parsing import default_dump

DEBRID_UPDATE_INTERVAL = (
    settings.DEBRID_CACHE_TTL // 2 if settings.DEBRID_CACHE_TTL > 0 else 31536000
)

DEBRID_CHANGE_DETECTION_COLUMNS = (
    "title",
    "file_index",
    "size",
    "parsed_json",
)
DEBRID_UPDATE_COLUMNS = (*DEBRID_CHANGE_DETECTION_COLUMNS, "updated_at")
DEBRID_UPDATE_SET_SQL = build_upsert_assignments(DEBRID_UPDATE_COLUMNS)
DEBRID_DISTINCT_UPDATE_WHERE_SQL = build_distinct_from_predicate(
    "debrid_availability",
    "EXCLUDED",
    DEBRID_CHANGE_DETECTION_COLUMNS,
)
INFO_HASH_MEMBERSHIP_SQL = build_json_list_membership_predicate(
    "info_hash", "info_hashes"
)
SCOPE_FILTER_SQL = """
season_norm = :season_norm
AND episode_norm = :episode_norm
"""


def _build_conditional_update() -> str:
    return f"""
        DO UPDATE SET
{DEBRID_UPDATE_SET_SQL}
        WHERE
            {DEBRID_DISTINCT_UPDATE_WHERE_SQL}
            OR COALESCE(debrid_availability.updated_at, 0) < (EXCLUDED.updated_at - :update_interval)
"""


CONDITIONAL_UPDATE_SQL = _build_conditional_update()
CACHE_AVAILABILITY_QUERY = f"""
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
    {CONDITIONAL_UPDATE_SQL}
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
            "parsed_json": (
                encode_json_param(file["parsed"], default=default_dump)
                if file["parsed"] is not None
                else None
            ),
            "updated_at": current_time,
            "update_interval": DEBRID_UPDATE_INTERVAL,
        }
        for file in availability
    ]

    await database.execute_many(CACHE_AVAILABILITY_QUERY, values)


async def get_cached_availability(
    debrid_service: str,
    info_hashes: list[str],
    season: int | None = None,
    episode: int | None = None,
):
    select_clause = "SELECT info_hash, file_index, title, size, parsed_json AS parsed"

    min_timestamp = time.time() - settings.DEBRID_CACHE_TTL
    base_from_where = f"""
        FROM debrid_availability
        WHERE {INFO_HASH_MEMBERSHIP_SQL}
        AND updated_at >= :min_timestamp
    """

    params = {
        "info_hashes": encode_json_param(info_hashes),
        "min_timestamp": min_timestamp,
        **build_scope_lookup_params(season, episode),
    }

    base_from_where += " AND debrid_service = :debrid_service"
    params["debrid_service"] = debrid_service

    if debrid_service == "offcloud":
        query = f"""
            SELECT info_hash, file_index, title, size, parsed
            FROM (
                SELECT
                    info_hash,
                    file_index,
                    title,
                    size,
                    parsed_json AS parsed,
                    ROW_NUMBER() OVER (
                        PARTITION BY info_hash
                        ORDER BY
                            CASE WHEN {SCOPE_FILTER_SQL} THEN 0 ELSE 1 END,
                            updated_at DESC
                    ) AS row_number
                {base_from_where}
                AND (
                    ({SCOPE_FILTER_SQL})
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
            AND {SCOPE_FILTER_SQL}
        """
        results = await database.fetch_all(query, params)

    return results


async def get_cached_availability_any_service(
    info_hashes: list, season: int = None, episode: int = None
):
    min_timestamp = time.time() - settings.DEBRID_CACHE_TTL
    base_from_where = f"""
        FROM debrid_availability
        WHERE {INFO_HASH_MEMBERSHIP_SQL}
        AND updated_at >= :min_timestamp
        AND season_norm = :season_norm
        AND episode_norm = :episode_norm
    """

    params = {
        "info_hashes": encode_json_param(info_hashes),
        "min_timestamp": min_timestamp,
        **build_scope_lookup_params(season, episode),
    }

    query = f"""
        SELECT info_hash, file_index, title, size, parsed
        FROM (
            SELECT
                info_hash,
                file_index,
                title,
                size,
                parsed_json AS parsed,
                ROW_NUMBER() OVER (
                    PARTITION BY info_hash
                    ORDER BY updated_at DESC
                ) AS row_number
            {base_from_where}
        ) latest_debrid_availability
        WHERE row_number = 1
    """

    return await database.fetch_all(query, params)
