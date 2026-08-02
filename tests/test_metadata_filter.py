import unittest
from unittest.mock import AsyncMock, patch

from comet.metadata.filter import DigitalReleaseFilter


class DigitalReleaseFilterTests(unittest.IsolatedAsyncioTestCase):
    async def test_indeterminate_watch_provider_state_fails_open_without_caching(self):
        release_filter = DigitalReleaseFilter()
        with (
            patch(
                "comet.metadata.filter.database.fetch_val",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "comet.metadata.filter.TMDBApi.get_tmdb_id_from_imdb",
                new=AsyncMock(return_value="123"),
            ),
            patch(
                "comet.metadata.filter.TMDBApi.get_upcoming_movie_release_date",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "comet.metadata.filter.TMDBApi.has_watch_providers",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "comet.metadata.filter.database.execute",
                new=AsyncMock(),
            ) as execute,
        ):
            released = await release_filter.check_is_released(
                None,
                "movie",
                "tt1234567",
            )

        self.assertTrue(released)
        execute.assert_not_awaited()

    async def test_invalid_cached_timestamp_is_refreshed(self):
        release_filter = DigitalReleaseFilter()
        with (
            patch(
                "comet.metadata.filter.database.fetch_val",
                new=AsyncMock(return_value=float("inf")),
            ),
            patch(
                "comet.metadata.filter.TMDBApi.get_tmdb_id_from_imdb",
                new=AsyncMock(return_value="123"),
            ),
            patch(
                "comet.metadata.filter.TMDBApi.get_upcoming_movie_release_date",
                new=AsyncMock(return_value="2020-01-01"),
            ),
            patch(
                "comet.metadata.filter.database.execute",
                new=AsyncMock(),
            ) as execute,
        ):
            released = await release_filter.check_is_released(
                None,
                "movie",
                "tt1234567",
            )

        self.assertTrue(released)
        execute.assert_awaited_once()

    async def test_unexpected_database_failure_is_not_masked(self):
        release_filter = DigitalReleaseFilter()
        with patch(
            "comet.metadata.filter.database.fetch_val",
            new=AsyncMock(side_effect=RuntimeError("database failure")),
        ):
            with self.assertRaisesRegex(RuntimeError, "database failure"):
                await release_filter.check_is_released(
                    None,
                    "movie",
                    "tt1234567",
                )

    def test_release_timestamps_have_a_closed_domain(self):
        release_filter = DigitalReleaseFilter()
        for value in (None, True, "1", -1, float("inf"), 253402300800):
            with self.subTest(value=value):
                self.assertIsNone(release_filter._release_timestamp(value))
        self.assertEqual(release_filter._release_timestamp(0), 0)
