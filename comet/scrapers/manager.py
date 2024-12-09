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
    SettingsModel,
    BestRanking,
    Torrent,
)

from comet.utils.models import settings, database
from comet.debrid.manager import retrieve_debrid_availability
from .zilean import get_zilean
from .torrentio import get_torrentio
from .mediafusion import get_mediafusion


def default(obj):
    if isinstance(obj, ParsedData):
        return obj.model_dump()


class TorrentManager:
    def __init__(
        self,
        debrid_service: str,
        debrid_api_key: str,
        ip: str,
        media_type: str,
        media_id: str,
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
        self.media_id = media_id
        self.title = title
        self.year = year
        self.year_end = year_end
        self.season = season
        self.episode = episode
        self.aliases = aliases
        self.remove_adult_content = remove_adult_content

        self.seen_hashes = set()
        self.torrents = {}
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

        await asyncio.gather(*tasks)

        await self.cache_torrents()

    async def get_cached_torrents(self):
        rows = await database.fetch_all(
            """
                SELECT data
                FROM torrents_cache
                WHERE media_id = :media_id
                AND ((cast(:season as INTEGER) IS NULL AND season IS NULL) OR season = cast(:season as INTEGER))
                AND ((cast(:episode as INTEGER) IS NULL AND episode IS NULL) OR episode = cast(:episode as INTEGER))
                AND timestamp + :cache_ttl >= :current_time
            """,
            {
                "media_id": self.media_id,
                "season": self.season,
                "episode": self.episode,
                "cache_ttl": settings.CACHE_TTL,
                "current_time": time.time(),
            },
        )
        for row in rows:
            data = orjson.loads(row["data"])

            data["parsed"] = ParsedData(**data["parsed"])

            self.torrents[data["infoHash"]] = data

    async def cache_torrents(self):
        current_time = time.time()
        values = [
            {
                "info_hash": info_hash,
                "media_id": self.media_id,
                "season": self.season,
                "episode": self.episode,
                "data": orjson.dumps(torrent, default),
                "timestamp": current_time,
            }
            for info_hash, torrent in self.torrents.items()
        ]

        query = f"""
            INSERT {'OR IGNORE ' if settings.DATABASE_TYPE == 'sqlite' else ''}
            INTO torrents_cache
            VALUES (:info_hash, :media_id, :season, :episode, :data, :timestamp)
            {' ON CONFLICT DO NOTHING' if settings.DATABASE_TYPE == 'postgresql' else ''}
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

            self.torrents[torrent["infoHash"]] = torrent

    async def filter_manager(self, torrents: list):
        new_torrents = [
            torrent
            for torrent in torrents
            if torrent["infoHash"] not in self.seen_hashes
        ]
        self.seen_hashes.update(torrent["infoHash"] for torrent in new_torrents)

        chunk_size = 50
        tasks = []
        for i in range(0, len(new_torrents), chunk_size):
            chunk = new_torrents[i : i + chunk_size]
            tasks.append(self.filter(chunk))

        await asyncio.gather(*tasks)

    def rank_torrents(
        self,
        rtn_settings: SettingsModel,
        rtn_ranking: BestRanking,
        max_results_per_resolution: int,
        max_size: int,
        cached_only: int,
        remove_trash: int,
    ):
        ranked_torrents = set()
        for info_hash, torrent in self.torrents.items():
            if cached_only and self.debrid_service != "torrent" and not torrent["cached"]:
                continue

            if max_size != 0 and torrent["size"] > max_size:
                continue

            parsed_data = torrent["parsed"]

            if self.media_type == "series":
                if parsed_data.episodes and self.episode not in parsed_data.episodes or parsed_data.seasons and self.season not in parsed_data.seasons:
                    continue

            raw_title = torrent["title"]

            is_fetchable, failed_keys = check_fetch(parsed_data, rtn_settings)
            rank = get_rank(parsed_data, rtn_settings, rtn_ranking)

            if remove_trash:
                if rtn_settings.options["remove_all_trash"]:
                    if not is_fetchable:
                        # print(f"'{raw_title}' denied by: {', '.join(failed_keys)}")
                        continue

                if rank < rtn_settings.options["remove_ranks_under"]:
                    # print(f"'{raw_title}' does not meet the minimum rank requirement, got rank of {rank}")
                    continue

            try:
                ranked_torrents.add(
                    Torrent(
                        infohash=info_hash,
                        raw_title=raw_title,
                        data=parsed_data,
                        fetch=is_fetchable,
                        rank=rank,
                        lev_ratio=0.0,
                    )
                )
            except:
                pass

        self.ranked_torrents = sort_torrents(
            ranked_torrents, max_results_per_resolution
        )

    async def get_and_cache_debrid_availability(self, session: aiohttp.ClientSession):
        info_hashes = list(self.torrents.keys())
        availability = await retrieve_debrid_availability(
            session, self.debrid_service, self.debrid_api_key, self.ip, info_hashes
        )

        if len(availability) == 0:
            return

        current_time = time.time()
        values = [
            {
                "debrid_service": self.debrid_service,
                "info_hash": file["info_hash"],
                "season": file["season"],
                "episode": file["episode"],
                "file_index": file["index"],
                "title": file["title"],
                "size": file["size"],
                "file_data": orjson.dumps(file["file_data"], default),
                "timestamp": current_time,
            }
            for file in availability
        ]

        query = f"""
            INSERT {'OR IGNORE ' if settings.DATABASE_TYPE == 'sqlite' else ''}
            INTO availability_cache
            VALUES (:debrid_service, :info_hash, :season, :episode, :file_index, :title, :size, :file_data, :timestamp)
            {' ON CONFLICT DO NOTHING' if settings.DATABASE_TYPE == 'postgresql' else ''}
        """

        await database.execute_many(query, values)

        for file in availability:
            info_hash = file["info_hash"]
            self.torrents[info_hash]["cached"] = True
            self.torrents[info_hash]["parsed"] = file["file_data"]
            self.torrents[info_hash]["fileIndex"] = file["index"]
            self.torrents[info_hash]["title"] = file["title"]
            self.torrents[info_hash]["size"] = file["size"]

    async def get_cached_availability(self):
        info_hashes = list(self.torrents.keys())
        for hash in info_hashes:
            self.torrents[hash]["cached"] = False
        if self.debrid_service == "torrent" or len(info_hashes) == 0:
            return

        query = f"""
            SELECT info_hash, file_index, title, size, file_data
            FROM availability_cache
            WHERE info_hash IN (SELECT cast(value as TEXT) FROM {'json_array_elements_text' if settings.DATABASE_TYPE == 'postgresql' else 'json_each'}(:info_hashes))
            AND debrid_service = :debrid_service
            AND ((cast(:season as INTEGER) IS NULL AND season IS NULL) OR season = cast(:season as INTEGER))
            AND ((cast(:episode as INTEGER) IS NULL AND episode IS NULL) OR episode = cast(:episode as INTEGER))
            AND timestamp + :cache_ttl >= :current_time
        """
        params = {
            "info_hashes": orjson.dumps(info_hashes),
            "debrid_service": self.debrid_service,
            "season": self.season,
            "episode": self.episode,
            "cache_ttl": settings.CACHE_TTL,
            "current_time": time.time(),
        }

        rows = await database.fetch_all(query, params)
        for row in rows:
            info_hash = row["info_hash"]
            self.torrents[info_hash]["cached"] = True
            self.torrents[info_hash]["parsed"] = ParsedData(**orjson.loads(row["file_data"]))
            self.torrents[info_hash]["fileIndex"] = row["file_index"]
            self.torrents[info_hash]["title"] = row["title"]
            self.torrents[info_hash]["size"] = row["size"]
