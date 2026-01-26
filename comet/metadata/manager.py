import asyncio
import time

import aiohttp
import orjson

from comet.core.logger import logger
from comet.core.models import database, settings
from comet.services.anime import anime_mapper
from comet.utils.parsing import parse_media_id

from .imdb import get_imdb_metadata
from .kitsu import get_kitsu_metadata
from .trakt import get_trakt_aliases

_CACHE_SELECT_QUERY = """
    SELECT title, year, year_end, aliases
    FROM metadata_cache
    WHERE media_id = :media_id
    AND timestamp >= :min_timestamp
"""

_CACHE_INSERT_SQLITE = """
    INSERT OR IGNORE INTO metadata_cache
    VALUES (:media_id, :title, :year, :year_end, :aliases, :timestamp)
"""

_CACHE_INSERT_POSTGRESQL = """
    INSERT INTO metadata_cache
    VALUES (:media_id, :title, :year, :year_end, :aliases, :timestamp)
    ON CONFLICT DO NOTHING
"""


class MetadataScraper:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self._cache_insert_query = (
            _CACHE_INSERT_SQLITE
            if settings.DATABASE_TYPE == "sqlite"
            else _CACHE_INSERT_POSTGRESQL
        )

    async def get_from_cache_by_media_id(
        self, media_id: str, id: str, season: int | None, episode: int | None
    ):
        provider = self._extract_provider(media_id)
        cache_id = f"{provider}:{id}" if provider else id
        cache_season = 1 if provider == "kitsu" else season

        return await self.get_cached(cache_id, cache_season, episode)

    async def fetch_metadata_and_aliases(
        self,
        media_type: str,
        media_id: str,
        id: str | None = None,
        season: int | None = None,
        episode: int | None = None,
    ):
        if id is None:
            id, season, episode = parse_media_id(media_type, media_id)

        provider = self._extract_provider(media_id)
        cache_id = f"{provider}:{id}" if provider else id
        cache_season = 1 if provider == "kitsu" else season

        get_cached = await self.get_cached(cache_id, cache_season, episode)
        if get_cached is not None:
            return get_cached[0], get_cached[1]

        is_kitsu = provider == "kitsu"

        metadata_task = asyncio.create_task(
            self.get_metadata(id, season, episode, is_kitsu)
        )
        aliases_task = asyncio.create_task(self.get_aliases(media_type, id, provider))
        metadata, aliases = await asyncio.gather(metadata_task, aliases_task)

        if metadata is not None:
            await self.cache_metadata(cache_id, metadata, aliases)

        return metadata, aliases

    @staticmethod
    def _extract_provider(media_id: str):
        if media_id.startswith("tt"):
            return "imdb"

        first_part, sep, _ = media_id.partition(":")

        if sep:
            return first_part.lower()

        return None

    async def get_cached(self, media_id: str, season: int, episode: int):
        row = await database.fetch_one(
            _CACHE_SELECT_QUERY,
            {
                "media_id": media_id,
                "min_timestamp": time.time() - settings.METADATA_CACHE_TTL,
            },
        )
        if row is None:
            return None

        return (
            {
                "title": row["title"],
                "year": row["year"],
                "year_end": row["year_end"],
                "season": season,
                "episode": episode,
            },
            orjson.loads(row["aliases"]),
        )

    async def cache_metadata(self, media_id: str, metadata: dict, aliases: dict):
        await database.execute(
            self._cache_insert_query,
            {
                "media_id": media_id,
                "title": metadata["title"],
                "year": metadata["year"],
                "year_end": metadata["year_end"],
                "aliases": orjson.dumps(aliases).decode("utf-8"),
                "timestamp": time.time(),
            },
        )

    def normalize_metadata(self, metadata: dict, season: int, episode: int):
        if not metadata:
            return None

        title, year, year_end = metadata

        if title is None:  # metadata retrieving failed
            return None

        return {
            "title": title,
            "year": year,
            "year_end": year_end,
            "season": season,
            "episode": episode,
        }

    async def get_metadata(self, id: str, season: int, episode: int, is_kitsu: bool):
        if is_kitsu:
            raw_metadata = await get_kitsu_metadata(self.session, id)
            return self.normalize_metadata(raw_metadata, 1, episode)
        else:
            raw_metadata = await get_imdb_metadata(self.session, id)
            return self.normalize_metadata(raw_metadata, season, episode)

    async def fetch_aliases_with_metadata(
        self,
        media_type: str,
        media_id: str,
        title: str,
        year: int,
        year_end: int = None,
        id: str | None = None,
    ):
        """
        Fetch only aliases for media when we already have the metadata from another source.
        This method will cache the provided metadata along with the scraped aliases.
        """
        if id is None:
            id, _, _ = parse_media_id(media_type, media_id)

        provider = self._extract_provider(media_id)
        cache_id = f"{provider}:{id}" if provider else id

        get_cached = await self.get_cached(cache_id, 1, 1)
        if get_cached is not None:
            return get_cached[0], get_cached[1]

        metadata = {
            "title": title,
            "year": year,
            "year_end": year_end,
        }

        aliases = await self.get_aliases(media_type, id, provider)

        await self.cache_metadata(cache_id, metadata, aliases)

        return metadata, aliases

    async def get_aliases(
        self,
        media_type: str,
        media_id: str,
        provider: str | None = None,
    ):
        if anime_mapper.is_loaded():
            full_media_id = f"{provider}:{media_id}"

            if anime_mapper.is_anime_content(full_media_id, media_id):
                aliases = await anime_mapper.get_aliases(full_media_id)
                logger.log(
                    "SCRAPER",
                    f"📜 Found {len(aliases.get('ez', []))} Anime title aliases for {media_id}",
                )
                if aliases:
                    return aliases

        if provider == "kitsu":
            logger.log("SCRAPER", f"📜 No Anime title aliases found for {media_id}")
            return {}

        trakt_aliases = await get_trakt_aliases(self.session, media_type, media_id)
        if trakt_aliases:
            total_aliases = sum(len(titles) for titles in trakt_aliases.values())
            logger.log(
                "SCRAPER",
                f"📜 Found {total_aliases} Trakt title aliases for {media_id}",
            )
        else:
            logger.log("SCRAPER", f"📜 No Trakt title aliases found for {media_id}")

        return trakt_aliases
