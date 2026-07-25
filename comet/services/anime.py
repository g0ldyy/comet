import asyncio
import os
import time
from contextlib import asynccontextmanager

import aiohttp
import orjson

from comet.core.database import backend_lock, database
from comet.core.logger import logger
from comet.core.models import settings
from comet.utils.memory import trim_process_memory

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

_DB_CHUNK_SIZE = 10000
_ANIME_REFRESH_LOCK_ID = 0xA11E0001
_ANIME_MEDIA_TYPES = {
    "MOVIE": "movie",
    "ONA": "series",
    "OVA": "series",
    "SPECIAL": "series",
    "TV": "series",
}


@asynccontextmanager
async def _anime_refresh_lock():
    async with backend_lock(
        postgres_lock_id=_ANIME_REFRESH_LOCK_ID,
        sqlite_lock_path=f"{os.path.abspath(settings.DATABASE_PATH)}.anime.lock",
        wait_message="Waiting for anime mapping refresh lock",
    ):
        yield


class AnimeMapper:
    def __init__(self):
        self.loaded = False
        self._refresh_lock = asyncio.Lock()
        self._refresh_task = None

        self.anime_imdb_ids = set()
        self._kitsu_mapping_cache = {}
        self._imdb_kitsu_mapping_cache = {}

        self._aod_url = "https://github.com/manami-project/anime-offline-database/releases/latest/download/anime-offline-database-minified.json"
        self._fribb_url = "https://raw.githubusercontent.com/Fribb/anime-lists/refs/heads/master/anime-list-full.json"
        self._kitsu_imdb_url = "https://raw.githubusercontent.com/TheBeastLT/stremio-kitsu-anime/master/static/data/imdb_mapping.json"

    async def load_anime_mapping(self, session: aiohttp.ClientSession | None = None):
        if not settings.ANIME_MAPPING_ENABLED:
            return True

        if self.loaded:
            return True

        if await self._load_from_database(schedule_refresh=True):
            return True

        return await self._refresh_from_remote(session)

    async def load_cached_mapping(self) -> bool:
        """Load persisted mappings without scheduling downloads or refreshes."""
        if self.loaded:
            return True
        return await self._load_from_database(schedule_refresh=False)

    def is_anime_content(self, media_id: str, media_only_id: str):
        if not settings.ANIME_MAPPING_ENABLED:
            return False

        if not self.loaded:  # to prevent blocking anime-only scrapers
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
            SELECT e.data_json 
            FROM anime_entries e
            INNER JOIN anime_ids i ON e.id = i.entry_id
            WHERE i.provider = :provider AND i.provider_id = :provider_id
            LIMIT 1
            """,
            {"provider": provider, "provider_id": provider_id},
        )
        if not row:
            return None

        try:
            data = orjson.loads(row["data_json"])
        except (TypeError, orjson.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    async def get_aliases(self, media_id: str):
        if not self.loaded:
            return {}

        data = await self._get_entry_data(media_id)
        if not data:
            return {}

        title = data.get("title")
        if not isinstance(title, str) or not title:
            title = None
        raw_synonyms = data.get("synonyms")

        synonyms = []
        seen = set()
        for value in raw_synonyms if isinstance(raw_synonyms, list) else []:
            if not isinstance(value, str) or not value or value == title:
                continue
            if value not in seen:
                seen.add(value)
                synonyms.append(value)

        if not title and not synonyms:
            return {}

        aliases = {}
        if title:
            aliases["original"] = [title]
        if synonyms:
            aliases["ez"] = synonyms
        return aliases

    async def get_media_type(self, media_id: str) -> str | None:
        data = await self._get_entry_data(media_id)
        if not data:
            return None
        raw_type = data.get("type")
        if not isinstance(raw_type, str):
            return None
        return _ANIME_MEDIA_TYPES.get(raw_type.upper())

    async def get_imdb_from_kitsu(self, kitsu_id: str | int):
        if not self.loaded:
            return None

        imdb_id = await database.fetch_val(
            """
            SELECT i2.provider_id
            FROM anime_ids i1
            JOIN anime_ids i2 ON i1.entry_id = i2.entry_id
            WHERE i1.provider = 'kitsu' AND i1.provider_id = :kitsu_id
            AND i2.provider = 'imdb'
            LIMIT 1
            """,
            {"kitsu_id": str(kitsu_id)},
        )

        if imdb_id:
            return imdb_id

        mapping = self._kitsu_mapping_cache.get(str(kitsu_id))
        if mapping:
            return mapping.get("imdb_id")

        return None

    async def get_kitsu_from_imdb(self, imdb_id: str | int):
        if not self.loaded:
            return None

        kitsu_id = await database.fetch_val(
            """
            SELECT i2.provider_id
            FROM anime_ids i1
            JOIN anime_ids i2 ON i1.entry_id = i2.entry_id
            WHERE i1.provider = 'imdb' AND i1.provider_id = :imdb_id
            AND i2.provider = 'kitsu'
            LIMIT 1
            """,
            {"imdb_id": str(imdb_id)},
        )

        if kitsu_id:
            return kitsu_id

        kitsu_ids = self._imdb_kitsu_mapping_cache.get(str(imdb_id)) or []
        return kitsu_ids[0] if kitsu_ids else None

    def get_kitsu_ids_from_imdb(self, imdb_id: str | int) -> list[str]:
        if not self.loaded:
            return []

        kitsu_ids = self._imdb_kitsu_mapping_cache.get(str(imdb_id))
        return list(kitsu_ids) if kitsu_ids else []

    async def get_anilist_id(self, media_id: str):
        if not self.loaded:
            return None

        provider, provider_id = self._parse_media_id(media_id)

        if provider is None:
            return None

        query = """
            SELECT i2.provider_id
            FROM anime_ids i1
            JOIN anime_ids i2 ON i1.entry_id = i2.entry_id
            WHERE i1.provider = :provider AND i1.provider_id = :provider_id
            AND i2.provider = 'anilist'
            LIMIT 1
        """

        return await database.fetch_val(
            query, {"provider": provider, "provider_id": provider_id}
        )

    def get_kitsu_episode_mapping(self, kitsu_id: str | int):
        if not self.loaded:
            return None

        return self._kitsu_mapping_cache.get(str(kitsu_id))

    def is_loaded(self):
        return self.loaded

    @staticmethod
    def _parse_media_id(media_id: str):
        if media_id.startswith("tt"):
            return "imdb", media_id.split(":")[0]

        if media_id.startswith("kitsu:"):
            provider_id = media_id.partition(":")[2]
            return ("kitsu", provider_id) if provider_id else (None, None)

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

        last_refresh = row["refreshed_at"]
        if last_refresh is None:
            return True

        return (time.time() - float(last_refresh)) >= interval

    async def _read_provider_ids(self):
        query = "SELECT provider_id FROM anime_ids WHERE provider = 'imdb'"
        rows = await database.fetch_all(query)
        return {row["provider_id"] for row in rows}

    async def _read_kitsu_mapping_caches(self):
        rows = await database.fetch_all(
            """
            SELECT source_id, target_id, from_season, from_episode
            FROM anime_provider_overrides
            WHERE source_provider = 'kitsu'
              AND target_provider = 'imdb'
              AND (
                    (from_episode IS NOT NULL AND from_episode > 1)
                    OR from_season IS NOT NULL
                  )
            """
        )

        kitsu_mapping_cache = {}
        imdb_kitsu_mapping_cache = {}
        for row in rows:
            kitsu_id = row["source_id"]
            imdb_id = row["target_id"]

            kitsu_mapping_cache[str(kitsu_id)] = {
                "imdb_id": imdb_id,
                "from_season": row["from_season"],
                "from_episode": row["from_episode"],
            }

            imdb_kitsu_mapping_cache.setdefault(str(imdb_id), []).append(str(kitsu_id))

        return kitsu_mapping_cache, imdb_kitsu_mapping_cache

    async def _load_mapping_caches(self):
        anime_imdb_ids = await self._read_provider_ids()
        (
            kitsu_mapping_cache,
            imdb_kitsu_mapping_cache,
        ) = await self._read_kitsu_mapping_caches()
        self.anime_imdb_ids = anime_imdb_ids
        self._kitsu_mapping_cache = kitsu_mapping_cache
        self._imdb_kitsu_mapping_cache = imdb_kitsu_mapping_cache

    async def _needs_refresh(self) -> bool:
        kitsu_count = await database.fetch_val(
            """
            SELECT COUNT(*)
            FROM anime_provider_overrides
            WHERE source_provider = 'kitsu'
              AND target_provider = 'imdb'
            """
        )
        needs_kitsu_refresh = kitsu_count == 0 or len(self._kitsu_mapping_cache) == 0
        return await self._is_cache_stale() or needs_kitsu_refresh

    def _handle_refresh_task_done(self, task: asyncio.Task):
        if self._refresh_task is task:
            self._refresh_task = None
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.warning(f"Anime mapping refresh task failed: {error}")

    def _schedule_background_refresh(self):
        if self._refresh_task is not None and not self._refresh_task.done():
            return

        self._refresh_task = asyncio.create_task(
            self._refresh_from_remote(background=True),
            name="anime-mapping-refresh",
        )
        self._refresh_task.add_done_callback(self._handle_refresh_task_done)

    async def stop(self):
        task = self._refresh_task
        if task is None:
            return
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if self._refresh_task is task:
            self._refresh_task = None

    async def _load_from_database(
        self,
        *,
        schedule_refresh: bool,
        log_loaded: bool = True,
    ) -> bool:
        count = await database.fetch_val("SELECT COUNT(*) FROM anime_entries")
        if not count or count <= 0:
            return False

        try:
            await self._load_mapping_caches()
        except Exception as exc:
            logger.warning(f"Failed to load anime mapping caches: {exc}")
            return False

        if schedule_refresh and await self._needs_refresh():
            self._schedule_background_refresh()

        self.loaded = True
        if log_loaded:
            logger.log(
                "COMET",
                f"✅ Anime mapping loaded from database: {count} entries, {len(self._kitsu_mapping_cache)} Kitsu-IMDB mappings",
            )
        return True

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
                async with _anime_refresh_lock():
                    if (
                        await self._load_from_database(
                            schedule_refresh=False,
                            log_loaded=False,
                        )
                        and not await self._needs_refresh()
                    ):
                        return True

                    async def _download_json(url: str, label: str):
                        logger.log("COMET", f"Downloading anime mapping ({label})...")
                        async with session.get(url) as response:
                            return response.status, await response.read()

                    (
                        (aod_status, aod_payload),
                        (fribb_status, fribb_payload),
                        (kitsu_status, kitsu_payload),
                    ) = await asyncio.gather(
                        _download_json(
                            self._aod_url, "Source 1/3: Anime Offline Database"
                        ),
                        _download_json(self._fribb_url, "Source 2/3: Fribb Anime List"),
                        _download_json(
                            self._kitsu_imdb_url, "Source 3/3: Kitsu-IMDB Mapping"
                        ),
                    )

                    if aod_status != 200:
                        logger.error(f"Failed to load AOD: HTTP {aod_status}")
                        return False

                    if fribb_status != 200:
                        logger.error(f"Failed to load Fribb List: HTTP {fribb_status}")
                        return False

                    if kitsu_status != 200:
                        logger.warning(
                            f"Failed to load Kitsu-IMDB mapping: HTTP {kitsu_status}"
                        )
                        return False

                    data_aod = orjson.loads(aod_payload)
                    data_fribb = orjson.loads(fribb_payload)
                    data_kitsu_imdb = orjson.loads(kitsu_payload)

                    if not isinstance(data_aod, dict):
                        raise ValueError("AOD payload must be an object")
                    anime_list = data_aod.get("data")
                    if (
                        not isinstance(anime_list, list)
                        or not isinstance(data_fribb, list)
                        or not isinstance(data_kitsu_imdb, list)
                    ):
                        raise ValueError("Anime mapping payloads have invalid shapes")
                    total_entries = await self._persist_remote_mapping(
                        anime_list,
                        data_fribb,
                        data_kitsu_imdb,
                    )

                    del data_aod
                    del data_fribb
                    del data_kitsu_imdb
                    del anime_list
                    trim_process_memory()

                    await self._load_mapping_caches()

                    self.loaded = True
                    logger.log(
                        "COMET",
                        f"✅ Anime mapping loaded: {total_entries} entries, {len(self._kitsu_mapping_cache)} Kitsu-IMDB mappings cached",
                    )

                    return True
            except Exception as exc:
                log_fn = logger.warning if background else logger.error
                log_fn(f"Exception while loading anime mapping: {exc}")
                return False
            finally:
                if own_session and session:
                    await session.close()

    async def _persist_remote_mapping(
        self,
        anime_list: list,
        fribb_list: list,
        kitsu_imdb_data: list,
    ) -> int:
        async with database.transaction():
            total_entries = await self._persist_mapping(anime_list, fribb_list)
            await self._persist_provider_overrides(kitsu_imdb_data)
        return total_entries

    async def _persist_mapping(self, anime_list: list, fribb_list: list):
        timestamp = time.time()

        entries_batch = []
        ids_batch = []
        lookup_map = {}
        total_entries = 0

        entries_query = """
            INSERT INTO anime_entries (id, data_json)
            VALUES (:id, :data_json)
            ON CONFLICT (id) DO UPDATE SET data_json = EXCLUDED.data_json
        """
        ids_query = """
            INSERT INTO anime_ids (provider, provider_id, entry_id) 
            VALUES (:provider, :provider_id, :entry_id)
            ON CONFLICT DO NOTHING
        """

        try:
            async with database.transaction():
                await database.execute("DELETE FROM anime_ids")
                await database.execute("DELETE FROM anime_entries")

                async def flush_entries(batch: list[dict]) -> int:
                    if not batch:
                        return 0

                    await database.execute_many(entries_query, batch)
                    flushed_count = len(batch)
                    batch.clear()
                    return flushed_count

                async def flush_ids(batch: list[dict]):
                    if not batch:
                        return

                    # `anime_ids.entry_id` now has a real FK to `anime_entries.id`,
                    # so parent rows must exist before child rows are flushed.
                    await database.execute_many(ids_query, batch)
                    batch.clear()

                for idx, entry in enumerate(anime_list):
                    entry_id = idx + 1
                    entries_batch.append(
                        {
                            "id": entry_id,
                            "data_json": orjson.dumps(entry).decode("utf-8"),
                        }
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
                        total_entries += await flush_entries(entries_batch)

                    if len(ids_batch) >= _DB_CHUNK_SIZE:
                        total_entries += await flush_entries(entries_batch)
                        await flush_ids(ids_batch)

                total_entries += await flush_entries(entries_batch)
                await flush_ids(ids_batch)

                del entries_batch
                del ids_batch

                fribb_batch = []
                for entry in fribb_list:
                    imdb_id = entry.get("imdb_id")
                    if not imdb_id:
                        continue

                    # Fribb stores imdb_id as a list for multi-season entries.
                    # SQLite can't bind a list, so normalize to individual ids.
                    imdb_ids = imdb_id if isinstance(imdb_id, list) else [imdb_id]

                    for provider, key in _FRIBB_PROVIDER_ORDER:
                        val = entry.get(key)
                        if val:
                            found_entry_id = lookup_map.get(f"{provider}:{val}")
                            if found_entry_id is not None:
                                for single_imdb_id in imdb_ids:
                                    if not single_imdb_id:
                                        continue
                                    fribb_batch.append(
                                        {
                                            "provider": "imdb",
                                            "provider_id": str(single_imdb_id),
                                            "entry_id": found_entry_id,
                                        }
                                    )
                                break

                    if len(fribb_batch) >= _DB_CHUNK_SIZE:
                        await database.execute_many(ids_query, fribb_batch)
                        fribb_batch.clear()

                if fribb_batch:
                    await database.execute_many(ids_query, fribb_batch)
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

            return total_entries
        except Exception as exc:
            logger.error(f"Failed to persist anime mapping cache: {exc}")
            raise

    async def _persist_provider_overrides(self, kitsu_imdb_data: list):
        total_count = 0
        batch = []
        batch_size = 1000

        try:
            async with database.transaction():
                await database.execute(
                    """
                    DELETE FROM anime_provider_overrides
                    WHERE source_provider = 'kitsu'
                      AND target_provider = 'imdb'
                    """
                )

                insert_query = """
                    INSERT INTO anime_provider_overrides
                    (
                        source_provider,
                        source_id,
                        target_provider,
                        target_id,
                        from_season,
                        from_episode
                    )
                    VALUES (
                        'kitsu',
                        :source_id,
                        'imdb',
                        :target_id,
                        :from_season,
                        :from_episode
                    )
                    ON CONFLICT (source_provider, source_id, target_provider) DO UPDATE SET
                        target_id = :target_id,
                        from_season = :from_season,
                        from_episode = :from_episode
                """

                for entry in kitsu_imdb_data:
                    kitsu_id = entry["kitsu_id"]

                    imdb_id = entry.get("imdb_id")
                    if not imdb_id:
                        continue

                    from_season = entry.get("fromSeason")
                    from_episode = entry.get("fromEpisode")

                    batch.append(
                        {
                            "source_id": str(kitsu_id),
                            "target_id": imdb_id,
                            "from_season": from_season,
                            "from_episode": from_episode,
                        }
                    )

                    if len(batch) >= batch_size:
                        await database.execute_many(insert_query, batch)
                        total_count += len(batch)
                        batch.clear()

                if batch:
                    await database.execute_many(insert_query, batch)
                    total_count += len(batch)
                    batch.clear()

            return total_count
        except Exception as exc:
            logger.error(f"Failed to persist anime provider overrides: {exc}")
            raise


anime_mapper = AnimeMapper()
