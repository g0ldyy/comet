import asyncio
import ctypes
import gc
import sys
import time

import aiohttp
import orjson

from comet.core.logger import logger
from comet.core.models import database, settings

_PROVIDER_URL_PATTERNS = (
    ("anilist.co/anime/", "anilist"),
    ("myanimelist.net/anime/", "myanimelist"),
    ("kitsu.app/anime/", "kitsu"),
    ("kitsu.io/anime/", "kitsu"),
    ("anidb.net/anime/", "anidb"),
    ("anime-planet.com/anime/", "anime-planet"),
    ("anisearch.com/anime/", "anisearch"),
    ("livechart.me/anime/", "livechart"),
    ("animecountdown.com/", "animecountdown"),
    ("simkl.com/anime/", "simkl"),
)

_FRIBB_PROVIDER_ORDER = (
    ("anilist", "anilist_id"),
    ("myanimelist", "mal_id"),
    ("kitsu", "kitsu_id"),
    ("anidb", "anidb_id"),
    ("anime-planet", "anime-planet_id"),
    ("anisearch", "anisearch_id"),
    ("livechart", "livechart_id"),
    ("animecountdown", "animecountdown_id"),
    ("simkl", "simkl_id"),
)

_DB_CHUNK_SIZE = 1000


class AnimeMapper:
    def __init__(self):
        self.loaded = False
        self._refresh_lock = asyncio.Lock()

        self.anime_imdb_ids = set()
        self._aod_url = "https://github.com/manami-project/anime-offline-database/releases/latest/download/anime-offline-database-minified.json"
        self._fribb_url = "https://raw.githubusercontent.com/Fribb/anime-lists/refs/heads/master/anime-list-full.json"

    async def load_anime_mapping(self, session: aiohttp.ClientSession | None = None):
        if self.loaded:
            return True

        count = await database.fetch_val("SELECT COUNT(*) FROM anime_entries")
        if count and count > 0:
            await self._load_provider_ids()

            if await self._is_cache_stale():
                asyncio.create_task(self._refresh_from_remote(background=True))

            self.loaded = True
            logger.log(
                "COMET", f"✅ Anime mapping loaded from database: {count} entries"
            )
            return True

        return await self._refresh_from_remote(session)

    def is_anime_content(self, media_id: str, media_only_id: str):
        if not self.loaded:
            return True

        provider, provider_id = self._parse_media_id(media_id)

        if provider == "kitsu":
            return True

        if provider == "imdb":
            return provider_id in self.anime_imdb_ids

        return media_only_id in self.anime_imdb_ids

    async def _get_entry_data(self, media_id: str):
        provider, provider_id = self._parse_media_id(media_id)
        if provider is None:
            return None

        row = await database.fetch_one(
            """
            SELECT e.data 
            FROM anime_entries e
            INNER JOIN anime_ids i ON e.id = i.entry_id
            WHERE i.provider = :provider AND i.provider_id = :provider_id
            LIMIT 1
            """,
            {"provider": provider, "provider_id": provider_id},
        )
        if not row:
            return None

        return orjson.loads(row["data"])

    async def get_aliases(self, media_id: str):
        if not self.loaded:
            return {}

        data = await self._get_entry_data(media_id)
        if not data:
            return {}

        title = data.get("title")
        synonyms = data.get("synonyms")

        if not title and not synonyms:
            return {}

        if title and synonyms:
            return {"ez": [title, *synonyms]}
        elif title:
            return {"ez": [title]}
        else:
            return {"ez": list(synonyms)}

    def is_loaded(self):
        return self.loaded

    @staticmethod
    def _parse_media_id(media_id: str):
        provider, sep, provider_id = media_id.partition(":")
        if not sep:
            return None, None
        return provider, provider_id

    async def _is_cache_stale(self):
        interval = settings.ANIME_MAPPING_REFRESH_INTERVAL
        if interval <= 0:
            return False

        row = await database.fetch_one(
            "SELECT refreshed_at FROM anime_mapping_state WHERE id = 1",
        )

        if not row:
            return True

        last_refresh = row[0] if isinstance(row, tuple) else row["refreshed_at"]
        if last_refresh is None:
            return True

        return (time.time() - float(last_refresh)) >= interval

    async def _load_provider_ids(self):
        try:
            query = "SELECT provider_id FROM anime_ids WHERE provider = 'imdb'"
            rows = await database.fetch_all(query)

            self.anime_imdb_ids = {
                row[0] if isinstance(row, tuple) else row["provider_id"] for row in rows
            }

            logger.log(
                "DATABASE",
                f"Loaded {len(self.anime_imdb_ids)} anime IMDb IDs into memory",
            )
        except Exception as e:
            logger.error(f"Failed to load anime provider IDs: {e}")

    async def _refresh_from_remote(
        self,
        session: aiohttp.ClientSession | None = None,
        *,
        background: bool = False,
    ):
        async with self._refresh_lock:
            if self.loaded and not background:
                return True

            own_session = False
            if session is None:
                own_session = True
                session = aiohttp.ClientSession()

            try:
                logger.log(
                    "COMET",
                    "Downloading anime mapping (Source 1/2: Anime Offline Database)...",
                )
                async with session.get(self._aod_url) as response_aod:
                    if response_aod.status != 200:
                        logger.error(f"Failed to load AOD: HTTP {response_aod.status}")
                        return False
                    data_aod = orjson.loads(await response_aod.read())

                logger.log(
                    "COMET",
                    "Downloading anime mapping (Source 2/2: Fribb Anime List)...",
                )
                async with session.get(self._fribb_url) as response_fribb:
                    if response_fribb.status != 200:
                        logger.error(
                            f"Failed to load Fribb List: HTTP {response_fribb.status}"
                        )
                        return False
                    data_fribb = orjson.loads(await response_fribb.read())

                anime_list = data_aod.get("data", [])
                total_entries, total_fribb = await self._persist_mapping(
                    anime_list, data_fribb
                )

                del data_aod
                del data_fribb
                del anime_list
                gc.collect()

                if sys.platform == "linux":
                    try:
                        ctypes.CDLL("libc.so.6").malloc_trim(0)
                    except Exception:
                        pass
                elif sys.platform == "win32":
                    try:
                        ctypes.windll.psapi.EmptyWorkingSet(
                            ctypes.windll.kernel32.GetCurrentProcess()
                        )
                    except Exception:
                        pass

                await self._load_provider_ids()

                self.loaded = True
                logger.log(
                    "COMET",
                    f"✅ Anime mapping loaded: {total_entries} entries",
                )

                return True
            except Exception as exc:
                log_fn = logger.warning if background else logger.error
                log_fn(f"Exception while loading anime mapping: {exc}")
                return False
            finally:
                if own_session and session:
                    await session.close()

    async def _persist_mapping(self, anime_list: list, fribb_list: list):
        timestamp = time.time()

        entries_batch = []
        ids_batch = []
        lookup_map = {}
        total_entries = 0
        total_fribb = 0

        entries_query = "INSERT INTO anime_entries (id, data) VALUES (:id, :data)"
        ids_query = """
            INSERT INTO anime_ids (provider, provider_id, entry_id) 
            VALUES (:provider, :provider_id, :entry_id)
            ON CONFLICT (provider, provider_id) DO NOTHING
        """

        try:
            async with database.transaction():
                await database.execute("DELETE FROM anime_entries")
                await database.execute("DELETE FROM anime_ids")

                for idx, entry in enumerate(anime_list):
                    entry_id = idx + 1
                    entries_batch.append(
                        {"id": entry_id, "data": orjson.dumps(entry).decode("utf-8")}
                    )

                    sources = entry.get("sources")
                    if sources:
                        for source in sources:
                            for url_part, provider in _PROVIDER_URL_PATTERNS:
                                if url_part in source:
                                    try:
                                        if "id=" in source:
                                            provider_id = source.split("id=", 1)[
                                                1
                                            ].split("&", 1)[0]
                                        else:
                                            provider_id = source.rstrip("/").rsplit(
                                                "/", 1
                                            )[-1]

                                        ids_batch.append(
                                            {
                                                "provider": provider,
                                                "provider_id": provider_id,
                                                "entry_id": entry_id,
                                            }
                                        )
                                        lookup_map[f"{provider}:{provider_id}"] = (
                                            entry_id
                                        )
                                    except (IndexError, ValueError):
                                        pass
                                    break

                    if len(entries_batch) >= _DB_CHUNK_SIZE:
                        await database.execute_many(entries_query, entries_batch)
                        total_entries += len(entries_batch)
                        entries_batch.clear()

                    if len(ids_batch) >= _DB_CHUNK_SIZE:
                        await database.execute_many(ids_query, ids_batch)
                        ids_batch.clear()

                if entries_batch:
                    await database.execute_many(entries_query, entries_batch)
                    total_entries += len(entries_batch)
                    entries_batch.clear()

                if ids_batch:
                    await database.execute_many(ids_query, ids_batch)
                    ids_batch.clear()

                del entries_batch
                del ids_batch

                fribb_batch = []
                for entry in fribb_list:
                    imdb_id = entry.get("imdb_id")
                    if not imdb_id:
                        continue

                    for provider, key in _FRIBB_PROVIDER_ORDER:
                        val = entry.get(key)
                        if val:
                            found_entry_id = lookup_map.get(f"{provider}:{val}")
                            if found_entry_id is not None:
                                fribb_batch.append(
                                    {
                                        "provider": "imdb",
                                        "provider_id": imdb_id,
                                        "entry_id": found_entry_id,
                                    }
                                )
                                break

                    if len(fribb_batch) >= _DB_CHUNK_SIZE:
                        await database.execute_many(ids_query, fribb_batch)
                        total_fribb += len(fribb_batch)
                        fribb_batch.clear()

                if fribb_batch:
                    await database.execute_many(ids_query, fribb_batch)
                    total_fribb += len(fribb_batch)
                    fribb_batch.clear()

                del fribb_batch
                del lookup_map

                await database.execute(
                    """
                    INSERT INTO anime_mapping_state (id, refreshed_at)
                    VALUES (1, :timestamp)
                    ON CONFLICT (id) DO UPDATE SET refreshed_at = :timestamp
                    """,
                    {"timestamp": timestamp},
                )

            logger.log(
                "DATABASE",
                f"Anime mapping updated: {total_entries} entries, {total_fribb} IMDb mappings added",
            )

            return total_entries, total_fribb

        except Exception as exc:
            logger.error(f"Failed to persist anime mapping cache: {exc}")



anime_mapper = AnimeMapper()
