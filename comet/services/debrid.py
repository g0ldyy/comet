import asyncio

from comet.debrid.manager import retrieve_debrid_availability
from comet.services.debrid_cache import (cache_availability,
                                         get_cached_availability)


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

        asyncio.create_task(cache_availability(self.debrid_service, availability))
        return cached_hashes

    async def check_existing_availability(
        self, info_hashes: list, season: int, episode: int
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

        return cached_hashes
