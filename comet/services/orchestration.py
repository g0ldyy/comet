import asyncio

from RTN import DefaultRanking, ParsedData

from comet.core.execution import get_executor
from comet.core.logger import logger
from comet.core.models import CometSettingsModel, database, settings
from comet.scrapers.manager import scraper_manager
from comet.scrapers.models import ScrapeRequest
from comet.services.filtering import filter_worker
from comet.services.ranking import rank_worker
from comet.services.torrent_manager import torrent_update_queue
from comet.utils.media_ids import normalize_cache_media_ids
from comet.utils.languages import select_indexer_titles
from comet.utils.parsing import (
    ensure_multi_language,
    load_cached_parsed,
    load_cached_string_list,
    parsed_matches_target,
)
from comet.utils.torrent_cache import build_torrent_cache_where, normalize_search_params


def _is_optional_int(value: object) -> bool:
    return value is None or type(value) is int


def _is_current_scrape_result(torrent: object) -> bool:
    if not isinstance(torrent, dict):
        return False

    required_keys = {
        "title",
        "infoHash",
        "fileIndex",
        "seeders",
        "size",
        "tracker",
        "sources",
    }
    return (
        required_keys <= torrent.keys()
        and isinstance(torrent["title"], str)
        and bool(torrent["title"])
        and isinstance(torrent["infoHash"], str)
        and bool(torrent["infoHash"])
        and _is_optional_int(torrent["fileIndex"])
        and _is_optional_int(torrent["seeders"])
        and _is_optional_int(torrent["size"])
        and isinstance(torrent["tracker"], str)
        and isinstance(torrent["sources"], list)
        and all(isinstance(source, str) for source in torrent["sources"])
    )


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
        target_air_date: str | None = None,
        reject_unknown_episode_files: bool = False,
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
        self.target_air_date = target_air_date
        self.reject_unknown_episode_files = reject_unknown_episode_files

        self.seen_hashes = set()
        self.torrents = {}
        self.ready_to_cache = []
        self.ranked_torrents = {}
        self.primary_cached = False

    def _matches_requested_scope(
        self,
        parsed: ParsedData,
        *,
        reject_unknown_override: bool | None = None,
    ) -> bool:
        reject_unknown = (
            self.reject_unknown_episode_files
            if reject_unknown_override is None
            else reject_unknown_override
        )
        return parsed_matches_target(
            parsed,
            self.search_season,
            self.search_episode,
            target_air_date=self.target_air_date,
            reject_unknown_episode_files=reject_unknown,
        )

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
            search_titles=select_indexer_titles(
                self.title,
                self.aliases,
                settings.INDEXER_LANGUAGES,
                include_canonical=settings.INDEXER_INCLUDE_CANONICAL_TITLE,
                include_original=settings.INDEXER_INCLUDE_ORIGINAL_TITLE,
            ),
        )
        titles = " · ".join(f"“{title}”" for title in request.query_titles)
        logger.log(
            "SCRAPER",
            f"🔤 Indexer titles ({len(request.query_titles)}): {titles}",
        )

        async for scraper_name, results, response_time in scraper_manager.scrape_all(
            request
        ):
            await self.filter_manager(scraper_name, results, response_time)

        await self.cache_torrents()

        for torrent in self.ready_to_cache:
            if not self._matches_requested_scope(torrent["parsed"]):
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
            "SELECT info_hash, file_index, title, seeders, size, tracker, sources_json, parsed_json, episode, updated_at "
            + where_clause
        )
        return await database.fetch_all(query, params)

    async def get_cached_torrents(self):
        rows = []
        cache_row_groups = await asyncio.gather(
            *(
                self._fetch_cached_rows(cache_media_id)
                for cache_media_id in self.cache_media_ids
            )
        )
        for cache_media_id, cache_rows in zip(self.cache_media_ids, cache_row_groups):
            if cache_rows and cache_media_id == self.media_only_id:
                self.primary_cached = True
            rows.extend(cache_rows)

        if rows:
            best_rows = {}

            def row_priority(row):
                exact_episode_match = (
                    self.search_episode is not None
                    and row["episode"] == self.search_episode
                )
                has_episode_scope = row["episode"] is not None
                has_file_index = row["file_index"] is not None
                has_specific_title = bool(row["title"])
                updated_at = row["updated_at"] or 0
                return (
                    exact_episode_match,
                    has_episode_scope,
                    has_file_index,
                    has_specific_title,
                    updated_at,
                )

            for row in rows:
                info_hash = row["info_hash"]
                current = best_rows.get(info_hash)
                if current is None or row_priority(row) > row_priority(current):
                    best_rows[info_hash] = row

            rows = list(best_rows.values())

        for row in rows:
            parsed_data = load_cached_parsed(row["parsed_json"])
            if parsed_data is None:
                logger.warning(
                    f"Skipping torrent cache row with invalid parsed data: {row['info_hash']}"
                )
                continue
            ensure_multi_language(parsed_data)

            target_season = self.search_season
            if (
                target_season is not None
                and parsed_data.seasons
                and target_season not in parsed_data.seasons
            ):
                continue

            reject_unknown_override = (
                True
                if self.reject_unknown_episode_files and self.search_episode is not None
                else None
            )
            if not self._matches_requested_scope(
                parsed_data, reject_unknown_override=reject_unknown_override
            ):
                continue

            info_hash = row["info_hash"]
            self.torrents[info_hash] = {
                "fileIndex": row["file_index"],
                "title": row["title"],
                "seeders": row["seeders"],
                "size": row["size"],
                "tracker": row["tracker"],
                "sources": load_cached_string_list(row["sources_json"]),
                "parsed": parsed_data,
            }

    def _append_cache_file_infos(self, file_infos: list[dict], torrent: dict):
        parsed = torrent["parsed"]
        cache_seasons = parsed.seasons or [
            self.search_season if self.search_season is not None else self.season
        ]
        parsed_episodes = parsed.episodes or [None]

        if self.reject_unknown_episode_files and self.search_episode is not None:
            if not self._matches_requested_scope(parsed, reject_unknown_override=True):
                return

            cache_seasons = [self.search_season]
            parsed_episodes = [self.search_episode]

        episode = None if len(parsed_episodes) > 1 else parsed_episodes[0]
        info_hash = torrent["infoHash"]
        file_index = torrent["fileIndex"]
        title = torrent["title"]
        size = torrent["size"]
        seeders = torrent["seeders"]
        tracker = torrent["tracker"]
        sources = torrent["sources"]

        for season in cache_seasons:
            file_infos.append(
                {
                    "info_hash": info_hash,
                    "index": file_index,
                    "title": title,
                    "size": size,
                    "season": season,
                    "episode": episode,
                    "parsed": parsed,
                    "seeders": seeders,
                    "tracker": tracker,
                    "sources": sources,
                }
            )

    async def cache_torrents(self):
        file_infos = []
        for torrent in self.ready_to_cache:
            self._append_cache_file_infos(file_infos, torrent)

        if file_infos:
            await torrent_update_queue.add_torrent_infos(file_infos, self.media_only_id)

    async def filter_manager(
        self,
        scraper_name: str,
        torrents: object,
        response_time: float | None = None,
    ):
        timing = f" Took {response_time:.2f}s." if response_time is not None else ""
        if not isinstance(torrents, list):
            logger.warning(
                f"Scraper {scraper_name} returned an invalid result container.{timing}"
            )
            return

        if len(torrents) == 0:
            logger.log("SCRAPER", f"Scraper {scraper_name} found 0 torrents.{timing}")
            return

        valid_torrents = [
            torrent for torrent in torrents if _is_current_scrape_result(torrent)
        ]
        if len(valid_torrents) != len(torrents):
            logger.warning(
                f"Scraper {scraper_name} returned "
                f"{len(torrents) - len(valid_torrents)} invalid torrents."
            )

        new_torrents = [
            torrent
            for torrent in valid_torrents
            if (torrent["infoHash"], torrent["title"]) not in self.seen_hashes
        ]

        self.seen_hashes.update(
            (torrent["infoHash"], torrent["title"]) for torrent in new_torrents
        )

        logger.log(
            "SCRAPER",
            f"Scraper {scraper_name} found {len(torrents)} torrents, "
            f"{len(new_torrents)} new.{timing}",
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
