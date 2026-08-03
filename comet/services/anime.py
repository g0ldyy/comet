import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

import orjson

from comet.core.database import backend_lock, database
from comet.core.models import settings
from comet.observability import log
from comet.observability.context import create_detached_task
from comet.usenet.outbound import OutboundUrlError, fetch_http_bytes
from comet.utils.memory import trim_process_memory

_FRIBB_PROVIDER_KEYS = {
    "anilist": "anilist_id",
    "myanimelist": "mal_id",
    "kitsu": "kitsu_id",
    "anidb": "anidb_id",
    "anime-planet": "anime-planet_id",
    "anisearch": "anisearch_id",
    "livechart": "livechart_id",
    "animecountdown": "animecountdown_id",
    "simkl": "simkl_id",
}

_DB_CHUNK_SIZE = 10000
_MAX_ANIME_ENTRIES = 100_000
_MAX_FRIBB_ENTRIES = 100_000
_MAX_KITSU_MAPPINGS = 100_000
_MAX_ENTRY_JSON_BYTES = 128 * 1024
_MAX_SOURCES_PER_ENTRY = 64
_MAX_IDENTITIES = 250_000
_MAX_PROVIDER_ID_BYTES = 128
_MAX_TITLE_BYTES = 512
_MAX_ALIASES = 64
_MAX_SOURCE_URL_BYTES = 2_048
_AOD_MAX_BYTES = 80 * 1024 * 1024
_FRIBB_MAX_BYTES = 16 * 1024 * 1024
_KITSU_MAX_BYTES = 4 * 1024 * 1024
_ANIME_REFRESH_LOCK_ID = 0xA11E0001


class AnimeMappingPayloadError(ValueError):
    pass


def _reject_json_constant(_value):
    raise ValueError("invalid anime JSON constant")


