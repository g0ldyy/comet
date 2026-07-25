import unittest
from unittest.mock import AsyncMock, patch

from comet.db_cli import (
    _classify_cleanup_media_id,
    _get_debrid_account_cleanup_candidates,
)


class DebridAccountCliTests(unittest.IsolatedAsyncioTestCase):
    def test_cleanup_media_ids_are_classified_by_storage_shape(self):
        self.assertEqual(_classify_cleanup_media_id("tt123"), ("tt123", "imdb"))
        self.assertEqual(_classify_cleanup_media_id("152"), ("152", "kitsu"))
        self.assertEqual(_classify_cleanup_media_id("kitsu:152"), ("152", "kitsu"))
        self.assertEqual(_classify_cleanup_media_id("other"), ("other", None))

    async def test_kitsu_discovery_joins_persisted_provider_ids(self):
        fetch_all = AsyncMock(return_value=[{"media_id": "152", "row_count": 3}])
        with patch("comet.db_cli.database.fetch_all", new=fetch_all):
            candidates = await _get_debrid_account_cleanup_candidates(
                None, min_rows=1, limit=None, provider="kitsu"
            )

        query = fetch_all.await_args.args[0]
        self.assertIn("INNER JOIN anime_ids", query)
        self.assertIn("ai.provider = 'kitsu'", query)
        self.assertEqual(candidates, [("152", 3, "kitsu")])

    async def test_explicit_kitsu_ids_are_normalized_and_deduplicated(self):
        candidates = await _get_debrid_account_cleanup_candidates(
            ["kitsu:152", "152", "tt123"],
            min_rows=1,
            limit=None,
            provider="kitsu",
        )

        self.assertEqual(candidates, [("152", None, "kitsu")])


if __name__ == "__main__":
    unittest.main()
