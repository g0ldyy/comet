import asyncio
import time
from dataclasses import dataclass
from enum import StrEnum
from weakref import WeakValueDictionary

import aiohttp
import orjson

from comet.core.database import database, encode_json_param
from comet.core.models import settings
from comet.metadata.http import MetadataHttpError
from comet.metadata.validation import metadata_text, metadata_year, normalize_aliases
from comet.services.anime import anime_mapper
from comet.utils.languages import merge_aliases
from comet.utils.parsing import parse_media_id

from .imdb import get_imdb_metadata
from .kitsu import get_kitsu_metadata
from .tmdb import TMDBApi

_CACHE_SELECT_QUERY = """
    SELECT
        title,
        year,
        year_end,
        aliases_json,
        metadata_updated_at,
        aliases_updated_at
    FROM media_metadata_cache
    WHERE media_id = :media_id
"""

_CACHE_UPSERT_QUERY = """
    INSERT INTO media_metadata_cache (
        media_id,
        title,
        year,
        year_end,
        aliases_json,
        metadata_updated_at,
        aliases_updated_at
    )
    VALUES (
        :media_id,
        :title,
        :year,
        :year_end,
        :aliases_json,
        :metadata_updated_at,
        :aliases_updated_at
    )
    ON CONFLICT (media_id) DO UPDATE SET
        title = CASE
            WHEN :update_metadata
            THEN EXCLUDED.title
            ELSE media_metadata_cache.title
        END,
        year = CASE
            WHEN :update_metadata
            THEN EXCLUDED.year
            ELSE media_metadata_cache.year
        END,
        year_end = CASE
            WHEN :update_metadata
            THEN EXCLUDED.year_end
            ELSE media_metadata_cache.year_end
        END,
        metadata_updated_at = CASE
            WHEN :update_metadata
            THEN EXCLUDED.metadata_updated_at
            ELSE media_metadata_cache.metadata_updated_at
        END,
        aliases_json = CASE
            WHEN :update_aliases AND EXCLUDED.aliases_json IS NOT NULL
            THEN EXCLUDED.aliases_json
            ELSE media_metadata_cache.aliases_json
        END,
        aliases_updated_at = CASE
            WHEN :update_aliases
            THEN EXCLUDED.aliases_updated_at
            ELSE media_metadata_cache.aliases_updated_at
        END
    RETURNING
        title,
        year,
        year_end,
        aliases_json,
        metadata_updated_at,
        aliases_updated_at
"""

_ALIAS_FAILURE_RETRY_DELAY = 300


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    metadata: dict | None
    aliases: dict | None


