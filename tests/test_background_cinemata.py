import unittest
from unittest.mock import AsyncMock

from comet.background_scraper.cinemata_client import (
    CinemataClient,
    _extract_catalog_page,
    _extract_series_episodes,
)
from comet.background_scraper.worker import BackgroundScraperWorker


class CinemataSchemaTests(unittest.IsolatedAsyncioTestCase):
    def test_catalog_page_requires_current_root_and_controls(self):
        valid = {"metas": [None, {"id": "tt1"}, "bad"], "hasMore": True}
        self.assertEqual(_extract_catalog_page(valid), ([{"id": "tt1"}], True, 3))

        for payload in (
            None,
            [],
            {},
            {"metas": {}, "hasMore": False},
            {"metas": [], "hasMore": 0},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    _extract_catalog_page(payload)

    async def test_catalog_pagination_isolates_items_and_uses_raw_page_size(self):
        client = CinemataClient(session=object())
        client._fetch_catalog_page = AsyncMock(
            side_effect=[
                {
                    "metas": [
                        None,
                        {"id": "tt1", "year": 2025},
                        "invalid",
                    ],
                    "hasMore": True,
                },
                {"metas": [{"id": "tt2", "year": 2026}], "hasMore": False},
            ]
        )

        items = [item async for item in client.fetch_all_from_category("movie", "top")]

        self.assertEqual([item["id"] for item in items], ["tt1", "tt2"])
        self.assertEqual(
            [call.args[2] for call in client._fetch_catalog_page.await_args_list],
            [0, 3],
        )

    def test_series_episodes_require_exact_positive_integers(self):
        payload = {
            "meta": {
                "videos": [
                    None,
                    {"season": True, "episode": 1},
                    {"season": 1, "episode": 1.5},
                    {"season": 0, "episode": 1},
                    {"season": 2, "episode": 3},
                    {"season": 1, "number": 4},
                    {"season": 2, "episode": 3},
                ]
            }
        }

        self.assertEqual(
            _extract_series_episodes(payload),
            [{"season": 1, "episode": 4}, {"season": 2, "episode": 3}],
        )
        for invalid in (None, [], {}, {"meta": []}, {"meta": {"videos": {}}}):
            with self.subTest(payload=invalid):
                with self.assertRaises(ValueError):
                    _extract_series_episodes(invalid)

    def test_discovery_metadata_rejects_bad_identity_and_finite_priority(self):
        worker = BackgroundScraperWorker()
        self.assertIsNone(
            worker._normalize_media_item(
                {"id": 123, "name": "Bad", "year": 2026},
                "movie",
                1.0,
                2026,
            )
        )
        self.assertEqual(
            worker._calculate_priority(
                {"imdbRating": "nan", "imdbVotes": float("inf")},
                "movie",
                2026,
                2026,
            ),
            12.0,
        )
        self.assertEqual(
            worker._calculate_priority(
                {"imdbRating": "1e308", "imdbVotes": "9" * 300},
                "movie",
                2026,
                2026,
            ),
            16.0,
        )


if __name__ == "__main__":
    unittest.main()
