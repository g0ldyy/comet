import asyncio
import time
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from databases import Database

from comet.core.db_router import ReplicaAwareDatabase
from comet.core.models import settings
from comet.metadata.manager import (
    MetadataScraper,
    _alias_cache_timestamp,
    _CacheEntry,
)
from comet.services.anime import anime_mapper


class _SharedMetadataState:
    def __init__(self):
        self.cached = _CacheEntry(metadata=None, aliases=None)
        self.initial_checks = 0
        self.both_checked = asyncio.Event()
        self.metadata_calls = 0
        self.alias_calls = 0
        self.cache_calls = 0


class _MetadataScraper(MetadataScraper):
    def __init__(self, state):
        super().__init__(session=None)
        self.state = state
        self.first_check = True

    async def get_cached(self, media_id, season, episode):
        if self.first_check:
            self.first_check = False
            self.state.initial_checks += 1
            if self.state.initial_checks == 2:
                self.state.both_checked.set()

        metadata = self.state.cached.metadata
        if metadata is not None:
            metadata = {**metadata, "season": season, "episode": episode}
        return _CacheEntry(metadata=metadata, aliases=self.state.cached.aliases)

    async def get_metadata(self, id, season, episode, is_kitsu, media_type):
        await self.state.both_checked.wait()
        self.state.metadata_calls += 1
        return {
            "title": "Shared Movie",
            "year": 2026,
            "year_end": None,
            "season": season,
            "episode": episode,
        }

    async def get_aliases(self, media_type, media_id, provider=None):
        self.state.alias_calls += 1
        return {"us": ["Shared Movie"]}

    async def cache_metadata(
        self,
        media_id,
        metadata,
        aliases,
        *,
        update_metadata,
        update_aliases,
        season,
        episode,
    ):
        self.state.cache_calls += 1
        current = self.state.cached
        canonical_metadata = current.metadata
        if update_metadata:
            canonical_metadata = {
                key: metadata[key] for key in ("title", "year", "year_end")
            }
        effective_aliases = current.aliases
        if update_aliases:
            effective_aliases = aliases if aliases is not None else {}
        self.state.cached = _CacheEntry(canonical_metadata, effective_aliases)
        return _CacheEntry(
            metadata=(
                {**canonical_metadata, "season": season, "episode": episode}
                if canonical_metadata is not None
                else None
            ),
            aliases=effective_aliases,
        )


class MetadataRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_imdb_anime_aliases_are_merged_with_tmdb_languages(self):
        scraper = MetadataScraper(session=None)
        with (
            patch.object(anime_mapper, "is_loaded", return_value=True),
            patch.object(anime_mapper, "is_anime_content", return_value=True),
            patch.object(
                anime_mapper,
                "get_aliases",
                new=AsyncMock(return_value={"original": ["Romaji"], "ez": ["Synonym"]}),
            ),
            patch(
                "comet.metadata.manager.TMDBApi.get_title_aliases",
                new=AsyncMock(return_value={"lang:fr": ["Titre français"]}),
            ),
        ):
            aliases = await scraper.get_aliases("movie", "tt123", "imdb")

        self.assertEqual(
            aliases,
            {
                "original": ["Romaji"],
                "ez": ["Synonym"],
                "lang:fr": ["Titre français"],
            },
        )

    async def test_kitsu_aliases_use_existing_imdb_mapping_for_tmdb_languages(self):
        scraper = MetadataScraper(session=None)
        with (
            patch.object(anime_mapper, "is_loaded", return_value=True),
            patch.object(anime_mapper, "is_anime_content", return_value=True),
            patch.object(
                anime_mapper,
                "get_aliases",
                new=AsyncMock(return_value={"original": ["Romaji"]}),
            ),
            patch.object(
                anime_mapper,
                "get_imdb_from_kitsu",
                new=AsyncMock(return_value="tt123"),
            ) as get_imdb,
            patch(
                "comet.metadata.manager.TMDBApi.get_title_aliases",
                new=AsyncMock(return_value={"lang:fr": ["Titre français"]}),
            ) as get_tmdb,
        ):
            aliases = await scraper.get_aliases("series", "456", "kitsu")

        get_imdb.assert_awaited_once_with("456")
        get_tmdb.assert_awaited_once_with("series", "tt123")
        self.assertEqual(
            aliases,
            {"original": ["Romaji"], "lang:fr": ["Titre français"]},
        )

    def test_alias_failure_timestamp_expires_after_short_retry_delay(self):
        current_time = 10_000_000.0

        failed_at = _alias_cache_timestamp(current_time, succeeded=False)

        self.assertEqual(
            failed_at - (current_time - settings.METADATA_CACHE_TTL),
            min(300, settings.METADATA_CACHE_TTL),
        )
        self.assertEqual(
            _alias_cache_timestamp(current_time, succeeded=True),
            current_time,
        )

    async def test_alias_failure_preserves_stale_data_with_short_backoff(self):
        with TemporaryDirectory() as temp_dir:
            database = ReplicaAwareDatabase(
                Database(f"sqlite+aiosqlite:///{temp_dir}/cache.db")
            )
            await database.connect()
            try:
                await database.execute(
                    """
                    CREATE TABLE media_metadata_cache (
                        media_id TEXT PRIMARY KEY,
                        title TEXT,
                        year INTEGER,
                        year_end INTEGER,
                        aliases_json TEXT,
                        metadata_updated_at REAL,
                        aliases_updated_at REAL
                    )
                    """
                )
                scraper = MetadataScraper(session=None)
                with patch("comet.metadata.manager.database", database):
                    await scraper.cache_metadata(
                        "imdb:tt123",
                        {
                            "title": "Movie",
                            "year": 2026,
                            "year_end": None,
                        },
                        {"fr": ["Film"]},
                        update_metadata=True,
                        update_aliases=True,
                        season=None,
                        episode=None,
                    )
                    failed_at = time.time()
                    cached = await scraper.cache_metadata(
                        "imdb:tt123",
                        None,
                        None,
                        update_metadata=False,
                        update_aliases=True,
                        season=None,
                        episode=None,
                    )
                    row = await database.fetch_one(
                        """
                        SELECT aliases_json, aliases_updated_at
                        FROM media_metadata_cache
                        WHERE media_id = 'imdb:tt123'
                        """
                    )

                self.assertEqual(cached.aliases, {"fr": ["Film"]})
                self.assertEqual(row["aliases_json"], '{"fr":["Film"]}')
                retry_at = row["aliases_updated_at"] + settings.METADATA_CACHE_TTL
                self.assertGreaterEqual(retry_at, failed_at + 299)
                self.assertLessEqual(retry_at, time.time() + 301)
            finally:
                await database.disconnect()

    async def test_concurrent_cache_misses_share_one_refresh(self):
        state = _SharedMetadataState()
        first = _MetadataScraper(state)
        second = _MetadataScraper(state)

        first_result, second_result = await asyncio.gather(
            first.fetch_metadata_and_aliases(
                "series", "tt123:1:1", "tt123", season=1, episode=1
            ),
            second.fetch_metadata_and_aliases(
                "series", "tt123:1:2", "tt123", season=1, episode=2
            ),
        )

        self.assertEqual(state.metadata_calls, 1)
        self.assertEqual(state.alias_calls, 1)
        self.assertEqual(state.cache_calls, 1)
        self.assertEqual(first_result[0]["episode"], 1)
        self.assertEqual(second_result[0]["episode"], 2)

    async def test_missing_aliases_do_not_refresh_fresh_metadata(self):
        state = _SharedMetadataState()
        state.both_checked.set()
        state.cached = _CacheEntry(
            metadata={"title": "Cached", "year": 2025, "year_end": None},
            aliases=None,
        )
        scraper = _MetadataScraper(state)

        metadata, aliases = await scraper.fetch_metadata_and_aliases(
            "movie", "tt123", "tt123"
        )

        self.assertEqual(metadata["title"], "Cached")
        self.assertEqual(aliases, {"us": ["Shared Movie"]})
        self.assertEqual(state.metadata_calls, 0)
        self.assertEqual(state.alias_calls, 1)
        self.assertEqual(state.cache_calls, 1)
