import time

import orjson

from comet.core.models import database, settings
from comet.utils.parsing import default_dump


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
        }
        for file in availability
    ]

    if settings.DATABASE_TYPE == "sqlite":
        query = """
            INSERT OR REPLACE
            INTO debrid_availability
            VALUES (:debrid_service, :info_hash, :file_index, :title, :season, :episode, :size, :parsed, :timestamp)
        """
        await database.execute_many(query, values)
    elif settings.DATABASE_TYPE == "postgresql":
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

        # handle each case separately with appropriate ON CONFLICT clauses
        if both_values:
            query = """
                INSERT INTO debrid_availability
                VALUES (:debrid_service, :info_hash, :file_index, :title, :season, :episode, :size, :parsed, :timestamp)
                ON CONFLICT (debrid_service, info_hash, season, episode) 
                WHERE season IS NOT NULL AND episode IS NOT NULL
                DO UPDATE SET
                title = EXCLUDED.title,
                file_index = EXCLUDED.file_index,
                size = EXCLUDED.size,
                parsed = EXCLUDED.parsed,
                timestamp = EXCLUDED.timestamp
            """
            await database.execute_many(query, both_values)

        if season_only_values:
            query = """
                INSERT INTO debrid_availability
                VALUES (:debrid_service, :info_hash, :file_index, :title, :season, :episode, :size, :parsed, :timestamp)
                ON CONFLICT (debrid_service, info_hash, season) 
                WHERE season IS NOT NULL AND episode IS NULL
                DO UPDATE SET
                title = EXCLUDED.title,
                file_index = EXCLUDED.file_index,
                size = EXCLUDED.size,
                parsed = EXCLUDED.parsed,
                timestamp = EXCLUDED.timestamp
            """
            await database.execute_many(query, season_only_values)

        if episode_only_values:
            query = """
                INSERT INTO debrid_availability
                VALUES (:debrid_service, :info_hash, :file_index, :title, :season, :episode, :size, :parsed, :timestamp)
                ON CONFLICT (debrid_service, info_hash, episode) 
                WHERE season IS NULL AND episode IS NOT NULL
                DO UPDATE SET
                title = EXCLUDED.title,
                file_index = EXCLUDED.file_index,
                size = EXCLUDED.size,
                parsed = EXCLUDED.parsed,
                timestamp = EXCLUDED.timestamp
            """
            await database.execute_many(query, episode_only_values)

        if no_season_episode_values:
            query = """
                INSERT INTO debrid_availability
                VALUES (:debrid_service, :info_hash, :file_index, :title, :season, :episode, :size, :parsed, :timestamp)
                ON CONFLICT (debrid_service, info_hash) 
                WHERE season IS NULL AND episode IS NULL
                DO UPDATE SET
                title = EXCLUDED.title,
                file_index = EXCLUDED.file_index,
                size = EXCLUDED.size,
                parsed = EXCLUDED.parsed,
                timestamp = EXCLUDED.timestamp
            """
            await database.execute_many(query, no_season_episode_values)
    else:
        query = """
            INSERT 
            INTO debrid_availability
            VALUES (:debrid_service, :info_hash, :file_index, :title, :season, :episode, :size, :parsed, :timestamp)
        """
        await database.execute_many(query, values)


async def get_cached_availability(
    debrid_service: str, info_hashes: list, season: int = None, episode: int = None
):
    base_query = f"""
        SELECT info_hash, file_index, title, size, parsed
        FROM debrid_availability
        WHERE info_hash IN (SELECT CAST(value as TEXT) FROM {"json_array_elements_text" if settings.DATABASE_TYPE == "postgresql" else "json_each"}(:info_hashes))
        AND debrid_service = :debrid_service
        AND timestamp + :cache_ttl >= :current_time
    """

    params = {
        "info_hashes": orjson.dumps(info_hashes).decode("utf-8"),
        "debrid_service": debrid_service,
        "cache_ttl": settings.DEBRID_CACHE_TTL,
        "current_time": time.time(),
        "season": season,
        "episode": episode,
    }

    if debrid_service == "offcloud":
        query = (
            base_query
            + """
            AND ((CAST(:season as INTEGER) IS NULL AND season IS NULL) OR season = CAST(:season as INTEGER))
            AND ((CAST(:episode as INTEGER) IS NULL AND episode IS NULL) OR episode = CAST(:episode as INTEGER))
        """
        )
        results = await database.fetch_all(query, params)

        found_hashes = {r["info_hash"] for r in results}
        remaining_hashes = [h for h in info_hashes if h not in found_hashes]

        if remaining_hashes:
            null_title_params = {
                "info_hashes": orjson.dumps(remaining_hashes).decode("utf-8"),
                "debrid_service": debrid_service,
                "cache_ttl": settings.DEBRID_CACHE_TTL,
                "current_time": time.time(),
            }
            null_title_query = base_query + " AND title IS NULL"
            null_results = await database.fetch_all(null_title_query, null_title_params)
            results.extend(null_results)
    else:
        query = (
            base_query
            + """
            AND ((CAST(:season as INTEGER) IS NULL AND season IS NULL) OR season = CAST(:season as INTEGER))
            AND ((CAST(:episode as INTEGER) IS NULL AND episode IS NULL) OR episode = CAST(:episode as INTEGER))
        """
        )
        results = await database.fetch_all(query, params)

    return results
