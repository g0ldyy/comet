import time

from RTN import ParsedData

from comet.debrid.exceptions import DebridAuthError
from comet.debrid.file_selection import select_best_availability_files
from comet.debrid.manager import retrieve_debrid_availability
from comet.observability import metrics
from comet.services.debrid_cache import (
    get_cached_availability,
    get_cached_availability_any_service,
    schedule_cache_availability,
    schedule_cache_availability_after_response,
)
from comet.utils.parsing import MediaScope, ensure_multi_language, load_cached_parsed


class DebridService:
    def __init__(self, debrid_service: str, debrid_api_key: str, ip: str):
        self.debrid_service = debrid_service
        self.debrid_api_key = debrid_api_key
        self.ip = ip

    @staticmethod
    def _coerce_file_index(value):
        return None if value is None else int(value)

    @staticmethod
    def _backfill_attr(merged: ParsedData, original: ParsedData, attr: str):
        merged_value = getattr(merged, attr)
        original_value = getattr(original, attr)
        if not merged_value and original_value:
            setattr(merged, attr, original_value)

    @staticmethod
    def _merge_parsed(
        original: ParsedData | None, incoming: ParsedData | None
    ) -> ParsedData | None:
        if incoming is None:
            return original
        if original is None:
            ensure_multi_language(incoming)
            return incoming

        merged = incoming

        incoming_resolution = incoming.resolution
        original_resolution = original.resolution
        if incoming_resolution in (None, "unknown") and original_resolution not in (
            None,
            "unknown",
        ):
            merged.resolution = original_resolution

        for attr in (
            "quality",
            "languages",
            "audio",
            "channels",
            "codec",
            "hdr",
            "bit_depth",
            "group",
        ):
            DebridService._backfill_attr(merged, original, attr)

        ensure_multi_language(merged)
        return merged

    @classmethod
    def _build_torrent_update(
        cls,
        torrent: dict,
        *,
        file_index,
        title: str | None,
        size: int | None,
        parsed: ParsedData | None,
    ) -> dict:
        update = {}
        original_parsed = torrent.get("parsed")
        metadata_is_downgrade = (
            parsed is not None
            and parsed.trash
            and isinstance(original_parsed, ParsedData)
            and not original_parsed.trash
        )

        if parsed is not None and not metadata_is_downgrade:
            merged_parsed = cls._merge_parsed(original_parsed, parsed)
            if merged_parsed is not None:
                update["parsed"] = merged_parsed

        file_index = cls._coerce_file_index(file_index)
        if file_index is not None:
            update["fileIndex"] = file_index
        if title is not None and not metadata_is_downgrade:
            update["title"] = title
        if size is not None:
            update["size"] = size

        return update

    async def get_and_cache_availability(
        self,
        session,
        info_hashes: list[str],
        seeders_map: dict,
        tracker_map: dict,
        sources_map: dict,
        torrents: dict | None,
        media_id: str,
        media_only_id: str,
        season: int | None,
        episode: int | None,
        media_scope: MediaScope,
        target_air_date: str | None = None,
        add_background_task=None,
    ) -> tuple[set[str], dict[str, dict]]:
        started_at = time.perf_counter() if metrics.enabled else 0.0
        outcome = "success"
        try:
            availability = await retrieve_debrid_availability(
                session,
                media_id,
                media_only_id,
                self.debrid_service,
                self.debrid_api_key,
                self.ip,
                info_hashes,
                seeders_map,
                tracker_map,
                sources_map,
                target_air_date=target_air_date,
            )
        except DebridAuthError:
            outcome = "auth_error"
            raise
        except Exception:
            outcome = "error"
            raise
        finally:
            if metrics.enabled:
                metrics.observe_debrid(
                    self.debrid_service,
                    "availability",
                    outcome,
                    time.perf_counter() - started_at,
                    len(availability) if "availability" in locals() else 0,
                )

        availability = select_best_availability_files(availability, torrents)
        if len(availability) == 0:
            return set(), {}

        info_hash_set = set(info_hashes)
        cached_hashes = set()
        torrent_updates = {}
        for file in availability:
            if not media_scope.matches_file(
                season,
                episode,
                file["season"],
                file["episode"],
            ):
                continue

            info_hash = file["info_hash"]
            if info_hash not in info_hash_set:
                continue
            cached_hashes.add(info_hash)
            if torrents is not None and not media_scope.is_aggregate:
                torrent = torrents.get(info_hash)
                if torrent is None:
                    continue

                update = self._build_torrent_update(
                    torrent,
                    file_index=file["index"],
                    title=file["title"],
                    size=file["size"],
                    parsed=file["parsed"],
                )
                if update:
                    torrent_updates.setdefault(info_hash, {}).update(update)

        if add_background_task is None:
            schedule_cache_availability(self.debrid_service, availability)
        else:
            add_background_task(
                schedule_cache_availability_after_response,
                self.debrid_service,
                availability,
            )
        return cached_hashes, torrent_updates

    async def check_existing_availability(
        self,
        info_hashes: list,
        season: int | None,
        episode: int | None,
        media_scope: MediaScope,
        torrents: dict | None,
    ) -> tuple[set[str], dict[str, dict]]:
        if len(info_hashes) == 0:
            return set(), {}

        started_at = time.perf_counter() if metrics.enabled else 0.0
        try:
            rows = await get_cached_availability(
                self.debrid_service,
                info_hashes,
                media_scope,
                season,
                episode,
            )
        except Exception:
            if metrics.enabled:
                metrics.observe_debrid(
                    self.debrid_service,
                    "cache_lookup",
                    "error",
                    time.perf_counter() - started_at,
                    0,
                )
            raise
        if metrics.enabled:
            metrics.observe_debrid(
                self.debrid_service,
                "cache_lookup",
                "hit" if rows else "miss",
                time.perf_counter() - started_at,
                len(rows),
            )

        if media_scope.is_aggregate:
            return {row["info_hash"] for row in rows}, {}

        rows = select_best_availability_files(
            (
                {
                    "info_hash": row["info_hash"],
                    "index": row["file_index"],
                    "title": row["title"],
                    "size": row["size"],
                    "season": season,
                    "episode": episode,
                    "parsed": load_cached_parsed(row["parsed"]),
                }
                for row in rows
            ),
            torrents,
        )

        cached_hashes = set()
        torrent_updates = {}
        for row in rows:
            info_hash = row["info_hash"]
            cached_hashes.add(info_hash)
            if torrents is not None and not media_scope.is_aggregate:
                torrent = torrents.get(info_hash)
                if torrent is None:
                    continue

                update = self._build_torrent_update(
                    torrent,
                    file_index=row["index"],
                    title=row["title"],
                    size=row["size"],
                    parsed=row["parsed"],
                )
                if update:
                    torrent_updates[info_hash] = update

        return cached_hashes, torrent_updates

    @classmethod
    async def apply_cached_availability_any_service(
        cls,
        info_hashes: list,
        season: int | None,
        episode: int | None,
        media_scope: MediaScope,
        torrents: dict | None,
    ):
        if len(info_hashes) == 0 or torrents is None or media_scope.is_aggregate:
            return

        rows = await get_cached_availability_any_service(info_hashes, season, episode)
        rows = select_best_availability_files(
            (
                {
                    "info_hash": row["info_hash"],
                    "index": row["file_index"],
                    "title": row["title"],
                    "size": row["size"],
                    "season": season,
                    "episode": episode,
                    "parsed": load_cached_parsed(row["parsed"]),
                }
                for row in rows
            ),
            torrents,
        )

        for row in rows:
            info_hash = row["info_hash"]
            torrent = torrents.get(info_hash)
            if torrent is None:
                continue

            update = cls._build_torrent_update(
                torrent,
                file_index=row["index"],
                title=row["title"],
                size=row["size"],
                parsed=row["parsed"],
            )
            torrent.update(update)
