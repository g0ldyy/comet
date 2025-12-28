import time
from datetime import datetime

from comet.core.logger import logger
from comet.core.models import database, settings
from comet.metadata.tmdb import TMDBApi


class DigitalReleaseFilter:
    async def check_is_released(
        self,
        session,
        media_type: str,
        media_id: str,
        season: int = None,
        episode: int = None,
    ):
        if not settings.DIGITAL_RELEASE_FILTER:
            return True

        try:
            cached_date = await database.fetch_val(
                """
                SELECT release_date FROM digital_release_cache 
                WHERE media_id = :media_id
                AND timestamp + :cache_ttl >= :current_time
                """,
                {
                    "media_id": media_id,
                    "cache_ttl": settings.METADATA_CACHE_TTL,
                    "current_time": time.time(),
                },
            )

            if cached_date is not None:
                return self._is_released(cached_date)

            tmdb_id = None

            tmdb = TMDBApi(session)

            if media_id.startswith("tt"):
                imdb_id = media_id.split(":")[0]
                tmdb_id = await tmdb.get_tmdb_id_from_imdb(imdb_id)
            else:
                # Other formats (e.g. kitsu) are not supported
                return True

            if not tmdb_id:
                logger.warning(
                    f"DigitalReleaseFilter: Could not resolve {media_id} to TMDB ID. Allowing search."
                )
                return True

            release_date_str = None
            if media_type == "movie":
                release_date_str = await tmdb.get_upcoming_movie_release_date(tmdb_id)
            elif media_type == "series":
                release_date_str = await tmdb.get_episode_air_date(
                    tmdb_id, season, episode
                )

            cache_timestamp = int(time.time())
            if release_date_str is None:
                # Not found, treat as released in far future to block
                release_date_timestamp = 253402300799  # 9999-12-31
                # Cache for only 1 day (86400s) to recheck later
                if settings.METADATA_CACHE_TTL > 86400:
                    cache_timestamp = int(
                        time.time() - settings.METADATA_CACHE_TTL + 86400
                    )
            else:
                release_date_timestamp = int(
                    datetime.strptime(release_date_str, "%Y-%m-%d").timestamp()
                )

            await database.execute(
                """
                INSERT INTO digital_release_cache (media_id, release_date, timestamp)
                VALUES (:media_id, :release_date, :timestamp)
                ON CONFLICT (media_id) DO UPDATE SET release_date = :release_date, timestamp = :timestamp
                """,
                {
                    "media_id": media_id,
                    "release_date": release_date_timestamp,
                    "timestamp": cache_timestamp,
                },
            )

            return self._is_released(release_date_timestamp)
        except Exception as e:
            logger.error(
                f"DigitalReleaseFilter: Error checking release status for {media_id}: {e}"
            )
            return True

    def _is_released(self, release_timestamp: float):
        if release_timestamp is None:
            return True
        return release_timestamp <= time.time()


release_filter = DigitalReleaseFilter()
