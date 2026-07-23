import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch

from comet.metadata.episode_index import EpisodeIndexService, database


class EpisodeIndexRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_cached_air_date_requires_valid_current_date(self):
        service = EpisodeIndexService(session=None)
        with patch.object(database, "fetch_one", return_value={"air_date": "invalid"}):
            self.assertIsNone(await service._get_cached_air_date("tt123", 1, 2, None))

        with patch.object(
            database,
            "fetch_one",
            return_value={"air_date": "2026-07-22T12:00:00Z"},
        ):
            self.assertEqual(
                await service._get_cached_air_date("tt123", 1, 2, None),
                "2026-07-22",
            )

    async def test_invalid_refresh_timestamp_is_stale(self):
        service = EpisodeIndexService(session=None)
        for value in (None, True, "invalid", float("inf")):
            with (
                self.subTest(value=value),
                patch.object(database, "fetch_val", return_value=value),
            ):
                self.assertFalse(await service._is_series_index_fresh("tt123", 1.0))

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
