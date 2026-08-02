import math
import time
from datetime import UTC, datetime

from comet.core.models import database, settings
from comet.metadata.episode_index import EpisodeIndexService
from comet.metadata.tmdb import TMDBApi


class DigitalReleaseFilter:
    async def check_is_released(
        self,
        session,
        media_type: str,
        media_id: str,
        season: int | None = None,
        episode: int | None = None,
    ):
        if (
            media_type not in {"movie", "series"}
            or not isinstance(media_id, str)
            or not media_id.startswith("tt")
        ):
            return True

        cached_date = await database.fetch_val(
            """
            SELECT release_date FROM media_metadata_cache
            WHERE media_id = :media_id
            AND release_updated_at >= :min_timestamp
            """,
            {
                "media_id": media_id,
                "min_timestamp": time.time() - settings.METADATA_CACHE_TTL,
            },
        )

        if self._release_timestamp(cached_date) is not None:
            return self._is_released(cached_date)

        release_date_str = None
        imdb_id = media_id.partition(":")[0]
        if media_type == "movie":
            tmdb = TMDBApi(session)
            tmdb_id = await tmdb.get_tmdb_id_from_imdb(imdb_id, "movie")
            if not tmdb_id:
                return True
            release_date_str = await tmdb.get_upcoming_movie_release_date(tmdb_id)
            if not release_date_str:
                # Check watch providers as fallback
                has_watch_providers = await tmdb.has_watch_providers(tmdb_id)
                if has_watch_providers is True:
                    # Treat as released a long time ago
                    release_date_str = "1970-01-01"
                elif has_watch_providers is None:
                    return True

        elif media_type == "series":
            release_date_str = await EpisodeIndexService(session).get_target_air_date(
                imdb_id, season, episode
            )
            if not release_date_str:
                return True

        cache_timestamp = int(time.time())
        if release_date_str is None:
            # Not found, treat as released in far future to block
            release_date_timestamp = 253402300799  # 9999-12-31
            # Cache for only 1 day (86400s) to recheck later
            if settings.METADATA_CACHE_TTL > 86400:
                cache_timestamp = int(time.time() - settings.METADATA_CACHE_TTL + 86400)
        else:
            release_date_timestamp = int(
                datetime.strptime(release_date_str, "%Y-%m-%d")
                .replace(tzinfo=UTC)
                .timestamp()
            )

        await database.execute(
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
            {
                "media_id": media_id,
                "release_date": release_date_timestamp,
                "release_updated_at": cache_timestamp,
            },
        )

        return self._is_released(release_date_timestamp)

    @staticmethod
    def _release_timestamp(value: object) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        parsed = float(value)
        return parsed if math.isfinite(parsed) and 0 <= parsed <= 253402300799 else None

    def _is_released(self, release_timestamp: float):
        release_timestamp = self._release_timestamp(release_timestamp)
        if release_timestamp is None:
            return True
        return release_timestamp <= time.time()


release_filter = DigitalReleaseFilter()
