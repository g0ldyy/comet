import asyncio

import orjson
from RTN import DefaultRanking, ParsedData

from comet.core.execution import get_executor
from comet.core.logger import logger
from comet.core.models import CometSettingsModel, database
from comet.scrapers.manager import scraper_manager
from comet.scrapers.models import ScrapeRequest
from comet.services.filtering import filter_worker
from comet.services.ranking import rank_worker
from comet.services.torrent_manager import torrent_update_queue
from comet.utils.media_ids import normalize_cache_media_ids
from comet.utils.parsing import ensure_multi_language
from comet.utils.torrent_cache import (build_torrent_cache_where,
                                       normalize_search_params)


class TorrentManager:
    def __init__(
        self,
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
        is_kitsu: bool = False,
        context: str = "live",
        search_episode: int | None = None,
        search_season: int | None = None,
        cache_media_ids: list[str] | None = None,
    ):
        self.media_type = media_type
        self.media_id = media_full_id
        self.media_only_id = media_only_id
        self.title = title
        self.year = year
        self.year_end = year_end
        self.season = season
        self.episode = episode
        search = normalize_search_params(season, episode, search_season, search_episode)
        self.search_episode = search.episode
        self.search_season = search.season
        self.aliases = aliases
        self.remove_adult_content = remove_adult_content
        self.is_kitsu = is_kitsu
        self.context = context

        self.cache_media_ids = normalize_cache_media_ids(
            self.media_only_id, cache_media_ids
        )

        self.seen_hashes = set()
        self.torrents = {}
        self.ready_to_cache = []
        self.ranked_torrents = {}
        self.primary_cached = False

    async def scrape_torrents(
        self,
    ):
        request = ScrapeRequest(
            media_type=self.media_type,
            media_id=self.media_id,
            media_only_id=self.media_only_id,
            title=self.title,
            year=self.year,
            year_end=self.year_end,
            season=self.search_season,
            episode=self.search_episode,
            context=self.context,
        )

        async for scraper_name, results in scraper_manager.scrape_all(request):
            await self.filter_manager(scraper_name, results)

        asyncio.create_task(self.cache_torrents())

        for torrent in self.ready_to_cache:
            season = torrent["parsed"].seasons[0] if torrent["parsed"].seasons else None
            episode = (
                torrent["parsed"].episodes[0] if torrent["parsed"].episodes else None
            )

            if (season is not None and season != self.search_season) or (
                episode is not None and episode != self.search_episode
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

    async def _fetch_cached_rows(self, media_id: str):
        where_clause, params = build_torrent_cache_where(
            media_id, self.search_season, self.search_episode
        )
        query = (
            "SELECT info_hash, file_index, title, seeders, size, tracker, sources, parsed, episode "
            + where_clause
        )
        return await database.fetch_all(query, params)

    async def get_cached_torrents(self):
        rows = []
        for cache_media_id in self.cache_media_ids:
            cache_rows = await self._fetch_cached_rows(cache_media_id)
            if cache_rows and cache_media_id == self.media_only_id:
                self.primary_cached = True
            rows.extend(cache_rows)

        rows = sorted(rows, key=lambda r: (r["episode"] is not None, r["episode"]))

        for row in rows:
            parsed_data = ParsedData(**orjson.loads(row["parsed"]))
            ensure_multi_language(parsed_data)

            target_season = self.search_season
            if (
                target_season is not None
                and parsed_data.seasons
                and target_season not in parsed_data.seasons
            ):
                continue

            if row["episode"] is None and parsed_data.episodes:
                if self.search_episode not in parsed_data.episodes:
                    continue

            info_hash = row["info_hash"]
            self.torrents[info_hash] = {
                "fileIndex": row["file_index"],
                "title": row["title"],
                "seeders": row["seeders"],
                "size": row["size"],
                "tracker": row["tracker"],
                "sources": orjson.loads(row["sources"]),
                "parsed": parsed_data,
            }

    async def cache_torrents(self):
        for torrent in self.ready_to_cache:
            parsed_seasons = torrent["parsed"].seasons
            if parsed_seasons:
                cache_seasons = parsed_seasons
            else:
                cache_season = (
                    self.search_season
                    if self.search_season is not None
                    else self.season
                )
                cache_seasons = [cache_season]

            parsed_episodes = (
                torrent["parsed"].episodes if torrent["parsed"].episodes else [None]
            )

            if len(parsed_episodes) > 1:
                episode = None
            else:
                episode = parsed_episodes[0]

            for season in cache_seasons:
                file_info = {
                    "info_hash": torrent["infoHash"],
                    "index": torrent["fileIndex"],
                    "title": torrent["title"],
                    "size": torrent["size"],
                    "season": season,
                    "episode": episode,
                    "parsed": torrent["parsed"],
                    "seeders": torrent["seeders"],
                    "tracker": torrent["tracker"],
                    "sources": torrent["sources"],
                }
                await torrent_update_queue.add_torrent_info(
                    file_info, self.media_only_id
                )

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

        if not new_torrents:
            return

        loop = asyncio.get_running_loop()
        chunk_size = 20
        tasks = [
            loop.run_in_executor(
                get_executor(),
                filter_worker,
                new_torrents[i : i + chunk_size],
                self.title,
                self.year,
                self.year_end,
                self.media_type,
                self.aliases,
                self.remove_adult_content,
            )
            for i in range(0, len(new_torrents), chunk_size)
        ]
        results = await asyncio.gather(*tasks)
        for result in results:
            self.ready_to_cache.extend(result)

    async def rank_torrents(
        self,
        rtn_settings: CometSettingsModel,
        rtn_ranking: DefaultRanking,
        max_results_per_resolution: int,
        max_size: int,
        remove_trash: int,
    ):
        loop = asyncio.get_running_loop()
        self.ranked_torrents = await loop.run_in_executor(
            get_executor(),
            rank_worker,
            self.torrents,
            rtn_settings,
            rtn_ranking,
            max_results_per_resolution,
            max_size,
            remove_trash,
        )
