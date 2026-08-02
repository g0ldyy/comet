import unittest
from unittest.mock import AsyncMock

from comet.core.sources import ReleaseScope
from comet.discovery.coverage import (
    SearchCoverageRepository,
    query_fingerprint,
    search_scope,
)
from comet.discovery.models import MediaQuery


class SearchCoverageRepositoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.query = MediaQuery(
            "tt1234567",
            "series",
            season=2,
            episode=3,
            title_aliases=("Example", "Exemple"),
            year=2026,
            air_date="2026-07-27",
            absolute_episode=15,
            requested_language="fr",
            search_scope="episode",
        )
        self.branch = "a" * 64

    async def test_effective_coverage_distinguishes_fresh_stale_and_failed(self):
        database = type("Database", (), {})()
        database.fetch_one = AsyncMock(
            side_effect=[
                {
                    "freshness_state": "fresh",
                    "next_refresh_at": 200,
                },
                {
                    "freshness_state": "fresh",
                    "next_refresh_at": 200,
                },
                {
                    "freshness_state": "failed_with_results",
                    "next_refresh_at": 300,
                },
                {
                    "freshness_state": "failed",
                    "next_refresh_at": 300,
                },
                {
                    "freshness_state": "failed",
                    "next_refresh_at": 300,
                },
            ]
        )
        repository = SearchCoverageRepository(database)

        fresh = await repository.effective(self.query, self.branch, now=150)
        stale = await repository.effective(self.query, self.branch, now=200)
        stale_wait = await repository.effective(self.query, self.branch, now=250)
        failed_wait = await repository.effective(self.query, self.branch, now=250)
        failed = await repository.effective(self.query, self.branch, now=300)

        self.assertEqual(fresh.state, "fresh")
        self.assertEqual(stale.state, "stale")
        self.assertEqual(stale_wait.state, "stale_wait")
        self.assertEqual(failed_wait.state, "failed_wait")
        self.assertEqual(failed.state, "failed")

    async def test_success_atomically_replaces_failure_metadata(self):
        database = type("Database", (), {"execute": AsyncMock()})()

        await SearchCoverageRepository(database).record_success(
            self.query,
            self.branch,
            now=100,
            next_refresh_at=200,
        )

        sql, values = database.execute.await_args.args
        self.assertIn("freshness_state = 'fresh'", sql)
        self.assertNotIn("last_attempt_at", sql)
        self.assertNotIn("failure_code", sql)
        self.assertNotIn("title_aliases", values)

    async def test_failure_preserves_prior_success_for_stale_reuse(self):
        database = type("Database", (), {"execute": AsyncMock()})()

        await SearchCoverageRepository(database).record_failure(
            self.query,
            self.branch,
            now=100,
            next_refresh_at=130,
        )

        sql, _values = database.execute.await_args.args
        assignments = sql.split("DO UPDATE SET", 1)[1]
        self.assertIn("failed_with_results", assignments)
        self.assertNotIn("last_scraped_at", sql)
        self.assertNotIn("last_attempt_at", sql)
        self.assertNotIn("failure_code", sql)

    def test_query_fingerprint_covers_every_result_dimension(self):
        baseline = query_fingerprint(self.query)
        variants = [
            MediaQuery(**{**self.query.__dict__, "episode": 4}),
            MediaQuery(**{**self.query.__dict__, "title_aliases": ("Other",)}),
            MediaQuery(**{**self.query.__dict__, "year": 2025}),
            MediaQuery(**{**self.query.__dict__, "air_date": "2026-07-28"}),
            MediaQuery(**{**self.query.__dict__, "absolute_episode": 16}),
            MediaQuery(**{**self.query.__dict__, "requested_language": "en"}),
            MediaQuery(**{**self.query.__dict__, "search_scope": "season_pack"}),
        ]

        self.assertEqual(
            len({baseline, *(query_fingerprint(item) for item in variants)}), 8
        )

    def test_query_resolves_every_closed_release_scope(self):
        queries = (
            (MediaQuery("tt1", "movie"), ReleaseScope.MOVIE),
            (
                MediaQuery("tt1", "series", season=1, episode=2),
                ReleaseScope.EPISODE,
            ),
            (
                MediaQuery("tt1", "series", season=1),
                ReleaseScope.SEASON_PACK,
            ),
            (MediaQuery("tt1", "series"), ReleaseScope.SERIES_PACK),
            (
                MediaQuery("tt1", "series", air_date="2026-07-28"),
                ReleaseScope.DAILY_EPISODE,
            ),
            (
                MediaQuery(
                    "kitsu:1",
                    "series",
                    season=1,
                    episode=2,
                    search_scope="anime_episode",
                ),
                ReleaseScope.ANIME_EPISODE,
            ),
        )

        for query, expected in queries:
            with self.subTest(expected=expected):
                self.assertIs(query.scope, expected)
                self.assertIs(search_scope(query), expected)

    def test_invalid_query_and_unbounded_refresh_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "scope"):
            search_scope(MediaQuery("tt1", "movie", search_scope="unknown"))
        query_fingerprint(
            MediaQuery(
                "tt1",
                "movie",
                title_aliases=("x" * 300,),
            )
        )
        with self.assertRaisesRegex(ValueError, "aliases"):
            query_fingerprint(
                MediaQuery(
                    "tt1",
                    "movie",
                    title_aliases=("x" * 1025,),
                )
            )