class MetadataFetchStatus(StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"
    UNSUPPORTED = "unsupported"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    TIMEOUT = "timeout"
    INVALID_RESPONSE = "invalid_response"


@dataclass(frozen=True, slots=True)
class MetadataFetchResult:
    status: MetadataFetchStatus
    metadata: dict | None
    aliases: dict


@dataclass(frozen=True, slots=True)
class _MetadataValue:
    status: MetadataFetchStatus
    metadata: dict | None


_metadata_refresh_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


def _get_metadata_refresh_lock(cache_id: str) -> asyncio.Lock:
    lock = _metadata_refresh_locks.get(cache_id)
    if lock is None:
        lock = asyncio.Lock()
        _metadata_refresh_locks[cache_id] = lock
    return lock


def _alias_cache_timestamp(current_time: float, succeeded: bool) -> float:
    if succeeded:
        return current_time

    cache_ttl = max(settings.METADATA_CACHE_TTL, 0)
    retry_delay = min(_ALIAS_FAILURE_RETRY_DELAY, cache_ttl)
    return current_time - (cache_ttl - retry_delay)


async def _settle_metadata_tasks(tasks: dict[str, asyncio.Task]) -> dict[str, object]:
    if not tasks:
        return {}
    values = await asyncio.gather(*tasks.values())
    return dict(zip(tasks, values, strict=True))


class MetadataScraper:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def fetch_metadata_and_aliases(
        self,
        media_type: str,
        media_id: str,
        id: str | None = None,
        season: int | None = None,
        episode: int | None = None,
    ) -> MetadataFetchResult:
        if id is None:
            id, season, episode = parse_media_id(media_type, media_id)

        provider = self._extract_provider(media_id)
        cache_id = f"{provider}:{id}" if provider else id
        cache_season = 1 if provider == "kitsu" else season
        return await self._fetch_cached(
            cache_id=cache_id,
            media_type=media_type,
            media_id=id,
            provider=provider,
            season=cache_season,
            episode=episode,
        )

    @staticmethod
    def _extract_provider(media_id: str):
        if media_id.startswith("tt"):
            return "imdb"

        first_part, sep, _ = media_id.partition(":")

        if sep:
            return first_part.lower()

        return None

    @staticmethod
    def _load_cached_aliases(aliases_json) -> dict:
        return normalize_aliases(orjson.loads(aliases_json))

    @staticmethod
    def _is_fresh(timestamp, current_time: float) -> bool:
        return timestamp is not None and timestamp >= (
            current_time - settings.METADATA_CACHE_TTL
        )

    def _build_cache_entry(
        self,
        row,
        season: int | None,
        episode: int | None,
        current_time: float,
    ) -> _CacheEntry:
        metadata = None
        title = row["title"]
        if title and self._is_fresh(row["metadata_updated_at"], current_time):
            year = row["year"]
            year_end = row["year_end"]
            if year is not None and year_end is not None and year_end < year:
                year_end = None
            metadata = {
                "title": title,
                "year": year,
                "year_end": year_end,
                "season": season,
                "episode": episode,
            }

        aliases = None
        if self._is_fresh(row["aliases_updated_at"], current_time):
            if row["aliases_json"] is None:
                aliases = {}
            else:
                aliases = self._load_cached_aliases(row["aliases_json"])

        return _CacheEntry(metadata=metadata, aliases=aliases)

    async def get_cached(
        self,
        media_id: str,
        season: int | None,
        episode: int | None,
    ):
        row = await database.fetch_one(
            _CACHE_SELECT_QUERY,
            {"media_id": media_id},
        )
        if row is None:
            return _CacheEntry(metadata=None, aliases=None)

        return self._build_cache_entry(row, season, episode, time.time())

    async def cache_metadata(
        self,
        media_id: str,
        metadata: dict | None,
        aliases: dict | None,
        *,
        update_metadata: bool,
        update_aliases: bool,
        season: int | None,
        episode: int | None,
    ) -> _CacheEntry:
        metadata = self._normalize_metadata_dict(metadata, season, episode)
        if aliases is not None:
            aliases = normalize_aliases(aliases)
        current_time = time.time()
        aliases_updated_at = _alias_cache_timestamp(
            current_time,
            aliases is not None,
        )

        params = {
            "media_id": media_id,
            "title": metadata["title"] if metadata is not None else None,
            "year": metadata["year"] if metadata is not None else None,
            "year_end": metadata["year_end"] if metadata is not None else None,
            "aliases_json": (
                encode_json_param(aliases) if aliases is not None else None
            ),
            "metadata_updated_at": current_time if update_metadata else None,
            "aliases_updated_at": aliases_updated_at if update_aliases else None,
            "update_metadata": update_metadata,
            "update_aliases": update_aliases,
        }
        row = await database.fetch_one(
            _CACHE_UPSERT_QUERY,
            params,
            force_primary=True,
        )
        if row is None:
            return _CacheEntry(
                metadata=metadata,
                aliases=aliases if aliases is not None else {},
            )
        return self._build_cache_entry(row, season, episode, current_time)

    async def _fetch_cached(
        self,
        *,
        cache_id: str,
        media_type: str,
        media_id: str,
        provider: str | None,
        season: int | None,
        episode: int | None,
        provided_metadata: dict | None = None,
    ):
        cached = await self.get_cached(cache_id, season, episode)
        if cached.metadata is not None and cached.aliases is not None:
            return MetadataFetchResult(
                MetadataFetchStatus.OK,
                cached.metadata,
                cached.aliases,
            )

        async with _get_metadata_refresh_lock(cache_id):
            cached = await self.get_cached(cache_id, season, episode)
            if cached.metadata is not None and cached.aliases is not None:
                return MetadataFetchResult(
                    MetadataFetchStatus.OK,
                    cached.metadata,
                    cached.aliases,
                )

            metadata = cached.metadata
            aliases = cached.aliases
            update_metadata = False
            metadata_status = (
                MetadataFetchStatus.OK
                if metadata is not None
                else MetadataFetchStatus.NOT_FOUND
            )

            pending = {}
            if metadata is None:
                if provided_metadata is not None:
                    metadata = provided_metadata
                    metadata_status = MetadataFetchStatus.OK
                    update_metadata = True
                else:
                    pending["metadata"] = asyncio.create_task(
                        self._get_metadata_value(
                            media_id,
                            season,
                            episode,
                            provider == "kitsu",
                            media_type,
                        )
                    )
            if aliases is None:
                pending["aliases"] = asyncio.create_task(
                    self.get_aliases(media_type, media_id, provider)
                )

            settled = await _settle_metadata_tasks(pending)
            if "metadata" in settled:
                metadata_value = settled["metadata"]
                metadata_status = metadata_value.status
                metadata = metadata_value.metadata
                update_metadata = metadata is not None
            if "aliases" in settled:
                aliases = settled["aliases"]

            update_aliases = "aliases" in pending
            if update_metadata or update_aliases:
                refreshed = await self.cache_metadata(
                    cache_id,
                    metadata,
                    aliases,
                    update_metadata=update_metadata,
                    update_aliases=update_aliases,
                    season=season,
                    episode=episode,
                )
                metadata = refreshed.metadata
                aliases = refreshed.aliases

            return MetadataFetchResult(
                (MetadataFetchStatus.OK if metadata is not None else metadata_status),
                metadata,
                aliases if aliases is not None else {},
            )

    async def _get_metadata_value(
        self,
        media_id: str,
        season: int | None,
        episode: int | None,
        is_kitsu: bool,
        media_type: str,
    ) -> _MetadataValue:
        provider = "kitsu" if is_kitsu else "imdb"
        if media_type not in {"movie", "series"} or (
            provider == "kitsu" and media_type != "series"
        ):
            return _MetadataValue(MetadataFetchStatus.UNSUPPORTED, None)
        try:
            metadata = await self.get_metadata(
                media_id,
                season,
                episode,
                is_kitsu,
                media_type,
            )
        except TimeoutError:
            return _MetadataValue(MetadataFetchStatus.TIMEOUT, None)
        except MetadataHttpError:
            return _MetadataValue(MetadataFetchStatus.INVALID_RESPONSE, None)
        return _MetadataValue(
            (
                MetadataFetchStatus.OK
                if metadata is not None
                else MetadataFetchStatus.NOT_FOUND
            ),
            metadata,
        )

    def normalize_metadata(
        self,
        metadata,
        season: int | None,
        episode: int | None,
    ):
        if not metadata:
            return None

        title, year, year_end = metadata
        return self._normalize_metadata_dict(
            {
                "title": title,
                "year": year,
                "year_end": year_end,
            },
            season,
            episode,
        )

    @staticmethod
    def _normalize_metadata_dict(
        metadata: dict | None,
        season: int | None,
        episode: int | None,
    ) -> dict | None:
        if metadata is None:
            return None
        title = metadata_text(metadata.get("title"))
        if title is None:
            return None
        year = metadata_year(metadata.get("year"))
        year_end = metadata_year(metadata.get("year_end"))
        if year is not None and year_end is not None and year_end < year:
            year_end = None
        return {
            "title": title,
            "year": year,
            "year_end": year_end,
            "season": season,
            "episode": episode,
        }

    async def get_metadata(
        self,
        id: str,
        season: int | None,
        episode: int | None,
        is_kitsu: bool,
        media_type: str,
    ):
        if is_kitsu:
            raw_metadata = await get_kitsu_metadata(self.session, id)
            return self.normalize_metadata(raw_metadata, 1, episode)
        else:
            raw_metadata = await get_imdb_metadata(self.session, id, media_type)
            return self.normalize_metadata(raw_metadata, season, episode)

    async def fetch_aliases_with_metadata(
        self,
        media_type: str,
        media_id: str,
        title: str,
        year: int,
        year_end: int | None = None,
        id: str | None = None,
    ):
        if id is None:
            id, _, _ = parse_media_id(media_type, media_id)

        provider = self._extract_provider(media_id)
        cache_id = f"{provider}:{id}" if provider else id
        return await self._fetch_cached(
            cache_id=cache_id,
            media_type=media_type,
            media_id=id,
            provider=provider,
            season=1,
            episode=1,
            provided_metadata={
                "title": title,
                "year": year,
                "year_end": year_end,
                "season": 1,
                "episode": 1,
            },
        )

    async def get_aliases(
        self,
        media_type: str,
        media_id: str,
        provider: str | None = None,
    ) -> dict[str, list[str]] | None:
        anime_aliases = {}
        anime_mapping_loaded = anime_mapper.is_loaded()
        full_media_id = f"{provider}:{media_id}"
        is_anime = anime_mapping_loaded and anime_mapper.is_anime_content(
            full_media_id, media_id
        )

        pending = {}
        if is_anime:
            pending["anime"] = asyncio.create_task(
                anime_mapper.get_aliases(full_media_id)
            )
        if provider == "kitsu":
            if anime_mapping_loaded:
                pending["imdb_id"] = asyncio.create_task(
                    anime_mapper.get_imdb_from_kitsu(media_id)
                )
        else:
            pending["tmdb"] = asyncio.create_task(
                TMDBApi(self.session).get_title_aliases(media_type, media_id)
            )

        settled = await _settle_metadata_tasks(pending)

        if "anime" in settled:
            anime_aliases = normalize_aliases(settled["anime"])

        if provider == "kitsu":
            imdb_id = settled.get("imdb_id")
            if imdb_id is None:
                return anime_aliases
            tmdb_aliases = await TMDBApi(self.session).get_title_aliases(
                media_type, imdb_id
            )
        else:
            tmdb_aliases = settled["tmdb"]

        if tmdb_aliases is None:
            return None

        return normalize_aliases(merge_aliases(anime_aliases, tmdb_aliases))
