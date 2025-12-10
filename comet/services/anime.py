import asyncio
import time
from collections.abc import Mapping

import aiohttp
import orjson

from comet.core.logger import logger
from comet.core.models import database, settings


class AnimeMapper:
    def __init__(self):
        self.kitsu_to_imdb = {}
        self.imdb_to_kitsu = {}
        self.anime_imdb_ids = set()
        self.loaded = False
        self._refresh_lock = asyncio.Lock()
        self._background_task = None

    async def load_anime_mapping(self, session: aiohttp.ClientSession | None = None):
        if self.loaded:
            logger.log(
                "COMET",
                "Anime mapping already loaded in this process; skipping reload",
            )
            return True

        source = (settings.ANIME_MAPPING_SOURCE or "remote").lower()

        if source == "database":
            loaded = await self._load_from_database()
            if loaded:
                if await self._is_cache_stale():
                    asyncio.create_task(self._refresh_from_remote(background=True))
                self._ensure_periodic_refresh()
                return True

        return await self._refresh_from_remote(session)

    def get_imdb_from_kitsu(self, kitsu_id: int):
        return self.kitsu_to_imdb.get(kitsu_id)

    def get_kitsu_from_imdb(self, imdb_id: str):
        return self.imdb_to_kitsu.get(imdb_id)

    def is_anime(self, imdb_id: str):
        return imdb_id in self.anime_imdb_ids

    def is_anime_content(self, media_id: str, media_only_id: str):
        if "kitsu" in media_id:
            return True

        if not self.loaded:
            return True

        return self.is_anime(media_only_id)

    def is_loaded(self):
        return self.loaded

    async def _load_from_database(self):
        try:
            rows = await database.fetch_all(
                "SELECT kitsu_id, imdb_id FROM anime_mapping_cache",
                force_primary=True,
            )

            if not rows:
                logger.log(
                    "DATABASE",
                    "Anime mapping cache empty; falling back to remote source",
                )
                return False

            self._populate_from_rows(rows)
            logger.log(
                "COMET",
                f"✅ Anime mapping loaded from database: {len(rows)} cached entries",
            )
            return True
        except Exception as exc:
            logger.error(f"Failed to load anime mapping from database: {exc}")
            return False

    async def _is_cache_stale(self):
        interval = settings.ANIME_MAPPING_REFRESH_INTERVAL or 0
        if interval <= 0:
            return False

        row = await database.fetch_one(
            "SELECT refreshed_at FROM anime_mapping_state WHERE id = 1",
            force_primary=True,
        )

        if not row:
            return True

        last_refresh = row[0] if isinstance(row, tuple) else row["refreshed_at"]
        if last_refresh is None:
            return True

        last_refresh = float(last_refresh)
        return (time.time() - last_refresh) >= interval

    def _ensure_periodic_refresh(self):
        interval = settings.ANIME_MAPPING_REFRESH_INTERVAL or 0
        if interval <= 0:
            return

        if self._background_task and not self._background_task.done():
            return

        self._background_task = asyncio.create_task(self._refresh_loop(interval))

    async def _refresh_from_remote(
        self,
        session: aiohttp.ClientSession | None = None,
        *,
        background: bool = False,
    ):
        async with self._refresh_lock:
            if self.loaded and background:
                return True

            own_session = False
            if session is None:
                own_session = True
                session = aiohttp.ClientSession()

            try:
                url = "https://raw.githubusercontent.com/Fribb/anime-lists/refs/heads/master/anime-list-full.json"
                response = await session.get(url)

                if response.status != 200:
                    logger.error(
                        f"Failed to load anime mapping: HTTP {response.status}"
                    )
                    return False

                text = await response.text()
                data = orjson.loads(text)

                self._populate_from_rows(data)
                logger.log(
                    "COMET",
                    f"✅ Anime mapping loaded: {len(self.kitsu_to_imdb)} Kitsu entries, {len(self.imdb_to_kitsu)} with IMDB IDs",
                )

                if settings.ANIME_MAPPING_SOURCE == "database":
                    await self._persist_mapping(data)
                    self._ensure_periodic_refresh()

                return True
            except Exception as exc:
                log_fn = logger.warning if background else logger.error
                log_fn(f"Exception while loading anime mapping: {exc}")
                return False
            finally:
                if own_session and session:
                    await session.close()

    def _populate_from_rows(self, rows):
        self.kitsu_to_imdb.clear()
        self.imdb_to_kitsu.clear()
        self.anime_imdb_ids.clear()

        for entry in rows:
            kitsu_id = self._entry_value(entry, "kitsu_id")
            imdb_id = self._entry_value(entry, "imdb_id")

            if kitsu_id and imdb_id:
                self.kitsu_to_imdb[kitsu_id] = imdb_id
                self.imdb_to_kitsu[imdb_id] = kitsu_id
                self.anime_imdb_ids.add(imdb_id)

        self.loaded = True

    async def _persist_mapping(self, rows):
        timestamp = time.time()
        params = []
        for entry in rows:
            kitsu_id = self._entry_value(entry, "kitsu_id")
            if not kitsu_id:
                continue

            params.append(
                {
                    "kitsu_id": kitsu_id,
                    "imdb_id": self._entry_value(entry, "imdb_id"),
                    "is_anime": True,
                    "updated_at": timestamp,
                }
            )

        insert_query = (
            "INSERT INTO anime_mapping_cache (kitsu_id, imdb_id, is_anime, updated_at) "
            "VALUES (:kitsu_id, :imdb_id, :is_anime, :updated_at)"
        )

        chunk_size = 500

        try:
            async with database.transaction():
                await database.execute("DELETE FROM anime_mapping_cache")
                for idx in range(0, len(params), chunk_size):
                    await database.execute_many(
                        insert_query,
                        params[idx : idx + chunk_size],
                    )
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
                f"Anime mapping cache updated ({len(params)} rows)",
            )
        except Exception as exc:
            logger.error(f"Failed to persist anime mapping cache: {exc}")

    @staticmethod
    def _entry_value(entry, key):
        if isinstance(entry, Mapping):
            return entry.get(key)
        return entry[key]

    async def _refresh_loop(self, interval: int):
        while True:
            try:
                await asyncio.sleep(interval)
                await self._refresh_from_remote(background=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"Anime mapping refresh loop encountered an error: {exc}")

    async def stop(self):
        if self._background_task:
            self._background_task.cancel()
            try:
                await self._background_task
            except asyncio.CancelledError:
                pass


anime_mapper = AnimeMapper()
