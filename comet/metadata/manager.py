import asyncio
import time

import aiohttp
import orjson

from comet.core.models import database, settings
from comet.services.anime import anime_mapper
from comet.utils.parsing import parse_media_id

from .imdb import get_imdb_metadata
from .kitsu import get_kitsu_aliases, get_kitsu_metadata
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
            return metadata, orjson.loads(row["aliases"])

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
            "title": title,
            "year": year,
            "year_end": year_end,
        }

        is_kitsu = "kitsu" in media_id
        aliases = await self.get_aliases(media_type, id, is_kitsu)

        await self.cache_metadata(id, metadata, aliases)

        return metadata, aliases

    def combine_aliases(self, kitsu_aliases: dict, trakt_aliases: dict):
        combined = {"ez": []}

        # Add Kitsu aliases
        if kitsu_aliases and "ez" in kitsu_aliases:
            combined["ez"].extend(kitsu_aliases["ez"])

        # Add Trakt aliases
        if trakt_aliases and "ez" in trakt_aliases:
            combined["ez"].extend(trakt_aliases["ez"])

        # Case-insensitive deduplication
        combined["ez"] = list(
            {alias.lower(): alias for alias in combined["ez"]}.values()
        )

        return combined if combined["ez"] else {}

    async def get_aliases(self, media_type: str, media_id: str, is_kitsu: bool):
        if not anime_mapper.is_loaded():
            # Fallback to original behavior if mapping not loaded
            if is_kitsu:
                return await get_kitsu_aliases(self.session, media_id)
            return await get_trakt_aliases(self.session, media_type, media_id)

        kitsu_aliases = {}
        trakt_aliases = {}

        if is_kitsu:
            # Get Kitsu aliases
            kitsu_aliases = await get_kitsu_aliases(self.session, media_id)

            # Try to convert Kitsu ID to IMDB ID for Trakt aliases
            try:
                kitsu_id = int(media_id)
                imdb_id = anime_mapper.get_imdb_from_kitsu(kitsu_id)
                if imdb_id:
                    # We have an IMDB ID, get Trakt aliases too
                    trakt_aliases = await get_trakt_aliases(
                        self.session, media_type, imdb_id
                    )
            except Exception:
                pass
        else:
            # Get Trakt aliases for IMDB ID
            trakt_aliases = await get_trakt_aliases(self.session, media_type, media_id)

            # Check if this IMDB ID has a Kitsu equivalent for additional aliases
            kitsu_id = anime_mapper.get_kitsu_from_imdb(media_id)
            if kitsu_id:
                kitsu_aliases = await get_kitsu_aliases(self.session, kitsu_id)

        # Combine the aliases from both sources
        return self.combine_aliases(kitsu_aliases, trakt_aliases)
