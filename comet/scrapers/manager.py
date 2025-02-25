import aiohttp
import asyncio
import orjson
import time

from RTN import (
    parse,
    title_match,
    get_rank,
    check_fetch,
    sort_torrents,
    ParsedData,
    BestRanking,
    Torrent,
)

from comet.utils.models import settings, database, CometSettingsModel
from comet.utils.general import default_dump
from comet.debrid.manager import retrieve_debrid_availability
from .zilean import get_zilean
from .torrentio import get_torrentio
from .mediafusion import get_mediafusion
from .jackett import get_jackett
from .prowlarr import get_prowlarr


class TorrentManager:
    def __init__(
        self,
        debrid_service: str,
        debrid_api_key: str,
        ip: str,
        media_type: str,
        media_full_id: str,
        media_only_id: str,
        title: str,
        year: int,
        year_end: int,
        season: int,
        episode: int,
        aliases: dict,
        remove_adult_content: bool,
    ):
        self.debrid_service = debrid_service
        self.debrid_api_key = debrid_api_key
        self.ip = ip
        self.media_type = media_type
        self.media_id = media_full_id
        self.media_only_id = media_only_id
        self.title = title
        self.year = year
        self.year_end = year_end
        self.season = season
        self.episode = episode
        self.aliases = aliases
        self.remove_adult_content = remove_adult_content

        self.seen_hashes = set()
        self.torrents = {}
        self.ready_to_cache = []
        self.ranked_torrents = {}

    async def scrape_torrents(
        self,
        session: aiohttp.ClientSession,
    ):
        tasks = []
        if settings.SCRAPE_TORRENTIO:
            tasks.append(get_torrentio(self, self.media_type, self.media_id))
        if settings.SCRAPE_MEDIAFUSION:
            tasks.append(get_mediafusion(self, self.media_type, self.media_id))
        if settings.SCRAPE_ZILEAN:
            tasks.append(
                get_zilean(self, session, self.title, self.season, self.episode)
            )
        if settings.INDEXER_MANAGER_API_KEY:
            queries = [self.title]

            if self.media_type == "series":
                queries.append(f"{self.title} S{self.season:02d}")
                queries.append(f"{self.title} S{self.season:02d}E{self.episode:02d}")

            seen_already = set()
            for query in queries:
                if settings.INDEXER_MANAGER_TYPE == "jackett":
                    tasks.append(get_jackett(self, session, query, seen_already))
                elif settings.INDEXER_MANAGER_TYPE == "prowlarr":
                    tasks.append(get_prowlarr(self, session, query, seen_already))

        await asyncio.gather(*tasks)
        asyncio.create_task(self.cache_torrents())

        for torrent in self.ready_to_cache:
            season = torrent["parsed"].seasons[0] if torrent["parsed"].seasons else None
            episode = (
                torrent["parsed"].episodes[0] if torrent["parsed"].episodes else None
            )

            if (season is not None and season != self.season) or (
                episode is not None and episode != self.episode
            ):
                continue

            info_hash = torrent["infoHash"]
            self.torrents[info_hash] = {
                "fileIndex": torrent["fileIndex"],
                "title": torrent["title"],
                "seeders": torrent["seeders"],
                "size": torrent["size"],
                "tracker": torrent["tracker"],
                "sources": torrent["sources"],
                "parsed": torrent["parsed"],
            }

    async def get_cached_torrents(self):
        rows = await database.fetch_all(
            """
                SELECT info_hash, file_index, title, seeders, size, tracker, sources, parsed
                FROM torrents
                WHERE media_id = :media_id
                AND ((season IS NOT NULL AND season = cast(:season as INTEGER)) OR (season IS NULL AND cast(:season as INTEGER) IS NULL))
                AND (episode IS NULL OR episode = cast(:episode as INTEGER))
                AND timestamp + :cache_ttl >= :current_time
            """,
            {
                "media_id": self.media_only_id,
                "season": self.season,
                "episode": self.episode,
                "cache_ttl": settings.TORRENT_CACHE_TTL,
                "current_time": time.time(),
            },
        )

        for row in rows:
            info_hash = row["info_hash"]
            self.torrents[info_hash] = {
                "fileIndex": row["file_index"],
                "title": row["title"],
                "seeders": row["seeders"],
                "size": row["size"],
                "tracker": row["tracker"],
                "sources": orjson.loads(row["sources"]),
                "parsed": ParsedData(**orjson.loads(row["parsed"])),
            }

    async def cache_torrents(self):
        current_time = time.time()
        values = [
            {
                "media_id": self.media_only_id,
                "info_hash": torrent["infoHash"],
                "file_index": torrent["fileIndex"],
                "season": torrent["parsed"].seasons[0]
                if torrent["parsed"].seasons
                else self.season,
                "episode": torrent["parsed"].episodes[0]
                if torrent["parsed"].episodes
                else None,
                "title": torrent["title"],
                "seeders": torrent["seeders"],
                "size": torrent["size"],
                "tracker": torrent["tracker"],
                "sources": orjson.dumps(torrent["sources"]).decode("utf-8"),
                "parsed": orjson.dumps(torrent["parsed"], default_dump).decode("utf-8"),
                "timestamp": current_time,
            }
            for torrent in self.ready_to_cache
        ]

        query = f"""
            INSERT {"OR IGNORE " if settings.DATABASE_TYPE == "sqlite" else ""}
            INTO torrents
            VALUES (:media_id, :info_hash, :file_index, :season, :episode, :title, :seeders, :size, :tracker, :sources, :parsed, :timestamp)
            {" ON CONFLICT DO NOTHING" if settings.DATABASE_TYPE == "postgresql" else ""}
        """

        await database.execute_many(query, values)

    async def filter(self, torrents: list):
        title = self.title
        year = self.year
        year_end = self.year_end
        aliases = self.aliases
        remove_adult_content = self.remove_adult_content

        for torrent in torrents:
            parsed = parse(torrent["title"])

            if remove_adult_content and parsed.adult:
                continue

            if parsed.parsed_title and not title_match(
                title, parsed.parsed_title, aliases=aliases
            ):
                continue

            if year and parsed.year:
                if year_end is not None:
                    if not (year <= parsed.year <= year_end):
                        continue
                else:
                    if year < (parsed.year - 1) or year > (parsed.year + 1):
                        continue

            torrent["parsed"] = parsed
            self.ready_to_cache.append(torrent)

    async def filter_manager(self, torrents: list):
        new_torrents = [
            torrent
            for torrent in torrents
            if (torrent["infoHash"], torrent["title"]) not in self.seen_hashes
        ]
        self.seen_hashes.update(
            (torrent["infoHash"], torrent["title"]) for torrent in new_torrents
        )

        chunk_size = 50
        tasks = [
            self.filter(new_torrents[i : i + chunk_size])
            for i in range(0, len(new_torrents), chunk_size)
        ]
        await asyncio.gather(*tasks)

    def rank_torrents(
        self,
        rtn_settings: CometSettingsModel,
        rtn_ranking: BestRanking,
        max_results_per_resolution: int,
        max_size: int,
        cached_only: int,
        remove_trash: int,
    ):
        ranked_torrents = set()
        for info_hash, torrent in self.torrents.items():
            if (
                cached_only
                and self.debrid_service != "torrent"
                and not torrent["cached"]
            ):
                continue

            if max_size != 0 and torrent["size"] > max_size:
                continue

            parsed = torrent["parsed"]

            raw_title = torrent["title"]

            is_fetchable, failed_keys = check_fetch(parsed, rtn_settings)
            rank = get_rank(parsed, rtn_settings, rtn_ranking)

            if remove_trash:
                if (
                    not is_fetchable
                    or rank < rtn_settings.options["remove_ranks_under"]
                ):
                    continue

            try:
                ranked_torrents.add(
                    Torrent(
                        infohash=info_hash,
                        raw_title=raw_title,
                        data=parsed,
                        fetch=is_fetchable,
                        rank=rank,
                        lev_ratio=0.0,
                    )
                )
            except Exception:
                pass

        self.ranked_torrents = sort_torrents(
            ranked_torrents, max_results_per_resolution
        )

    async def get_and_cache_debrid_availability(self, session: aiohttp.ClientSession):
        if self.debrid_service == "torrent" or len(self.torrents) == 0:
            return

        info_hashes = list(self.torrents.keys())

        seeders_map = {hash: self.torrents[hash]["seeders"] for hash in info_hashes}
        tracker_map = {hash: self.torrents[hash]["tracker"] for hash in info_hashes}
        sources_map = {hash: self.torrents[hash]["sources"] for hash in info_hashes}

        availability = await retrieve_debrid_availability(
            session,
            self.media_id,
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

        is_not_offcloud = self.debrid_service != "offcloud"
        for file in availability:
            season = file["season"]
            episode = file["episode"]
            if (season is not None and season != self.season) or (
                episode is not None and episode != self.episode
            ):
                continue

            info_hash = file["info_hash"]
            self.torrents[info_hash]["cached"] = True

            if is_not_offcloud:
                self.torrents[info_hash]["parsed"] = file["parsed"]
                self.torrents[info_hash]["fileIndex"] = file["index"]
                self.torrents[info_hash]["title"] = file["title"]
                self.torrents[info_hash]["size"] = file["size"]

        asyncio.create_task(self._background_cache_availability(availability))

    async def _background_cache_availability(self, availability: list):
        current_time = time.time()

        is_not_offcloud = self.debrid_service != "offcloud"
        values = [
            {
                "debrid_service": self.debrid_service,
                "info_hash": file["info_hash"],
                "file_index": str(file["index"]) if is_not_offcloud else None,
                "title": file["title"],
                "season": file["season"],
                "episode": file["episode"],
                "size": file["size"],
                "parsed": orjson.dumps(file["parsed"], default_dump).decode("utf-8")
                if is_not_offcloud
                else None,
                "timestamp": current_time,
            }
            for file in availability
        ]

        query = f"""
            INSERT {"OR IGNORE " if settings.DATABASE_TYPE == "sqlite" else ""}
            INTO debrid_availability (debrid_service, info_hash, file_index, title, season, episode, size, parsed, timestamp)
            VALUES (:debrid_service, :info_hash, :file_index, :title, :season, :episode, :size, :parsed, :timestamp)
            {" ON CONFLICT DO NOTHING" if settings.DATABASE_TYPE == "postgresql" else ""}
        """

        await database.execute_many(query, values)

    async def get_cached_availability(self):
        info_hashes = list(self.torrents.keys())
        for hash in info_hashes:
            self.torrents[hash]["cached"] = False

        if self.debrid_service == "torrent" or len(self.torrents) == 0:
            return

        query = f"""
            SELECT info_hash, file_index, title, size, parsed
            FROM debrid_availability
            WHERE info_hash IN (SELECT cast(value as TEXT) FROM {"json_array_elements_text" if settings.DATABASE_TYPE == "postgresql" else "json_each"}(:info_hashes))
            AND debrid_service = :debrid_service
            AND timestamp + :cache_ttl >= :current_time
        """
        params = {
            "info_hashes": orjson.dumps(info_hashes).decode("utf-8"),
            "debrid_service": self.debrid_service,
            "cache_ttl": settings.DEBRID_CACHE_TTL,
            "current_time": time.time(),
        }
        if self.debrid_service != "offcloud":
            query += """
            AND ((cast(:season as INTEGER) IS NULL AND season IS NULL) OR season = cast(:season as INTEGER))
            AND ((cast(:episode as INTEGER) IS NULL AND episode IS NULL) OR episode = cast(:episode as INTEGER))
            """
            params["season"] = self.season
            params["episode"] = self.episode

        is_not_offcloud = self.debrid_service != "offcloud"

        rows = await database.fetch_all(query, params)
        for row in rows:
            info_hash = row["info_hash"]
            self.torrents[info_hash]["cached"] = True

            if is_not_offcloud:
                self.torrents[info_hash]["parsed"] = ParsedData(
                    **orjson.loads(row["parsed"])
                )
                self.torrents[info_hash]["fileIndex"] = row["file_index"]
                self.torrents[info_hash]["title"] = row["title"]
                self.torrents[info_hash]["size"] = row["size"]
