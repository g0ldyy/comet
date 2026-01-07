import time

import orjson

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
            parsed = EXCLUDED.parsed,
            timestamp = EXCLUDED.timestamp
        WHERE
            debrid_availability.title IS DISTINCT FROM EXCLUDED.title
            OR debrid_availability.file_index IS DISTINCT FROM EXCLUDED.file_index
            OR debrid_availability.size IS DISTINCT FROM EXCLUDED.size
            OR debrid_availability.parsed IS DISTINCT FROM EXCLUDED.parsed
            OR COALESCE(debrid_availability.timestamp, 0) < (EXCLUDED.timestamp - :update_interval)
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
            "size": file["size"] if file["index"] is not None else None,
            "parsed": orjson.dumps(file["parsed"], default_dump).decode("utf-8")
            if file["parsed"] is not None
            else None,
            "timestamp": current_time,
            "update_interval": DEBRID_UPDATE_INTERVAL,
        }
        for file in availability
    ]

    if settings.DATABASE_TYPE == "sqlite":
        query = """
            INSERT OR REPLACE
            INTO debrid_availability
            VALUES (:debrid_service, :info_hash, :file_index, :title, :season, :episode, :size, :parsed, :timestamp)
        """
        sqlite_values = [
            {k: v for k, v in val.items() if k != "update_interval"} for val in values
        ]
        await database.execute_many(query, sqlite_values)
    else:
        both_values = []
        season_only_values = []
        episode_only_values = []
        no_season_episode_values = []

        for val in values:
            if val["season"] is not None and val["episode"] is not None:
                both_values.append(val)
            elif val["season"] is not None and val["episode"] is None:
                season_only_values.append(val)
            elif val["season"] is None and val["episode"] is not None:
                episode_only_values.append(val)
            else:
                no_season_episode_values.append(val)

        if both_values:
            query = f"""
                INSERT INTO debrid_availability
                VALUES (:debrid_service, :info_hash, :file_index, :title, :season, :episode, :size, :parsed, :timestamp)
                ON CONFLICT (debrid_service, info_hash, season, episode) 
                WHERE season IS NOT NULL AND episode IS NOT NULL
                {CONDITIONAL_UPDATE}
            """
            await database.execute_many(query, both_values)

        if season_only_values:
            query = f"""
                INSERT INTO debrid_availability
                VALUES (:debrid_service, :info_hash, :file_index, :title, :season, :episode, :size, :parsed, :timestamp)
                ON CONFLICT (debrid_service, info_hash, season) 
                WHERE season IS NOT NULL AND episode IS NULL
                {CONDITIONAL_UPDATE}
            """
            await database.execute_many(query, season_only_values)

        if episode_only_values:
            query = f"""
                INSERT INTO debrid_availability
                VALUES (:debrid_service, :info_hash, :file_index, :title, :season, :episode, :size, :parsed, :timestamp)
                ON CONFLICT (debrid_service, info_hash, episode) 
                WHERE season IS NULL AND episode IS NOT NULL
                {CONDITIONAL_UPDATE}
            """
            await database.execute_many(query, episode_only_values)

        if no_season_episode_values:
            query = f"""
                INSERT INTO debrid_availability
                VALUES (:debrid_service, :info_hash, :file_index, :title, :season, :episode, :size, :parsed, :timestamp)
                ON CONFLICT (debrid_service, info_hash) 
                WHERE season IS NULL AND episode IS NULL
                {CONDITIONAL_UPDATE}
            """
            await database.execute_many(query, no_season_episode_values)


async def get_cached_availability(
    debrid_service: str, info_hashes: list, season: int = None, episode: int = None
):
    select_clause = "SELECT info_hash, file_index, title, size, parsed"
    if debrid_service == "torrent" and settings.DATABASE_TYPE == "postgresql":
        select_clause = (
            "SELECT DISTINCT ON (info_hash) info_hash, file_index, title, size, parsed"
        )

    base_from_where = f"""
        FROM debrid_availability
        WHERE info_hash IN (SELECT CAST(value as TEXT) FROM {"json_array_elements_text" if settings.DATABASE_TYPE == "postgresql" else "json_each"}(:info_hashes))
        AND timestamp + :cache_ttl >= :current_time
    """

    params = {
        "info_hashes": orjson.dumps(info_hashes).decode("utf-8"),
        "cache_ttl": settings.DEBRID_CACHE_TTL,
        "current_time": time.time(),
        "season": season,
        "episode": episode,
    }

    if debrid_service != "torrent":
        base_from_where += " AND debrid_service = :debrid_service"
        params["debrid_service"] = debrid_service

    group_by_clause = ""
    if debrid_service == "torrent" and settings.DATABASE_TYPE == "sqlite":
        group_by_clause = " GROUP BY info_hash"

    if debrid_service == "offcloud":
        season_episode_filter = """
            AND ((CAST(:season as INTEGER) IS NULL AND season IS NULL) OR season = CAST(:season as INTEGER))
            AND ((CAST(:episode as INTEGER) IS NULL AND episode IS NULL) OR episode = CAST(:episode as INTEGER))
        """
        query = (
            select_clause + base_from_where + season_episode_filter + group_by_clause
        )

        results = await database.fetch_all(query, params)

        found_hashes = {r["info_hash"] for r in results}
        remaining_hashes = [h for h in info_hashes if h not in found_hashes]

        if remaining_hashes:
            null_title_params = {
                "info_hashes": orjson.dumps(remaining_hashes).decode("utf-8"),
                "cache_ttl": settings.DEBRID_CACHE_TTL,
                "current_time": time.time(),
            }
            if debrid_service != "torrent":
                null_title_params["debrid_service"] = debrid_service

            null_title_query = (
                select_clause + base_from_where + " AND title IS NULL" + group_by_clause
            )
            null_results = await database.fetch_all(null_title_query, null_title_params)
            results.extend(null_results)
    else:
        query = (
            select_clause
            + base_from_where
            + """
            AND ((CAST(:season as INTEGER) IS NULL AND season IS NULL) OR season = CAST(:season as INTEGER))
            AND ((CAST(:episode as INTEGER) IS NULL AND episode IS NULL) OR episode = CAST(:episode as INTEGER))
        """
            + group_by_clause
        )
        results = await database.fetch_all(query, params)

    return results
