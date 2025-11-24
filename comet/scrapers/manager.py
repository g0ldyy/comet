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
    DefaultRanking,
    Torrent,
)

from comet.utils.models import settings, database, CometSettingsModel
from comet.utils.general import default_dump
from comet.utils.logger import logger
from comet.utils.debrid import cache_availability, get_cached_availability
from comet.debrid.manager import retrieve_debrid_availability
from comet.utils.general import associate_urls_credentials
from comet.utils.anime_mapper import anime_mapper
from .zilean import get_zilean
from .torrentio import get_torrentio
from .mediafusion import get_mediafusion
from .jackett import get_jackett
from .prowlarr import get_prowlarr
from .comet import get_comet
from .stremthru import get_stremthru
from .bitmagnet import get_bitmagnet
from .aiostreams import get_aiostreams
from .jackettio import get_jackettio
from .debridio import get_debridio
from .torbox import get_torbox
from .nyaa import get_nyaa


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
        context: str = "live",  # "live" or "background"
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
        self.context = context

        self.seen_hashes = set()
        self.torrents = {}
        self.ready_to_cache = []
        self.ranked_torrents = {}

    def is_anime_content(self):
        if "kitsu" in self.media_id:
            # All Kitsu content is anime
            return True

        if not anime_mapper.is_loaded():
            return True
        else:
            # Check if this IMDB ID corresponds to an anime
            return anime_mapper.is_anime(self.media_only_id)

    async def scrape_torrents(
        self,
        session: aiohttp.ClientSession,
    ):
        tasks = []
        if settings.is_scraper_enabled(settings.SCRAPE_COMET, self.context):
            tasks.extend(get_all_comet_tasks(self))
        if settings.is_scraper_enabled(settings.SCRAPE_TORRENTIO, self.context):
            tasks.extend(get_all_torrentio_tasks(self))
        if settings.is_scraper_enabled(settings.SCRAPE_MEDIAFUSION, self.context):
            tasks.extend(get_all_mediafusion_tasks(self))
        if settings.is_scraper_enabled(settings.SCRAPE_NYAA, self.context):
            should_use_nyaa = True

            if settings.NYAA_ANIME_ONLY:
                should_use_nyaa = self.is_anime_content()

            if should_use_nyaa:
                tasks.append(get_nyaa(self))
        if settings.is_scraper_enabled(settings.SCRAPE_ZILEAN, self.context):
            tasks.extend(get_all_zilean_tasks(self, session))
        if settings.is_scraper_enabled(settings.SCRAPE_STREMTHRU, self.context):
            tasks.extend(get_all_stremthru_tasks(self, session))
        if settings.is_scraper_enabled(settings.SCRAPE_BITMAGNET, self.context):
            tasks.extend(get_all_bitmagnet_tasks(self, session))
        if settings.is_scraper_enabled(settings.SCRAPE_AIOSTREAMS, self.context):
            tasks.extend(get_all_aiostreams_tasks(self))
        if settings.is_scraper_enabled(settings.SCRAPE_JACKETTIO, self.context):
            tasks.extend(get_all_jackettio_tasks(self))
        if settings.is_scraper_enabled(settings.SCRAPE_DEBRIDIO, self.context):
            tasks.append(get_debridio(self, session))
        if settings.is_scraper_enabled(settings.SCRAPE_TORBOX, self.context):
            tasks.append(get_torbox(self, session))
        if settings.INDEXER_MANAGER_API_KEY and settings.is_scraper_enabled(
            settings.INDEXER_MANAGER_MODE, self.context
        ):
            queries = [self.title]

            if self.media_type == "series" and self.episode is not None:
                queries.append(f"{self.title} S{self.season:02d}")
                queries.append(f"{self.title} S{self.season:02d}E{self.episode:02d}")

            seen_already = set()
            for query in queries:
                if settings.INDEXER_MANAGER_TYPE == "jackett":
                    tasks.append(get_jackett(self, session, query, seen_already))
                elif settings.INDEXER_MANAGER_TYPE == "prowlarr":
                    tasks.append(get_prowlarr(self, session, query, seen_already))

        await asyncio.gather(*tasks)
        await self.cache_torrents()  # Wait for cache to be written before continuing

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
                AND ((season IS NOT NULL AND season = CAST(:season as INTEGER)) OR (season IS NULL AND CAST(:season as INTEGER) IS NULL))
                AND (episode IS NULL OR episode = CAST(:episode as INTEGER))
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
                "file_index": int(torrent["fileIndex"])
                if torrent["fileIndex"] is not None
                else None,
                "season": torrent["parsed"].seasons[0]
                if torrent["parsed"].seasons
                else self.season,
                "episode": torrent["parsed"].episodes[0]
                if torrent["parsed"].episodes
                else None,
                "title": torrent["title"],
                "seeders": int(torrent["seeders"])
                if torrent["seeders"] is not None
                else None,
                "size": int(torrent["size"]) if torrent["size"] is not None else None,
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
            torrent_title = torrent["title"]
            if "sample" in torrent_title.lower() or torrent_title == "":
                continue

            parsed = parse(torrent_title)

            if remove_adult_content and parsed.adult:
                continue

            if not parsed.parsed_title or not title_match(
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

    async def filter_manager(self, scraper_name: str, torrents: list):
        if len(torrents) == 0:
            logger.log("SCRAPER", f"Scraper {scraper_name} found 0 torrents.")
            return

        new_torrents = [
            torrent
            for torrent in torrents
            if (torrent["infoHash"], torrent["title"]) not in self.seen_hashes
        ]

        self.seen_hashes.update(
            (torrent["infoHash"], torrent["title"]) for torrent in new_torrents
        )

        logger.log(
            "SCRAPER",
            f"Scraper {scraper_name} found {len(torrents)} torrents, {len(new_torrents)} new.",
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
        rtn_ranking: DefaultRanking,
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
        info_hashes = list(self.torrents.keys())

        seeders_map = {hash: self.torrents[hash]["seeders"] for hash in info_hashes}
        tracker_map = {hash: self.torrents[hash]["tracker"] for hash in info_hashes}
        sources_map = {hash: self.torrents[hash]["sources"] for hash in info_hashes}

        availability = await retrieve_debrid_availability(
            session,
            self.media_id,
            self.media_only_id,
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
            season = file["season"]
            episode = file["episode"]
            if (season is not None and season != self.season) or (
                episode is not None and episode != self.episode
            ):
                continue

            info_hash = file["info_hash"]
            self.torrents[info_hash]["cached"] = True

            debrid_parsed = file["parsed"]
            if debrid_parsed is not None:
                if (
                    debrid_parsed.quality is None
                    and self.torrents[info_hash]["parsed"].quality is not None
                ):
                    debrid_parsed.quality = self.torrents[info_hash]["parsed"].quality
                self.torrents[info_hash]["parsed"] = debrid_parsed
            if file["index"] is not None:
                self.torrents[info_hash]["fileIndex"] = file["index"]
            if file["title"] is not None:
                self.torrents[info_hash]["title"] = file["title"]
            if file["size"] is not None:
                self.torrents[info_hash]["size"] = file["size"]

        asyncio.create_task(cache_availability(self.debrid_service, availability))

    async def get_cached_availability(self):
        info_hashes = list(self.torrents.keys())
        for hash in info_hashes:
            self.torrents[hash]["cached"] = False

        if self.debrid_service == "torrent" or len(self.torrents) == 0:
            return

        rows = await get_cached_availability(
            self.debrid_service, info_hashes, self.season, self.episode
        )

        for row in rows:
            info_hash = row["info_hash"]
            self.torrents[info_hash]["cached"] = True

            if row["parsed"] is not None:
                self.torrents[info_hash]["parsed"] = ParsedData(
                    **orjson.loads(row["parsed"])
                )
            if row["file_index"] is not None:
                self.torrents[info_hash]["fileIndex"] = row["file_index"]
            if row["title"] is not None:
                self.torrents[info_hash]["title"] = row["title"]
            if row["size"] is not None:
                self.torrents[info_hash]["size"] = row["size"]


# multi-instance scraping
def get_all_comet_tasks(manager):
    urls = settings.COMET_URL
    if isinstance(urls, str):
        urls = [urls]

    tasks = []
    for url in urls:
        tasks.append(get_comet(manager, url))
    return tasks


def get_all_torrentio_tasks(manager):
    urls = settings.TORRENTIO_URL
    if isinstance(urls, str):
        urls = [urls]

    tasks = []
    for url in urls:
        tasks.append(get_torrentio(manager, url))
    return tasks


def get_all_mediafusion_tasks(manager):
    url_credentials_pairs = associate_urls_credentials(
        settings.MEDIAFUSION_URL, settings.MEDIAFUSION_API_PASSWORD
    )

    tasks = []
    for url, password in url_credentials_pairs:
        tasks.append(get_mediafusion(manager, url, password))
    return tasks


def get_all_zilean_tasks(manager, session):
    urls = settings.ZILEAN_URL
    if isinstance(urls, str):
        urls = [urls]

    tasks = []
    for url in urls:
        tasks.append(get_zilean(manager, session, url))
    return tasks


def get_all_stremthru_tasks(manager, session):
    urls = settings.STREMTHRU_SCRAPE_URL
    if isinstance(urls, str):
        urls = [urls]

    tasks = []
    for url in urls:
        tasks.append(get_stremthru(manager, session, url))
    return tasks


def get_all_bitmagnet_tasks(manager, session):
    urls = settings.BITMAGNET_URL
    if isinstance(urls, str):
        urls = [urls]

    tasks = []
    for url in urls:
        tasks.append(get_bitmagnet(manager, session, url))
    return tasks


def get_all_aiostreams_tasks(manager):
    url_credentials_pairs = associate_urls_credentials(
        settings.AIOSTREAMS_URL, settings.AIOSTREAMS_USER_UUID_AND_PASSWORD
    )

    tasks = []
    for url, uuid_password in url_credentials_pairs:
        tasks.append(get_aiostreams(manager, url, uuid_password))
    return tasks


def get_all_jackettio_tasks(manager):
    urls = settings.JACKETTIO_URL
    if isinstance(urls, str):
        urls = [urls]

    tasks = []
    for url in urls:
        tasks.append(get_jackettio(manager, url))
    return tasks
