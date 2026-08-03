import asyncio
import time

import orjson
from RTN import DefaultRanking, ParsedData

from comet.core.capabilities import (
    CapabilityPlan,
    EligibleDiscovery,
    EligibleProvider,
)
from comet.core.execution import get_executor
from comet.core.models import CometSettingsModel, database, settings
from comet.core.scrape import ScrapeContext
from comet.core.sources import (
    ReleaseCandidate,
    TransportKind,
)
from comet.discovery.manager import SearchCoordinator
from comet.discovery.models import MediaQuery
from comet.discovery.torrent_models import ScrapeRequest
from comet.discovery.torrent_registry import (
    SERVER_TORRENT_ACCOUNT_PARTITION,
    torrent_adapter_registry,
)
from comet.discovery.torrent_repository import (
    TorrentReleaseRepository,
)
from comet.observability import current_request_id
from comet.observability.logging import log
from comet.services.filtering import (
    filter_release_records,
    normalize_release_candidates,
    release_normalization_fingerprint,
)
from comet.services.ranking import rank_release_records
from comet.services.torrent_manager import torrent_update_queue
from comet.utils.languages import select_indexer_titles
from comet.utils.media_ids import normalize_cache_media_ids
from comet.utils.parsing import (
    MediaScope,
    ensure_multi_language,
    load_cached_parsed,
    resolve_media_scope,
)


