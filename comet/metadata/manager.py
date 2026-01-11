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
    def __init__(self, session: aiohttp.ClientSession, config: dict = None):
        self.session = session
        self.config = config or {}
        self._cache_insert_query = (
            _CACHE_INSERT_SQLITE
            if settings.DATABASE_TYPE == "sqlite"
            else _CACHE_INSERT_POSTGRESQL
        )

    async def get_from_cache_by_media_id(
        self, media_id: str, id: str, season: int | None, episode: int | None
    ):
        provider = self._extract_provider(media_id)
        # For custom providers, use the full ID with prefix as cache_id
        if provider and provider.startswith("custom:"):
            cache_id = id  # id already contains prefix like "kbx3585128"
        else:
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
        # For custom providers, use the full ID with prefix as cache_id
        if provider and provider.startswith("custom:"):
            cache_id = id  # id already contains prefix like "kbx3585128"
        else:
            cache_id = f"{provider}:{id}" if provider else id
        cache_season = 1 if provider == "kitsu" else season

        get_cached = await self.get_cached(cache_id, cache_season, episode)
        if get_cached is not None:
            return get_cached[0], get_cached[1]

        is_kitsu = provider == "kitsu"
        is_custom = provider and provider.startswith("custom:")

        metadata_task = asyncio.create_task(
            self.get_metadata(id, season, episode, is_kitsu, provider)
        )
        # Skip aliases for custom providers
        if is_custom:
            aliases_task = asyncio.create_task(asyncio.sleep(0, result={}))
        else:
            aliases_task = asyncio.create_task(self.get_aliases(media_type, id, provider))
        metadata, aliases = await asyncio.gather(metadata_task, aliases_task)

        if metadata is not None:
            await self.cache_metadata(cache_id, metadata, aliases)

        return metadata, aliases

    def _extract_provider(self, media_id: str):
        if media_id.startswith("tt"):
            return "imdb"
        
        # Check custom metadata providers FIRST (before partition)
        metadata_providers = self.config.get("metadataProviders", [])
        for provider in metadata_providers:
            prefix = provider.get("prefix", "")
            if prefix and media_id.startswith(prefix):
                # Extract just the numeric part after prefix
                # e.g. "kbx3585128:1:8" -> check if starts with "kbx"
                after_prefix = media_id[len(prefix):]
                # Verify the character after prefix is a digit (not another letter)
                if after_prefix and (after_prefix[0].isdigit() or after_prefix[0] == ':'):
                    return f"custom:{prefix}"

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

    async def get_metadata(self, id: str, season: int, episode: int, is_kitsu: bool, provider: str = None):
        # Handle custom metadata providers
        if provider and provider.startswith("custom:"):
            # Extract the prefix from provider string (format: "custom:prefix")
            prefix = provider.split(":", 1)[1]
            
            logger.log("SCRAPER", f"🔍 Custom provider detected: prefix={prefix}, id={id}, season={season}, episode={episode}")
            
            # Find the provider configuration
            metadata_providers = self.config.get("metadataProviders", [])
            provider_config = None
            for p in metadata_providers:
                if p.get("prefix") == prefix:
                    provider_config = p
                    break
            
            if provider_config:
                logger.log("SCRAPER", f"✅ Found provider config: {provider_config}")
                # Build the full media_id for the meta endpoint
                media_id = id  # id already contains the full ID like "kbx3585128"
                if season is not None and episode is not None:
                    media_id = f"{id}:{season}:{episode}"
                elif season is not None:
                    media_id = f"{id}:{season}"
                
                # Determine media type based on season/episode
                media_type = "series" if season is not None else "movie"
                
                # Fetch metadata from custom provider
                provider_url = provider_config.get("url", "").rstrip("/")
                meta_url = f"{provider_url}/meta/{media_type}/{media_id}.json"
                
                logger.log("SCRAPER", f"📡 Fetching metadata from: {meta_url}")
                
                try:
                    from comet.core.constants import CATALOG_TIMEOUT
                    async with self.session.get(meta_url, timeout=CATALOG_TIMEOUT) as response:
                        if response.status == 200:
                            data = await response.json()
                            meta = data.get("meta", {})
                            
                            # Extract metadata from the response
                            title = meta.get("name") or meta.get("title") or id
                            
                            # Try to parse year from releaseInfo
                            year = None
                            year_end = None
                            release_info = meta.get("releaseInfo") or meta.get("year")
                            if release_info:
                                if isinstance(release_info, str):
                                    # Handle formats like "2025", "2016–2025"
                                    if "–" in release_info or "-" in release_info:
                                        parts = release_info.replace("–", "-").split("-")
                                        try:
                                            year = int(parts[0].strip())
                                            if len(parts) > 1 and parts[1].strip():
                                                year_end = int(parts[1].strip())
                                        except ValueError:
                                            pass
                                    else:
                                        try:
                                            year = int(release_info[:4])
                                        except ValueError:
                                            pass
                                elif isinstance(release_info, int):
                                    year = release_info
                            
                            logger.log("SCRAPER", f"✅ Successfully fetched metadata: title={title}, year={year}")
                            return {
                                "title": title,
                                "year": year,
                                "year_end": year_end,
                                "season": season,
                                "episode": episode,
                            }
                        else:
                            logger.warning(f"Custom provider returned status {response.status} for {meta_url}")
                except Exception as e:
                    logger.warning(f"Failed to fetch metadata from custom provider {prefix}: {e}")
            
            # Fallback to placeholder if provider not found or request failed
            logger.warning(f"Using fallback placeholder metadata for {id}")
            return {
                "title": id,
                "year": None,
                "year_end": None,
                "season": season,
                "episode": episode,
            }
        
        logger.log("SCRAPER", f"🔍 Standard provider: provider={provider}, id={id}, is_kitsu={is_kitsu}")
        
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
            logger.log(
                "SCRAPER",
                f"📜 Found {len(trakt_aliases['ez'])} Trakt title aliases for {media_id}",
            )
        else:
            logger.log("SCRAPER", f"📜 No Trakt title aliases found for {media_id}")

        return trakt_aliases