def _decode_json(document: bytes):
    try:
        return json.loads(
            document.decode("utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ValueError("invalid anime JSON") from None


def _decode_cached_entry(value: object) -> dict:
    if isinstance(value, str):
        document = value.encode("utf-8")
    elif isinstance(value, bytes):
        document = value
    else:
        raise ValueError("invalid cached anime entry")
    if not document or len(document) > _MAX_ENTRY_JSON_BYTES:
        raise ValueError("invalid cached anime entry")
    data = _decode_json(document)
    if not isinstance(data, dict):
        raise ValueError("invalid cached anime entry")
    return data


def _bounded_text(value: object, maximum: int) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return None
    if size > maximum or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        return None
    return value


def _provider_id(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    return _bounded_text(str(value), _MAX_PROVIDER_ID_BYTES)


def _imdb_id(value: object) -> str | None:
    value = _provider_id(value)
    if value is None or not value.startswith("tt") or not value[2:].isdigit():
        return None
    return value


def _coordinate(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise AnimeMappingPayloadError("invalid anime mapping coordinate")
    if value < 0:
        raise AnimeMappingPayloadError("invalid anime mapping coordinate")
    return value


def _provider_identity(source: object) -> tuple[str, str] | None:
    source = _bounded_text(source, _MAX_SOURCE_URL_BYTES)
    if source is None:
        return None
    try:
        parsed = urlsplit(source)
    except ValueError:
        return None
    if not parsed.hostname:
        return None
    host = parsed.hostname.rstrip(".").lower()
    labels = host.split(".")
    provider = labels[-2] if len(labels) > 1 else labels[0]
    if provider not in _FRIBB_PROVIDER_KEYS:
        return None
    query = parse_qs(parsed.query, keep_blank_values=False)
    values = query.get("id") or query.get("aid")
    segments = [segment for segment in parsed.path.split("/") if segment]
    if values:
        candidate = values[0]
    elif "anime" in segments:
        index = segments.index("anime") + 1
        candidate = segments[index] if index < len(segments) else None
    else:
        candidate = segments[0] if segments else None
    provider_id = _provider_id(candidate)
    return (provider, provider_id) if provider_id is not None else None


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

    async def load_anime_mapping(self):
        if not settings.ANIME_MAPPING_ENABLED:
            log.info(
                "anime.mapping.disabled",
                "Anime mapping is disabled",
                provider_name="anime-mapping",
                operation="load",
            )
            return True

        if self.loaded:
            return True

        if await self._load_from_database(schedule_refresh=True):
            return True

        return await self._refresh_from_remote()

    def is_anime_content(self, media_id: str, media_only_id: str):
        if not settings.ANIME_MAPPING_ENABLED:
            return False

        if not self.loaded:
            return False

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
        if row is None:
            return None

        return _decode_cached_entry(row["data_json"])

    async def get_aliases(self, media_id: str):
        if not self.loaded:
            return {}

        data = await self._get_entry_data(media_id)
        if data is None:
            return {}

        title = _bounded_text(data.get("title"), _MAX_TITLE_BYTES)
        raw_synonyms = data.get("synonyms")

        synonyms = []
        seen = set()
        for value in raw_synonyms if isinstance(raw_synonyms, list) else []:
            value = _bounded_text(value, _MAX_TITLE_BYTES)
            if value is None or value == title:
                continue
            if value not in seen:
                seen.add(value)
                synonyms.append(value)
                if len(synonyms) == _MAX_ALIASES:
                    break

        if title is None and not synonyms:
            return {}

        aliases = {}
        if title is not None:
            aliases["original"] = [title]
        if synonyms:
            aliases["ez"] = synonyms
        return aliases

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

        if imdb_id is not None:
            return imdb_id

        mapping = self._kitsu_mapping_cache.get(str(kitsu_id))
        if mapping is not None:
            return mapping["imdb_id"]

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

        if kitsu_id is not None:
            return kitsu_id

        kitsu_ids = self._imdb_kitsu_mapping_cache.get(str(imdb_id))
        return kitsu_ids[0] if kitsu_ids is not None else None

    def get_kitsu_ids_from_imdb(self, imdb_id: str | int) -> list[str]:
        if not self.loaded:
            return []

        kitsu_ids = self._imdb_kitsu_mapping_cache.get(str(imdb_id))
        return list(kitsu_ids) if kitsu_ids is not None else []

    async def get_anilist_id(self, media_id: str):
        if not self.loaded:
            return None

        provider, provider_id = self._parse_media_id(media_id)

        if provider is None:
            return None

        if provider == "imdb":
            parts = media_id.split(":")
            if len(parts) > 1 and parts[1].isdigit():
                season = int(parts[1])
                episode = (
                    int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
                )
                kitsu_id = self._get_kitsu_for_imdb_scope(
                    provider_id,
                    season,
                    episode,
                )
                if kitsu_id is not None:
                    provider, provider_id = "kitsu", kitsu_id

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

    def _get_kitsu_for_imdb_scope(
        self,
        imdb_id: str,
        season: int,
        episode: int | None,
    ) -> str | None:
        candidates = []
        for kitsu_id in self._imdb_kitsu_mapping_cache.get(imdb_id, ()):
            mapping = self._kitsu_mapping_cache[kitsu_id]
            if mapping.get("from_season") != season:
                continue
            from_episode = mapping.get("from_episode")
            start = 1 if from_episode is None else from_episode
            if episode is None or start <= episode:
                candidates.append((start, kitsu_id))
        if not candidates:
            return None
        return (min if episode is None else max)(candidates)[1]

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

        if row is None:
            return True

        refreshed_at = row["refreshed_at"]
        if refreshed_at is None:
            return True

        return (time.time() - refreshed_at) >= interval

    async def _read_provider_ids(self):
        query = "SELECT provider_id FROM anime_ids WHERE provider = 'imdb'"
        rows = await database.fetch_all(query)
        if len(rows) > _MAX_IDENTITIES:
            raise ValueError("too many cached anime identities")
        provider_ids = set()
        for row in rows:
            provider_id = _imdb_id(row["provider_id"])
            if provider_id is None:
                raise ValueError("invalid cached IMDb identity")
            provider_ids.add(provider_id)
        return provider_ids

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

        if len(rows) > _MAX_KITSU_MAPPINGS:
            raise ValueError("too many cached Kitsu mappings")
        kitsu_mapping_cache = {}
        imdb_kitsu_mapping_cache = {}
        for row in rows:
            kitsu_id = _provider_id(row["source_id"])
            imdb_id = _imdb_id(row["target_id"])
            from_season = _coordinate(row["from_season"])
            from_episode = _coordinate(row["from_episode"])
            if (
                kitsu_id is None
                or not kitsu_id.isdigit()
                or imdb_id is None
                or kitsu_id in kitsu_mapping_cache
            ):
                raise ValueError("invalid cached Kitsu mapping")

            kitsu_mapping_cache[kitsu_id] = {
                "imdb_id": imdb_id,
                "from_season": from_season,
                "from_episode": from_episode,
            }

            imdb_kitsu_mapping_cache.setdefault(imdb_id, []).append(kitsu_id)

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
        return await self._is_cache_stale() or kitsu_count == 0

    def _handle_refresh_task_done(self, task: asyncio.Task):
        if self._refresh_task is task:
            self._refresh_task = None

    def _schedule_background_refresh(self):
        if self._refresh_task is not None and not self._refresh_task.done():
            return

        self._refresh_task = create_detached_task(
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
        try:
            await task
        except asyncio.CancelledError:
            pass
        if self._refresh_task is task:
            self._refresh_task = None

    async def _load_from_database(
        self,
        *,
        schedule_refresh: bool,
    ) -> bool:
        count = await database.fetch_val("SELECT COUNT(*) FROM anime_entries")
        if count == 0:
            return False

        await self._load_mapping_caches()

        if schedule_refresh:
            needs_refresh = await self._needs_refresh()
            log.info(
                "anime.mapping.loaded",
                "Anime mapping loaded from database",
                provider_name="anime-mapping",
                operation="load",
                cache_state="stale" if needs_refresh else "fresh",
                item_count=count,
                result_count=len(self.anime_imdb_ids),
            )
            if needs_refresh:
                self._schedule_background_refresh()

        self.loaded = True
        return True

    async def _refresh_from_remote(
        self,
        *,
        background: bool = False,
    ):
        async with self._refresh_lock:
            if self.loaded and not background:
                return True

            async with _anime_refresh_lock():
                if (
                    await self._load_from_database(schedule_refresh=False)
                    and not await self._needs_refresh()
                ):
                    return True

                loop = asyncio.get_running_loop()
                refresh_started = loop.time()
                operation = "background_refresh" if background else "startup_refresh"
                log.info(
                    "anime.download.started",
                    "Anime mapping download started",
                    provider_name="anime-mapping",
                    operation=operation,
                    cache_state="stale" if self.loaded else "empty",
                    requested_count=3,
                )

                def _log_refresh_failure(
                    failure_reason: str,
                    exc: BaseException | None = None,
                ) -> None:
                    log.warning(
                        "anime.refresh.failed",
                        "Anime mapping refresh failed",
                        provider_name="anime-mapping",
                        operation=operation,
                        outcome="failed",
                        failure_reason=failure_reason,
                        error_code="dependency_warning",
                        duration_ms=(loop.time() - refresh_started) * 1000,
                        exc=exc,
                    )

                async def _download_json(url: str, maximum: int):
                    return await fetch_http_bytes(
                        url,
                        max_bytes=maximum,
                        headers={"Accept": "application/json"},
                        redirects=3,
                    )

                download_started = loop.time()
                download_failed = False
                try:
                    async with asyncio.TaskGroup() as task_group:
                        aod_task = task_group.create_task(
                            _download_json(self._aod_url, _AOD_MAX_BYTES)
                        )
                        fribb_task = task_group.create_task(
                            _download_json(self._fribb_url, _FRIBB_MAX_BYTES)
                        )
                        kitsu_task = task_group.create_task(
                            _download_json(self._kitsu_imdb_url, _KITSU_MAX_BYTES)
                        )
                except* OutboundUrlError as exc_group:
                    log.warning(
                        "anime.download.failed",
                        "Anime mapping download failed",
                        provider_name="anime-mapping",
                        operation=operation,
                        outcome="failed",
                        failure_reason="network_error",
                        error_code="dependency_warning",
                        duration_ms=(loop.time() - download_started) * 1000,
                        exc=exc_group,
                    )
                    download_failed = True
                if download_failed:
                    return False

                response_bytes = sum(
                    len(task.result()) for task in (aod_task, fribb_task, kitsu_task)
                )
                log.info(
                    "anime.download.completed",
                    "Anime mapping download completed",
                    provider_name="anime-mapping",
                    operation=operation,
                    outcome="ok",
                    requested_count=3,
                    response_bytes=response_bytes,
                    duration_ms=(loop.time() - download_started) * 1000,
                )

                try:
                    data_aod = _decode_json(aod_task.result())
                    data_fribb = _decode_json(fribb_task.result())
                    data_kitsu_imdb = _decode_json(kitsu_task.result())
                except ValueError as exc:
                    _log_refresh_failure("invalid_json", exc)
                    return False

                anime_list = (
                    data_aod.get("data") if isinstance(data_aod, dict) else None
                )
                if (
                    not isinstance(anime_list, list)
                    or len(anime_list) > _MAX_ANIME_ENTRIES
                    or not isinstance(data_fribb, list)
                    or len(data_fribb) > _MAX_FRIBB_ENTRIES
                    or not isinstance(data_kitsu_imdb, list)
                    or len(data_kitsu_imdb) > _MAX_KITSU_MAPPINGS
                ):
                    _log_refresh_failure("invalid_payload")
                    return False
                try:
                    total_entries = await self._persist_remote_mapping(
                        anime_list,
                        data_fribb,
                        data_kitsu_imdb,
                    )
                except AnimeMappingPayloadError as exc:
                    _log_refresh_failure("invalid_payload", exc)
                    return False

                del data_aod
                del data_fribb
                del data_kitsu_imdb
                del anime_list
                trim_process_memory()

                await self._load_mapping_caches()

                self.loaded = True
                log.info(
                    "anime.refresh.completed",
                    "Anime mapping refresh completed",
                    provider_name="anime-mapping",
                    operation=operation,
                    outcome="ok",
                    item_count=total_entries,
                    result_count=len(self.anime_imdb_ids),
                    duration_ms=(loop.time() - refresh_started) * 1000,
                )
                return True

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
                if not isinstance(entry, dict):
                    continue
                encoded_entry = orjson.dumps(entry)
                if len(encoded_entry) > _MAX_ENTRY_JSON_BYTES:
                    continue
                entry_id = idx + 1
                entries_batch.append(
                    {
                        "id": entry_id,
                        "data_json": encoded_entry.decode("utf-8"),
                    }
                )

                sources = entry.get("sources")
                if sources is not None:
                    if (
                        not isinstance(sources, list)
                        or len(sources) > _MAX_SOURCES_PER_ENTRY
                    ):
                        sources = []
                    for source in sources:
                        identity = _provider_identity(source)
                        if identity is None:
                            continue
                        provider, provider_id = identity
                        identity_key = (provider, provider_id)
                        if identity_key in lookup_map:
                            raise AnimeMappingPayloadError("duplicate anime identity")
                        lookup_map[identity_key] = entry_id
                        ids_batch.append(
                            {
                                "provider": provider,
                                "provider_id": provider_id,
                                "entry_id": entry_id,
                            }
                        )
                        if len(lookup_map) > _MAX_IDENTITIES:
                            raise AnimeMappingPayloadError("too many anime identities")

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
                if not isinstance(entry, dict):
                    continue
                imdb_id = entry.get("imdb_id")
                if not imdb_id:
                    continue

                # Fribb stores imdb_id as a list for multi-season entries.
                # SQLite can't bind a list, so normalize to individual ids.
                imdb_ids = imdb_id if isinstance(imdb_id, list) else [imdb_id]

                for provider, key in _FRIBB_PROVIDER_KEYS.items():
                    provider_id = _provider_id(entry.get(key))
                    if provider_id is not None:
                        found_entry_id = lookup_map.get((provider, provider_id))
                        if found_entry_id is not None:
                            for single_imdb_id in imdb_ids:
                                if not single_imdb_id:
                                    continue
                                single_imdb_id = _imdb_id(single_imdb_id)
                                if single_imdb_id is None:
                                    continue
                                fribb_batch.append(
                                    {
                                        "provider": "imdb",
                                        "provider_id": single_imdb_id,
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

    async def _persist_provider_overrides(self, kitsu_imdb_data: list):
        total_count = 0
        batch = []
        batch_size = 1000

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

            seen_sources = set()
            for entry in kitsu_imdb_data:
                if not isinstance(entry, dict):
                    continue
                kitsu_id = _provider_id(entry.get("kitsu_id"))
                if (
                    kitsu_id is None
                    or not kitsu_id.isdigit()
                    or kitsu_id in seen_sources
                ):
                    continue
                seen_sources.add(kitsu_id)

                raw_imdb_id = entry.get("imdb_id")
                if not raw_imdb_id:
                    continue
                imdb_id = _imdb_id(raw_imdb_id)
                if imdb_id is None:
                    continue

                try:
                    from_season = _coordinate(entry.get("fromSeason"))
                    from_episode = _coordinate(entry.get("fromEpisode"))
                except AnimeMappingPayloadError:
                    continue

                batch.append(
                    {
                        "source_id": kitsu_id,
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


anime_mapper = AnimeMapper()