class TorrentResultAccumulator:
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
        search_episode: int | None = None,
        search_season: int | None = None,
        cache_media_ids: list[str] | None = None,
        target_air_date: str | None = None,
        reject_unknown_episode_files: bool = False,
        media_scope: MediaScope | None = None,
        cache_task_adder=None,
    ):
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
        self.media_scope = (
            resolve_media_scope(media_type, season, episode)
            if media_scope is None
            else media_scope
        )
        self.aliases = aliases
        self.remove_adult_content = remove_adult_content
        self.is_kitsu = is_kitsu
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
        self.live_result_timestamp = time.time()
        self.cache_task_adder = cache_task_adder

    def _matches_requested_scope(
        self,
        parsed: ParsedData,
        *,
        reject_unknown_override: bool | None = None,
        scope_is_known: bool = False,
    ) -> bool:
        reject_unknown = (
            self.reject_unknown_episode_files
            if reject_unknown_override is None
            else reject_unknown_override
        )
        return self.media_scope.matches_parsed(
            parsed,
            self.search_season,
            self.search_episode,
            target_air_date=self.target_air_date,
            reject_unknown_episode_files=reject_unknown,
            scope_is_known=scope_is_known,
        )

    async def scrape_torrents(
        self,
        context: ScrapeContext,
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
            context=context,
            search_titles=select_indexer_titles(
                self.title,
                self.aliases,
                settings.INDEXER_LANGUAGES,
                include_canonical=settings.INDEXER_INCLUDE_CANONICAL_TITLE,
                include_original=settings.INDEXER_INCLUDE_ORIGINAL_TITLE,
            ),
        )
        adapters = torrent_adapter_registry.build_adapters(request)
        source_ids = tuple(adapters)
        discovery = tuple(
            EligibleDiscovery(
                configuration_id,
                frozenset({TransportKind.BITTORRENT}),
            )
            for configuration_id in source_ids
        )
        plan = CapabilityPlan(
            frozenset({TransportKind.BITTORRENT}),
            source_ids,
            (EligibleProvider("direct_torrent", "direct_torrent", 0),),
            (),
            discovery,
        )
        hard_timeout = (
            8.0
            if context is ScrapeContext.LIVE
            else max(
                (adapter.discovery_timeout for adapter in adapters.values()),
                default=8.0,
            )
        )
        branch_fingerprints = torrent_adapter_registry.branch_fingerprints(
            adapters,
            context,
        )
        discovery_result = await SearchCoordinator(
            adapters,
            hard_timeout=hard_timeout,
            database=database,
            candidate_normalizer=self._normalize_candidates,
        ).search(
            MediaQuery(
                media_id=self.media_only_id,
                media_type=self.media_type,
                season=self.search_season,
                episode=self.search_episode,
                title_aliases=request.query_titles,
                year=self.year,
                request_media_id=self.media_id,
                title=self.title,
                year_end=self.year_end,
                search_titles=request.query_titles,
                normalization_fingerprint=release_normalization_fingerprint(
                    title=self.title,
                    year=self.year,
                    year_end=self.year_end,
                    media_type=self.media_type,
                    aliases=self.aliases,
                ),
            ),
            plan,
            account_partition=SERVER_TORRENT_ACCOUNT_PARTITION,
            trace_id=current_request_id(),
            work_class=context,
            branch_fingerprints=branch_fingerprints,
        )
        await self.filter_manager(
            [
                self._candidate_scrape_result(candidate)
                for candidate in discovery_result.candidates
            ],
        )

        await self.cache_torrents(defer=context is ScrapeContext.LIVE)

        self._publish_ready_torrents(self.ready_to_cache)
        return discovery_result

    async def _normalize_candidates(
        self,
        candidates: tuple[ReleaseCandidate, ...],
    ) -> tuple[ReleaseCandidate, ...]:
        return await normalize_release_candidates(
            candidates,
            title=self.title,
            year=self.year,
            year_end=self.year_end,
            media_type=self.media_type,
            aliases=self.aliases,
            content_id=self.media_id,
        )

    def _publish_ready_torrents(self, torrents: list[dict]) -> None:
        """Expose already-filtered releases through the legacy torrent view."""
        for torrent in torrents:
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
                "updatedAt": self.live_result_timestamp,
            }

    async def ingest_release_candidates(
        self, source_id: str, candidates: tuple[ReleaseCandidate, ...]
    ) -> None:
        """Send discovered BitTorrent candidates through the existing pipeline."""
        scrape_results = [
            self._candidate_scrape_result(candidate, source_id)
            for candidate in candidates
            if candidate.transport is TransportKind.BITTORRENT
        ]
        if not scrape_results:
            return

        ready_count = len(self.ready_to_cache)
        await self.filter_manager(scrape_results)
        new_ready = self.ready_to_cache[ready_count:]
        if new_ready:
            await self.cache_torrents(new_ready)
        self._publish_ready_torrents(new_ready)

    @staticmethod
    def _candidate_scrape_result(
        candidate: ReleaseCandidate,
        source_id: str = "Discovery",
    ) -> dict:
        if candidate.transport is not TransportKind.BITTORRENT:
            raise ValueError("torrent pipeline received a non-torrent candidate")
        locator = candidate.locators[0]
        seeders = candidate.transport_stats.get("seeders")
        tracker_sources = candidate.transport_stats.get("tracker_sources", ())
        return {
            "title": candidate.title,
            "infoHash": locator.info_hash,
            "fileIndex": locator.file_index,
            "seeders": seeders,
            "size": candidate.size,
            "tracker": candidate.source or source_id,
            "sources": list(tracker_sources),
            "parsed": candidate.parsed,
        }

    async def _fetch_cached_rows(self, media_id: str):
        return await TorrentReleaseRepository(database).load_cache_rows(
            media_id,
            self.media_scope,
            self.search_season,
            self.search_episode,
        )

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

            def row_priority(item):
                row, _parsed = item
                preferred_scope = (
                    row["episode"] is None
                    if self.media_scope.is_aggregate
                    else (
                        self.search_episode is not None
                        and row["episode"] == self.search_episode
                    )
                )
                has_file_index = row["file_index"] is not None
                has_specific_title = bool(row["title"])
                updated_at = row["updated_at"]
                return (
                    preferred_scope,
                    has_file_index,
                    has_specific_title,
                    updated_at,
                )

            for row in rows:
                parsed_data = load_cached_parsed(row["parsed_json"])
                if parsed_data is None:
                    continue
                info_hash = row["info_hash"]
                current = best_rows.get(info_hash)
                item = (row, parsed_data)
                if current is None or row_priority(item) > row_priority(current):
                    best_rows[info_hash] = item

            rows = list(best_rows.values())

        for row, parsed_data in rows:
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
                parsed_data,
                reject_unknown_override=reject_unknown_override,
                scope_is_known=True,
            ):
                continue

            info_hash = row["info_hash"]
            try:
                sources = orjson.loads(row["sources_json"])
            except (orjson.JSONDecodeError, TypeError):
                sources = []
            if not isinstance(sources, list):
                sources = []
            self.torrents[info_hash] = {
                "fileIndex": row["file_index"],
                "title": row["title"],
                "seeders": row["seeders"],
                "size": row["size"],
                "tracker": row["tracker"],
                "sources": [source for source in sources if isinstance(source, str)],
                "parsed": parsed_data,
                "updatedAt": row["updated_at"],
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

    async def cache_torrents(
        self,
        torrents: list[dict] | None = None,
        *,
        defer: bool = True,
    ):
        file_infos = []
        for torrent in self.ready_to_cache if torrents is None else torrents:
            self._append_cache_file_infos(file_infos, torrent)

        if file_infos:
            if defer and self.cache_task_adder is not None:
                self.cache_task_adder(
                    torrent_update_queue.add_torrent_infos,
                    file_infos,
                    self.media_only_id,
                )
            else:
                await torrent_update_queue.add_torrent_infos(
                    file_infos,
                    self.media_only_id,
                )

    async def filter_manager(
        self,
        torrents: list[dict],
    ):
        if len(torrents) == 0:
            return

        new_torrents = []
        for torrent in torrents:
            if not isinstance(torrent, dict):
                continue
            info_hash = torrent.get("infoHash")
            title = torrent.get("title")
            if not isinstance(info_hash, str) or not isinstance(title, str):
                continue
            identity = (info_hash, title)
            if identity in self.seen_hashes:
                continue
            self.seen_hashes.add(identity)
            new_torrents.append(torrent)

        if not new_torrents:
            return

        unparsed_torrents = []
        for torrent in new_torrents:
            parsed = torrent.get("parsed")
            if not isinstance(parsed, ParsedData):
                unparsed_torrents.append(torrent)
                continue
            parsed = parsed.model_copy(deep=True)
            if self.remove_adult_content and parsed.adult:
                continue
            self.ready_to_cache.append({**torrent, "parsed": parsed})

        if not unparsed_torrents:
            return

        loop = asyncio.get_running_loop()
        chunk_size = 20
        tasks = [
            loop.run_in_executor(
                get_executor(),
                filter_release_records,
                unparsed_torrents[i : i + chunk_size],
                self.title,
                self.year,
                self.year_end,
                self.media_type,
                self.aliases,
                self.remove_adult_content,
                self.media_id,
            )
            for i in range(0, len(unparsed_torrents), chunk_size)
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
        started_at = time.monotonic_ns()
        candidate_count = len(self.torrents)
        loop = asyncio.get_running_loop()
        ranked_torrents = await loop.run_in_executor(
            get_executor(),
            rank_release_records,
            self.torrents,
            rtn_settings,
            rtn_ranking,
            max_results_per_resolution,
            max_size,
            remove_trash,
        )
        if self.media_scope.is_aggregate:
            ranked_torrents = sorted(
                ranked_torrents,
                key=lambda info_hash: self.media_scope.granularity_priority(
                    self.torrents[info_hash]["parsed"]
                ),
                reverse=True,
            )
        self.ranked_torrents = ranked_torrents
        log.info(
            "filter.ranking.completed",
            "Release ranking completed",
            content_id=self.media_id,
            candidate_count=candidate_count,
            result_count=len(ranked_torrents),
            duration_ms=(time.monotonic_ns() - started_at) / 1_000_000,
        )
