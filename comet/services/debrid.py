import asyncio

import orjson
from RTN import ParsedData

from comet.debrid.manager import retrieve_debrid_availability
from comet.services.debrid_cache import (cache_availability,
                                         get_cached_availability,
                                         get_cached_availability_any_service)
from comet.utils.parsing import ensure_multi_language


class DebridService:
    def __init__(self, debrid_service: str, debrid_api_key: str, ip: str):
        self.debrid_service = debrid_service
        self.debrid_api_key = debrid_api_key
        self.ip = ip

    @staticmethod
    def _coerce_file_index(value):
        if value is None:
            return None
        if isinstance(value, int):
            return value
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _backfill_attr(merged: ParsedData, original: ParsedData, attr: str):
        if not hasattr(merged, attr) or not hasattr(original, attr):
            return
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

        incoming_resolution = getattr(incoming, "resolution", None)
        original_resolution = getattr(original, "resolution", None)
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
            "bitDepth",
            "bit_depth",
            "group",
        ):
            DebridService._backfill_attr(merged, original, attr)

        ensure_multi_language(merged)
        return merged

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
        season: int,
        episode: int,
    ) -> set[str]:
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
        )

        if len(availability) == 0:
            return set()

        info_hash_set = set(info_hashes)
        cached_hashes = set()
        for file in availability:
            file_season = file["season"]
            file_episode = file["episode"]
            if (file_season is not None and file_season != season) or (
                file_episode is not None and file_episode != episode
            ):
                continue

            info_hash = file["info_hash"]
            if info_hash not in info_hash_set:
                continue
            cached_hashes.add(info_hash)
            if torrents is not None:
                torrent = torrents.get(info_hash)
                if torrent is None:
                    continue

                merged_parsed = self._merge_parsed(
                    torrent.get("parsed"), file["parsed"]
                )
                if merged_parsed is not None:
                    torrent["parsed"] = merged_parsed

                file_index = self._coerce_file_index(file["index"])
                if file_index is not None:
                    torrent["fileIndex"] = file_index
                if file["title"] is not None:
                    torrent["title"] = file["title"]
                if file["size"] is not None:
                    torrent["size"] = file["size"]

        asyncio.create_task(cache_availability(self.debrid_service, availability))
        return cached_hashes

    async def check_existing_availability(
        self, info_hashes: list, season: int, episode: int, torrents: dict | None
    ) -> set[str]:
        if len(info_hashes) == 0:
            return set()

        rows = await get_cached_availability(
            self.debrid_service, info_hashes, season, episode
        )

        cached_hashes = set()
        for row in rows:
            info_hash = row["info_hash"]
            cached_hashes.add(info_hash)
            if torrents is not None:
                torrent = torrents.get(info_hash)
                if torrent is None:
                    continue

                file_index = self._coerce_file_index(row["file_index"])
                if file_index is not None:
                    torrent["fileIndex"] = file_index

                if row["size"] is not None:
                    torrent["size"] = row["size"]

                if row["parsed"] is not None:
                    cached_parsed = ParsedData(**orjson.loads(row["parsed"]))
                    merged_parsed = self._merge_parsed(
                        torrent.get("parsed"), cached_parsed
                    )
                    if merged_parsed is not None:
                        torrent["parsed"] = merged_parsed

                if row["title"] is not None:
                    torrent["title"] = row["title"]

        return cached_hashes

    @classmethod
    async def apply_cached_availability_any_service(
        cls, info_hashes: list, season: int, episode: int, torrents: dict | None
    ):
        if len(info_hashes) == 0 or torrents is None:
            return

        rows = await get_cached_availability_any_service(info_hashes, season, episode)

        for row in rows:
            info_hash = row["info_hash"]
            torrent = torrents.get(info_hash)
            if torrent is None:
                continue

            file_index = cls._coerce_file_index(row["file_index"])
            if file_index is not None:
                torrent["fileIndex"] = file_index

            if row["size"] is not None:
                torrent["size"] = row["size"]

            if row["parsed"] is not None:
                cached_parsed = ParsedData(**orjson.loads(row["parsed"]))
                merged_parsed = cls._merge_parsed(torrent.get("parsed"), cached_parsed)
                if merged_parsed is not None:
                    torrent["parsed"] = merged_parsed

            if row["title"] is not None:
                torrent["title"] = row["title"]
