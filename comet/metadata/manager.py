import aiohttp
import asyncio
import time
import orjson

from RTN.patterns import normalize_title

from comet.utils.models import database, settings, redis_client
from comet.utils.general import parse_media_id

from .kitsu import get_kitsu_metadata, get_kitsu_aliases
from .imdb import get_imdb_metadata
from .trakt import get_trakt_aliases


class MetadataScraper:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def fetch_metadata_and_aliases(self, media_type: str, media_id: str):
        id, season, episode = parse_media_id(media_type, media_id)

        get_cached = await self.get_cached(
            id, season if "kitsu" not in media_id else 1, episode
        )
        if get_cached is not None:
            return get_cached[0], get_cached[1]

        is_kitsu = "kitsu" in media_id
        metadata_task = asyncio.create_task(
            self.get_metadata(id, season, episode, is_kitsu)
        )
        aliases_task = asyncio.create_task(self.get_aliases(media_type, id, is_kitsu))
        metadata, aliases = await asyncio.gather(metadata_task, aliases_task)

        if metadata is not None:
            await self.cache_metadata(id, metadata, aliases)

        return metadata, aliases

    async def get_cached(self, media_id: str, season: int, episode: int):
        if redis_client and redis_client.is_connected():
            redis_key = f"metadata:{media_id}"
            cached_data = await redis_client.get(redis_key)
            if cached_data:
                try:
                    metadata_data = orjson.loads(cached_data) if isinstance(cached_data, str) else cached_data
                    metadata = {
                        "title": metadata_data["title"],
                        "year": metadata_data["year"],
                        "year_end": metadata_data["year_end"],
                        "season": season,
                        "episode": episode,
                    }
                    return metadata, metadata_data["aliases"]
                except (KeyError, orjson.JSONDecodeError):
                    pass
        
        row = await database.fetch_one(
            """
                SELECT title, year, year_end, aliases
                FROM metadata_cache
                WHERE media_id = :media_id
                AND timestamp + :cache_ttl >= :current_time
            """,
            {
                "media_id": media_id,
                "cache_ttl": settings.METADATA_CACHE_TTL,
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
            aliases = orjson.loads(row["aliases"])
            
            if redis_client and redis_client.is_connected():
                redis_key = f"metadata:{media_id}"
                cache_data = {
                    "title": row["title"],
                    "year": row["year"],
                    "year_end": row["year_end"],
                    "aliases": aliases
                }
                await redis_client.set(redis_key, cache_data, settings.METADATA_CACHE_TTL)
            
            return metadata, aliases

        return None

    async def cache_metadata(self, media_id: str, metadata: dict, aliases: dict):
        await database.execute(
            f"""
                INSERT {"OR IGNORE " if settings.DATABASE_TYPE == "sqlite" else ""}
                INTO metadata_cache
                VALUES (:media_id, :title, :year, :year_end, :aliases, :timestamp)
                {" ON CONFLICT DO NOTHING" if settings.DATABASE_TYPE == "postgresql" else ""}
            """,
            {
                "media_id": media_id,
                "title": metadata["title"],
                "year": metadata["year"],
                "year_end": metadata["year_end"],
                "aliases": orjson.dumps(aliases).decode("utf-8"),
                "timestamp": time.time(),
            },
        )
        
        if redis_client and redis_client.is_connected():
            redis_key = f"metadata:{media_id}"
            cache_data = {
                "title": metadata["title"],
                "year": metadata["year"],
                "year_end": metadata["year_end"],
                "aliases": aliases
            }
            await redis_client.set(redis_key, cache_data, settings.METADATA_CACHE_TTL)

    def normalize_metadata(self, metadata: dict, season: int, episode: int):
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
    ):
        """
        Fetch only aliases for media when we already have the metadata from another source.
        This method will cache the provided metadata along with the scraped aliases.
        """
        id, _, _ = parse_media_id(media_type, media_id)

        get_cached = await self.get_cached(id, 1, 1)
        if get_cached is not None:
            return get_cached[0], get_cached[1]

        metadata = {
            "title": normalize_title(title),
            "year": year,
            "year_end": year_end,
        }

        is_kitsu = "kitsu" in media_id
        aliases = await self.get_aliases(media_type, id, is_kitsu)

        await self.cache_metadata(id, metadata, aliases)

        return metadata, aliases

    async def get_aliases(self, media_type: str, media_id: str, is_kitsu: bool):
        if is_kitsu:
            return await get_kitsu_aliases(self.session, media_id)

        return await get_trakt_aliases(self.session, media_type, media_id)
