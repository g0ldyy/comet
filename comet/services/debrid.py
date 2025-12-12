import asyncio

import orjson
from RTN import ParsedData

from comet.core.logger import logger
from comet.debrid.manager import get_debrid, retrieve_debrid_availability
from comet.services.debrid_cache import (cache_availability,
                                         get_cached_availability)


class DebridService:
    def __init__(self, debrid_service: str, debrid_api_key: str, ip: str):
        self.debrid_service = debrid_service
        self.debrid_api_key = debrid_api_key
        self.ip = ip

    async def validate_credentials(
        self, session, media_id: str, media_only_id: str
    ) -> bool:
        if self.debrid_service == "torrent":
            return True

        try:
            client = get_debrid(
                session,
                media_id,
                media_only_id,
                self.debrid_service,
                self.debrid_api_key,
                self.ip,
            )
            if client is None:
                return False
            return await client.check_premium()
        except Exception as e:
            logger.warning(
                f"Failed to validate credentials for {self.debrid_service}: {e}"
            )
            return False

    async def get_and_cache_availability(
        self,
        session,
        torrents: dict,
        media_id: str,
        media_only_id: str,
        season: int,
        episode: int,
    ):
        info_hashes = list(torrents.keys())

        seeders_map = {hash: torrents[hash]["seeders"] for hash in info_hashes}
        tracker_map = {hash: torrents[hash]["tracker"] for hash in info_hashes}
        sources_map = {hash: torrents[hash]["sources"] for hash in info_hashes}

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
            return

        for file in availability:
            file_season = file["season"]
            file_episode = file["episode"]
            if (file_season is not None and file_season != season) or (
                file_episode is not None and file_episode != episode
            ):
                continue

            info_hash = file["info_hash"]
            torrents[info_hash]["cached"] = True

            debrid_parsed = file["parsed"]
            if debrid_parsed is not None:
                if (
                    debrid_parsed.quality is None
                    and torrents[info_hash]["parsed"].quality is not None
                ):
                    debrid_parsed.quality = torrents[info_hash]["parsed"].quality
                torrents[info_hash]["parsed"] = debrid_parsed
            if file["index"] is not None:
                torrents[info_hash]["fileIndex"] = file["index"]
            if file["title"] is not None:
                torrents[info_hash]["title"] = file["title"]
            if file["size"] is not None:
                torrents[info_hash]["size"] = file["size"]

        asyncio.create_task(cache_availability(self.debrid_service, availability))

    async def check_existing_availability(
        self, torrents: dict, season: int, episode: int
    ):
        info_hashes = list(torrents.keys())
        for hash in info_hashes:
            torrents[hash]["cached"] = False

        if self.debrid_service == "torrent" or len(torrents) == 0:
            return

        rows = await get_cached_availability(
            self.debrid_service, info_hashes, season, episode
        )

        for row in rows:
            info_hash = row["info_hash"]
            torrents[info_hash]["cached"] = True

            if row["parsed"] is not None:
                torrents[info_hash]["parsed"] = ParsedData(
                    **orjson.loads(row["parsed"])
                )
            if row["file_index"] is not None:
                torrents[info_hash]["fileIndex"] = row["file_index"]
            if row["title"] is not None:
                torrents[info_hash]["title"] = row["title"]
            if row["size"] is not None:
                torrents[info_hash]["size"] = row["size"]
