import asyncio

import orjson
from RTN import ParsedData

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

        if len(torrents) == 0:
            return

        rows = await get_cached_availability(
            self.debrid_service, info_hashes, season, episode
        )

        for row in rows:
            info_hash = row["info_hash"]
            torrents[info_hash]["cached"] = True

            if row["file_index"] is not None:
                try:
                    torrents[info_hash]["fileIndex"] = int(row["file_index"])
                except ValueError:
                    pass

            if row["size"] is not None:
                torrents[info_hash]["size"] = row["size"]

            # Only update title/parsed if the cached file has resolution info
            # Otherwise keep the original torrent info which may have better quality data
            # E.g. torrent "[Group] Show S01 1080p" vs file "Show - 02.mkv"
            if row["parsed"] is not None:
                cached_parsed = ParsedData(**orjson.loads(row["parsed"]))
                if (
                    cached_parsed.resolution != "unknown"
                    or torrents[info_hash]["parsed"].resolution == "unknown"
                ):
                    torrents[info_hash]["parsed"] = cached_parsed
                    if row["title"] is not None:
                        torrents[info_hash]["title"] = row["title"]
