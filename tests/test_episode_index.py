import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

from comet.metadata.episode_index import EpisodeIndexService, database
from comet.metadata.http import MetadataHttpResponse


class EpisodeIndexRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_unexpected_tmdb_failure_surfaces(self):
        service = EpisodeIndexService(session=None)
        with patch(
            "comet.metadata.episode_index.TMDBApi.get_tmdb_id_from_imdb",
            new=AsyncMock(side_effect=RuntimeError("broken client")),
        ):
            with self.assertRaisesRegex(RuntimeError, "broken client"):
                await service._refresh_single_episode_from_tmdb("tt1234567", 1, 2)

    async def test_air_date_reverse_lookup_refreshes_the_existing_index_once(self):
        service = EpisodeIndexService(session=None)
        with (
            patch.object(
                service,
                "_get_cached_episode",
                new=AsyncMock(side_effect=[None, (3, 9)]),
            ) as cached_lookup,
            patch.object(
                service,
                "_is_series_index_fresh",
                new=AsyncMock(return_value=False),
            ),
            patch.object(
                service,
                "_refresh_from_cinemeta",
                new=AsyncMock(),
            ) as refresh,
        ):
            episode = await service.get_episode_by_air_date("tt1234567", "2026-07-25")

        self.assertEqual(episode, (3, 9))
        self.assertEqual(cached_lookup.await_count, 2)
        refresh.assert_awaited_once_with("tt1234567")

    async def test_rows_and_refresh_marker_share_one_transaction(self):
        service = EpisodeIndexService(session=None)
        events = []

        @asynccontextmanager
        async def transaction():
            events.append("begin")
            try:
                yield
            except Exception:
                events.append("rollback")
                raise

        async def upsert_rows(rows):
            events.append(("rows", rows))

        async def delete_rows(series_id):
            events.append(("delete", series_id))

        async def fail_marker(series_id, refreshed_at):
            events.append(("marker", series_id, refreshed_at))
            raise RuntimeError("marker failed")

        rows = [{"season": 1, "episode": 1}]
        with (
            patch.object(database, "transaction", new=transaction),
            patch.object(service, "_delete_series_air_dates", new=delete_rows),
            patch.object(service, "_upsert_series_air_dates", new=upsert_rows),
            patch.object(service, "_upsert_series_refresh", new=fail_marker),
        ):
            with self.assertRaisesRegex(RuntimeError, "marker failed"):
                await service._replace_series_index("tt123", 42.0, rows)

        self.assertEqual(
            events,
            [
                "begin",
                ("delete", "tt123"),
                ("rows", rows),
                ("marker", "tt123", 42.0),
                "rollback",
            ],
        )

    async def test_conflicting_snapshot_does_not_replace_cached_index(self):
        service = EpisodeIndexService(session=None)
        payload = {
            "meta": {
                "videos": [
                    {
                        "season": 1,
                        "episode": 2,
                        "released": "2026-01-01",
                    },
                    {
                        "season": 1,
                        "episode": 2,
                        "released": "2026-01-02",
                    },
                ]
            }
        }
        with (
            patch(
                "comet.metadata.episode_index.get_metadata_json",
                new=AsyncMock(return_value=MetadataHttpResponse(200, payload)),
            ),
            patch.object(
                service,
                "_replace_series_index",
                new=AsyncMock(),
            ) as replace,
        ):
            await service._refresh_from_cinemeta("tt1234567")

        replace.assert_not_awaited()

    async def test_snapshot_accepts_numeric_coordinates_with_leading_zeroes(self):
        service = EpisodeIndexService(session=None)
        payload = {
            "meta": {
                "videos": [
                    {
                        "season": "01",
                        "episode": "002",
                        "released": "2026-01-01",
                    }
                ]
            }
        }
        with (
            patch(
                "comet.metadata.episode_index.get_metadata_json",
                new=AsyncMock(return_value=MetadataHttpResponse(200, payload)),
            ),
            patch.object(
                service,
                "_replace_series_index",
                new=AsyncMock(),
            ) as replace,
        ):
            await service._refresh_from_cinemeta("tt1234567")

        rows = replace.await_args.args[2]
        self.assertEqual(
            rows,
            [
                {
                    "series_id": "tt1234567",
                    "season": 1,
                    "episode": 2,
                    "air_date": "2026-01-01",
                    "updated_at": rows[0]["updated_at"],
                }
            ],
        )
