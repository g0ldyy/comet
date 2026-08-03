import time
from datetime import date

import aiohttp

from comet.core.models import database, settings
from comet.metadata.http import MetadataHttpError, get_metadata_json
from comet.metadata.tmdb import TMDBApi
from comet.metadata.validation import episode_coordinate

_CINEMETA_SERIES_META_URL = "https://v3-cinemeta.strem.io/meta/series/{series_id}.json"

_TARGET_EPISODE_AIR_DATE_QUERY = """
    SELECT air_date
    FROM series_episode_index
    WHERE series_id = :series_id
      AND season = CAST(:season AS INTEGER)
      AND episode = CAST(:episode AS INTEGER)
      AND (
          CAST(:min_timestamp AS DOUBLE PRECISION) IS NULL
          OR updated_at >= CAST(:min_timestamp AS DOUBLE PRECISION)
      )
"""

_EPISODE_BY_AIR_DATE_QUERY = """
    SELECT season, episode
    FROM series_episode_index
    WHERE series_id = :series_id
      AND air_date = :air_date
      AND (
          CAST(:min_timestamp AS DOUBLE PRECISION) IS NULL
          OR updated_at >= CAST(:min_timestamp AS DOUBLE PRECISION)
      )
    ORDER BY season, episode
    LIMIT 1
"""

_SERIES_INDEX_LAST_REFRESH_QUERY = """
    SELECT refreshed_at
    FROM series_episode_index_refresh
    WHERE series_id = :series_id
"""

_UPSERT_SERIES_EPISODE_INDEX_QUERY = """
    INSERT INTO series_episode_index (
        series_id,
        season,
        episode,
        air_date,
        updated_at
    )
    VALUES (
        :series_id,
        :season,
        :episode,
        :air_date,
        :updated_at
    )
    ON CONFLICT (series_id, season, episode) DO UPDATE SET
        air_date = EXCLUDED.air_date,
        updated_at = EXCLUDED.updated_at
"""

_UPSERT_SERIES_INDEX_REFRESH_QUERY = """
    INSERT INTO series_episode_index_refresh (
        series_id,
        refreshed_at
    )
    VALUES (
        :series_id,
        :refreshed_at
    )
    ON CONFLICT (series_id) DO UPDATE SET
        refreshed_at = EXCLUDED.refreshed_at
"""

_DELETE_SERIES_EPISODE_INDEX_QUERY = """
    DELETE FROM series_episode_index
    WHERE series_id = :series_id
"""


def _normalize_air_date(raw_value) -> str | None:
    if not raw_value:
        return None

    candidate = raw_value.strip().split("T", 1)[0]
    try:
        date.fromisoformat(candidate)
    except ValueError:
        return None
    return candidate


