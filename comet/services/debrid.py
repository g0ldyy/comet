import asyncio

import orjson
from RTN import ParsedData

from comet.debrid.manager import retrieve_debrid_availability
from comet.services.debrid_cache import (cache_availability,
                                         get_cached_availability)
from comet.utils.parsing import ensure_multi_language


class DebridService:
    def __init__(self, debrid_service: str, debrid_api_key: str, ip: str):
        self.debrid_service = debrid_service
        self.debrid_api_key = debrid_api_key
        self.ip = ip

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

                debrid_parsed = file["parsed"]
                if debrid_parsed is not None:
                    original_parsed = torrent.get("parsed")
                    if original_parsed is not None:
                        if (
                            debrid_parsed.quality is None
                            and original_parsed.quality is not None
                        ):
                            debrid_parsed.quality = original_parsed.quality
                        if not debrid_parsed.languages and original_parsed.languages:
                            debrid_parsed.languages = original_parsed.languages
                    torrent["parsed"] = debrid_parsed
                if file["index"] is not None:
                    torrent["fileIndex"] = file["index"]
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

                if row["file_index"] is not None:
                    try:
                        torrent["fileIndex"] = int(row["file_index"])
                    except ValueError:
                        pass

                if row["size"] is not None:
                    torrent["size"] = row["size"]

                if row["parsed"] is not None:
                    cached_parsed = ParsedData(**orjson.loads(row["parsed"]))
                    ensure_multi_language(cached_parsed)

                    original_parsed = torrent.get("parsed")
                    original_resolution = (
                        original_parsed.resolution if original_parsed else None
                    )
                    if (
                        cached_parsed.resolution != "unknown"
                        or original_resolution == "unknown"
                    ):
                        if (
                            original_parsed
                            and not cached_parsed.languages
                            and original_parsed.languages
                        ):
                            cached_parsed.languages = original_parsed.languages
                        torrent["parsed"] = cached_parsed
                        if row["title"] is not None:
                            torrent["title"] = row["title"]

        return cached_hashes
