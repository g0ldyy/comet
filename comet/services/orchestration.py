import asyncio
from concurrent.futures.process import BrokenProcessPool

import orjson
from RTN import DefaultRanking, ParsedData

from comet.core.execution import get_executor
from comet.core.logger import logger
from comet.core.models import CometSettingsModel, database
from comet.scrapers.manager import scraper_manager
from comet.services.filtering import filter_worker
from comet.services.ranking import rank_worker
from comet.services.torrent_manager import torrent_update_queue


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
        is_kitsu: bool = False,
        context: str = "live",
        search_episode: int | None = None,
        search_season: int | None = None,
        title_variants: list[str] | None = None,
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
        self.search_episode = search_episode if search_episode is not None else episode
        self.search_season = search_season if search_season is not None else season
        self.aliases = aliases
        self.remove_adult_content = remove_adult_content
        self.is_kitsu = is_kitsu
        self.context = context
        self.title_variants = title_variants

        self.seen_hashes = set()
        self.torrents = {}
        self.ready_to_cache = []
        self.ranked_torrents = {}

    async def scrape_torrents(
        self,
    ):
        from comet.scrapers.models import ScrapeRequest

        request = ScrapeRequest(
            media_type=self.media_type,
            media_id=self.media_id,
            media_only_id=self.media_only_id,
            title=self.title,
            title_variants=self.title_variants,
            year=self.year,
            year_end=self.year_end,
            season=self.search_season,
            episode=self.search_episode,
            context=self.context,
        )

        if len(request.title_variants) > 1:
            logger.log(
                "SCRAPER",
                f"🔎 Title variants for search: {', '.join(request.title_variants)}",
            )

        async for scraper_name, results in scraper_manager.scrape_all(request):
            await self.filter_manager(scraper_name, results)

        logger.log(
            "SCRAPER",
            f"🧪 Filtered torrents collected: {len(self.ready_to_cache)}",
        )

        asyncio.create_task(self.cache_torrents())

        for torrent in self.ready_to_cache:
            season = torrent["parsed"].seasons[0] if torrent["parsed"].seasons else None
            episode = (
                torrent["parsed"].episodes[0] if torrent["parsed"].episodes else None
            )

            if self.is_kitsu:
                if episode is not None and episode != self.search_episode:
                    continue
            else:
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
        if self.is_kitsu:
            rows = await database.fetch_all(
                """
                    SELECT info_hash, file_index, title, seeders, size, tracker, sources, parsed, episode
                    FROM torrents
                    WHERE media_id = :media_id
                    AND (episode IS NULL OR episode = CAST(:episode as INTEGER))
                """,
                {
                    "media_id": self.media_only_id,
                    "episode": self.search_episode,
                },
            )
        else:
            rows = await database.fetch_all(
                """
                    SELECT info_hash, file_index, title, seeders, size, tracker, sources, parsed, episode
                    FROM torrents
                    WHERE media_id = :media_id
                    AND ((season IS NOT NULL AND season = CAST(:season as INTEGER)) OR (season IS NULL AND CAST(:season as INTEGER) IS NULL))
                    AND (episode IS NULL OR episode = CAST(:episode as INTEGER))
                """,
                {
                    "media_id": self.media_only_id,
                    "season": self.season,
                    "episode": self.episode,
                },
            )

        rows = sorted(rows, key=lambda r: (r["episode"] is not None, r["episode"]))

        for row in rows:
            parsed_data = ParsedData(**orjson.loads(row["parsed"]))

            if row["episode"] is None and parsed_data.episodes:
                target_episode = self.search_episode if self.is_kitsu else self.episode
                if target_episode not in parsed_data.episodes:
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
            if self.is_kitsu:
                cache_seasons = [self.season]
            else:
                cache_seasons = (
                    torrent["parsed"].seasons
                    if torrent["parsed"].seasons
                    else [self.season]
                )

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
        try:
            tasks = [
                loop.run_in_executor(
                    get_executor(),
                    filter_worker,
                    new_torrents[i : i + chunk_size],
                    self.title,
                    self.year,
                    self.year_end,
                    self.aliases,
                    self.remove_adult_content,
                )
                for i in range(0, len(new_torrents), chunk_size)
            ]
            results = await asyncio.gather(*tasks)
            for result in results:
                self.ready_to_cache.extend(result)
        except BrokenProcessPool:
            logger.error(
                "RTN filter worker process pool crashed during filtering; skipping RTN filter for this request."
            )
        except Exception as e:
            logger.error(f"Unexpected error during RTN filtering: {e}")

    async def rank_torrents(
        self,
        rtn_settings: CometSettingsModel,
        rtn_ranking: DefaultRanking,
        max_results_per_resolution: int,
        max_size: int,
        cached_only: int,
        remove_trash: int,
    ):
        loop = asyncio.get_running_loop()
        try:
            self.ranked_torrents = await loop.run_in_executor(
                get_executor(),
                rank_worker,
                self.torrents,
                self.debrid_service,
                rtn_settings,
                rtn_ranking,
                max_results_per_resolution,
                max_size,
                cached_only,
                remove_trash,
            )
        except BrokenProcessPool:
            logger.error(
                "RTN ranking worker process pool crashed during ranking; returning unranked torrents."
            )
            # Fallback: leave torrents in current order
            self.ranked_torrents = self.torrents
        except Exception as e:
            logger.error(f"Unexpected error during RTN ranking: {e}")
