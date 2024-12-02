import aiohttp
import asyncio
import time
import orjson

from RTN.patterns import normalize_title

from comet.utils.models import database, settings

from .kitsu import get_kitsu_metadata
from .imdb import get_imdb_metadata
from .trakt import get_trakt_aliases


class MetadataScraper:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def fetch_metadata_and_aliases(self, media_type: str, media_id: str):
        id, season, episode = self._parse_media_id(media_type, media_id)

        real_id = id if id != "kitsu" else season

        get_cached = await self._get_cached(
            real_id, season if id != "kitsu" else 1, episode
        )
        if get_cached is not None:
            return get_cached[0], get_cached[1]

        metadata_task = asyncio.create_task(self._get_metadata(id, season, episode))
        aliases_task = asyncio.create_task(self._get_aliases(media_type, id))
        metadata, aliases = await asyncio.gather(metadata_task, aliases_task)
        await self._cache_metadata(real_id, metadata, aliases)

        return metadata, aliases

    async def _get_cached(self, media_id: str, season: int, episode: int):
        row = await database.fetch_one(
            """
                SELECT title, year, year_end, aliases
                FROM metadata_cache
                WHERE media_id = :media_id
                AND timestamp + :cache_ttl >= :current_time
            """,
            {
                "media_id": media_id,
                "cache_ttl": settings.CACHE_TTL,
                "current_time": time.time(),
            },
        )
        if row is not None:
            metadata = {
                "title": row["title"],
                "year": row["year"],
                "year_end": row["year_end"],
                "season": season,
                "episode": episode,
            }
            return metadata, orjson.loads(row["aliases"])

        return None

    async def _cache_metadata(self, media_id: str, metadata: dict, aliases: dict):
        await database.execute(
            f"""
                INSERT {'OR IGNORE ' if settings.DATABASE_TYPE == 'sqlite' else ''}INTO metadata_cache (media_id, title, year, year_end, aliases, timestamp)
                VALUES (:media_id, :title, :year, :year_end, :aliases, :timestamp){' ON CONFLICT DO NOTHING' if settings.DATABASE_TYPE == 'postgresql' else ''}
            """,
            {
                "media_id": media_id,
                "title": metadata["title"],
                "year": metadata["year"],
                "year_end": metadata["year_end"],
                "aliases": orjson.dumps(aliases),
                "timestamp": time.time(),
            },
        )

    def _parse_media_id(self, media_type: str, media_id: str) -> tuple:
        if media_type == "series":
            info = media_id.split(":")
            return info[0], int(info[1]), int(info[2])
        return media_id, None, None

    def _normalize_metadata(self, metadata: dict, season: int, episode: int):
        title, year, year_end = metadata

        if title is None:  # metadata retrieving failed
            return None

        return {
            "title": normalize_title(title),
            "year": year,
            "year_end": year_end,
            "season": season,
            "episode": episode,
        }

    async def _get_metadata(self, id: str, season: int, episode: int):
        if id == "kitsu":
            raw_metadata = await get_kitsu_metadata(self.session, season)
            return self._normalize_metadata(raw_metadata, 1, episode)
        else:
            raw_metadata = await get_imdb_metadata(self.session, id)
            return self._normalize_metadata(raw_metadata, season, episode)

    async def _get_aliases(self, media_type: str, media_id: str):
        if media_id == "kitsu":
            return {}
        return await get_trakt_aliases(self.session, media_type, media_id)