class EpisodeIndexService:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def _get_cached_air_date(
        self,
        series_id: str,
        season: int,
        episode: int,
        min_timestamp: float | None,
    ) -> str | None:
        row = await database.fetch_one(
            _TARGET_EPISODE_AIR_DATE_QUERY,
            {
                "series_id": series_id,
                "season": season,
                "episode": episode,
                "min_timestamp": min_timestamp,
            },
        )
        if row is None:
            return None
        return row["air_date"]

    async def _get_cached_episode(
        self,
        series_id: str,
        air_date: str,
        min_timestamp: float | None,
    ) -> tuple[int, int] | None:
        row = await database.fetch_one(
            _EPISODE_BY_AIR_DATE_QUERY,
            {
                "series_id": series_id,
                "air_date": air_date,
                "min_timestamp": min_timestamp,
            },
        )
        if row is None:
            return None
        return row["season"], row["episode"]

    async def _is_series_index_fresh(
        self, series_id: str, min_timestamp: float
    ) -> bool:
        last_refreshed = await database.fetch_val(
            _SERIES_INDEX_LAST_REFRESH_QUERY,
            {"series_id": series_id},
        )
        if last_refreshed is None:
            return False
        return float(last_refreshed) >= min_timestamp

    async def _upsert_series_air_dates(self, rows: list[dict]) -> None:
        if not rows:
            return
        await database.execute_many(_UPSERT_SERIES_EPISODE_INDEX_QUERY, rows)

    async def _delete_series_air_dates(self, series_id: str) -> None:
        await database.execute(
            _DELETE_SERIES_EPISODE_INDEX_QUERY,
            {"series_id": series_id},
        )

    async def _upsert_series_refresh(self, series_id: str, refreshed_at: float) -> None:
        await database.execute(
            _UPSERT_SERIES_INDEX_REFRESH_QUERY,
            {"series_id": series_id, "refreshed_at": refreshed_at},
        )

    async def _replace_series_index(
        self,
        series_id: str,
        refreshed_at: float,
        rows: list[dict],
    ) -> None:
        async with database.transaction():
            await self._delete_series_air_dates(series_id)
            await self._upsert_series_air_dates(rows)
            await self._upsert_series_refresh(series_id, refreshed_at)

    async def _refresh_from_cinemeta(self, series_id: str) -> None:
        try:
            response = await get_metadata_json(
                self.session, _CINEMETA_SERIES_META_URL.format(series_id=series_id)
            )
        except MetadataHttpError:
            return
        if response.status == 404:
            await self._upsert_series_refresh(series_id, time.time())
            return
        if not response.successful:
            return

        meta = response.payload.get("meta")
        if meta is None:
            return
        videos = meta.get("videos") or []

        updated_at = time.time()
        unique_rows: dict[tuple[int, int], dict] = {}
        for video in videos:
            season_int = episode_coordinate(video.get("season"))
            episode_int = episode_coordinate(video.get("episode", video.get("number")))
            if season_int is None or episode_int is None:
                continue

            air_date = _normalize_air_date(
                video.get("released")
                or video.get("firstAired")
                or video.get("air_date")
                or video.get("first_aired")
            )
            if air_date is None:
                continue

            key = (season_int, episode_int)
            existing = unique_rows.get(key)
            if existing is not None and existing["air_date"] != air_date:
                return
            unique_rows[key] = {
                "series_id": series_id,
                "season": season_int,
                "episode": episode_int,
                "air_date": air_date,
                "updated_at": updated_at,
            }

        await self._replace_series_index(
            series_id,
            updated_at,
            list(unique_rows.values()),
        )

    async def _refresh_single_episode_from_tmdb(
        self,
        series_id: str,
        season: int,
        episode: int,
    ) -> str | None:
        tmdb = TMDBApi(self.session)
        tmdb_id = await tmdb.get_tmdb_id_from_imdb(series_id, "series")
        if tmdb_id is None:
            return None

        air_date = _normalize_air_date(
            await tmdb.get_episode_air_date(tmdb_id, season, episode)
        )
        if air_date is None:
            return None

        await self._upsert_series_air_dates(
            [
                {
                    "series_id": series_id,
                    "season": season,
                    "episode": episode,
                    "air_date": air_date,
                    "updated_at": time.time(),
                }
            ]
        )
        return air_date

    async def get_target_air_date(
        self,
        series_id: str,
        season: int | None,
        episode: int | None,
    ) -> str | None:
        season_value = episode_coordinate(season)
        episode_value = episode_coordinate(episode)
        if season_value is None or episode_value is None:
            return None
        season = season_value
        episode = episode_value

        min_timestamp = time.time() - settings.METADATA_CACHE_TTL

        cached_air_date = await self._get_cached_air_date(
            series_id, season, episode, min_timestamp
        )
        if cached_air_date is not None:
            return cached_air_date

        if not await self._is_series_index_fresh(series_id, min_timestamp):
            await self._refresh_from_cinemeta(series_id)
            cached_air_date = await self._get_cached_air_date(
                series_id, season, episode, min_timestamp
            )
            if cached_air_date is not None:
                return cached_air_date

        tmdb_air_date = await self._refresh_single_episode_from_tmdb(
            series_id, season, episode
        )
        if tmdb_air_date is not None:
            return tmdb_air_date

        return await self._get_cached_air_date(series_id, season, episode, None)

    async def get_episode_by_air_date(
        self,
        series_id: str,
        air_date: str,
    ) -> tuple[int, int] | None:
        normalized_air_date = _normalize_air_date(air_date)
        if normalized_air_date is None:
            return None

        min_timestamp = time.time() - settings.METADATA_CACHE_TTL
        cached_episode = await self._get_cached_episode(
            series_id, normalized_air_date, min_timestamp
        )
        if cached_episode is not None:
            return cached_episode

        if not await self._is_series_index_fresh(series_id, min_timestamp):
            await self._refresh_from_cinemeta(series_id)
            cached_episode = await self._get_cached_episode(
                series_id, normalized_air_date, min_timestamp
            )
            if cached_episode is not None:
                return cached_episode

        return await self._get_cached_episode(series_id, normalized_air_date, None)
