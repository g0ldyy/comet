import time
from datetime import datetime

import aiohttp

from comet.core.logger import logger
from comet.core.models import database, settings
from comet.metadata.tmdb import TMDBApi

_CINEMETA_SERIES_META_URL = "https://v3-cinemeta.strem.io/meta/series/{series_id}.json"

_TARGET_EPISODE_AIR_DATE_QUERY = """
    SELECT air_date
    FROM series_episode_index
    WHERE series_id = :series_id
      AND season = :season
      AND episode = :episode
      AND (:min_timestamp IS NULL OR updated_at >= :min_timestamp)
"""

_SERIES_INDEX_MAX_UPDATED_AT_QUERY = """
    SELECT MAX(updated_at)
    FROM series_episode_index
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


def _normalize_air_date(raw_value) -> str | None:
    if not isinstance(raw_value, str) or not raw_value:
        return None

    candidate = raw_value.strip().split("T", 1)[0]
    try:
        datetime.strptime(candidate, "%Y-%m-%d")
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

    async def _is_series_index_fresh(
        self, series_id: str, min_timestamp: float
    ) -> bool:
        last_updated = await database.fetch_val(
            _SERIES_INDEX_MAX_UPDATED_AT_QUERY,
            {"series_id": series_id},
        )
        return last_updated is not None and float(last_updated) >= min_timestamp

    async def _upsert_series_air_dates(self, rows: list[dict]) -> None:
        if not rows:
            return
        await database.execute_many(_UPSERT_SERIES_EPISODE_INDEX_QUERY, rows)

    async def _refresh_from_cinemeta(self, series_id: str) -> None:
        try:
            async with self.session.get(
                _CINEMETA_SERIES_META_URL.format(series_id=series_id)
            ) as response:
                if response.status == 404:
                    return
                response.raise_for_status()
                payload = await response.json()
        except Exception as exc:
            logger.warning(
                f"EpisodeIndex: Failed to fetch Cinemeta episode data for {series_id}: {exc}"
            )
            return

        videos = payload.get("meta", {}).get("videos") or []
        if not isinstance(videos, list):
            return

        now = time.time()
        unique_rows: dict[tuple[int, int], dict] = {}
        for video in videos:
            if not isinstance(video, dict):
                continue

            season = video.get("season")
            episode = video.get("episode", video.get("number"))
            try:
                season_int = int(season)
                episode_int = int(episode)
            except (TypeError, ValueError):
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
            unique_rows[key] = {
                "series_id": series_id,
                "season": season_int,
                "episode": episode_int,
                "air_date": air_date,
                "updated_at": now,
            }

        await self._upsert_series_air_dates(list(unique_rows.values()))

    async def _refresh_single_episode_from_tmdb(
        self,
        series_id: str,
        season: int,
        episode: int,
    ) -> str | None:
        try:
            tmdb = TMDBApi(self.session)
            tmdb_id = await tmdb.get_tmdb_id_from_imdb(series_id)
            if not tmdb_id:
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
        except Exception as exc:
            logger.warning(
                "EpisodeIndex: Failed TMDB fallback for "
                f"{series_id} S{season:02d}E{episode:02d}: {exc}"
            )
            return None

    async def get_target_air_date(
        self,
        series_id: str,
        season: int | None,
        episode: int | None,
    ) -> str | None:
        if (
            not isinstance(series_id, str)
            or not series_id.startswith("tt")
            or season is None
            or episode is None
        ):
            return None

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
