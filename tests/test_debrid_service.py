import unittest
from unittest.mock import patch

from comet.services.debrid import DebridService


class DebridServiceCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_corrupt_cached_parse_is_isolated_to_optional_enrichment(self):
        service = DebridService("realdebrid", "token", "")
        torrents = {
            "a" * 40: {"parsed": None},
            "b" * 40: {"parsed": None},
        }
        rows = [
            {
                "info_hash": "a" * 40,
                "file_index": "1",
                "title": "Corrupt.mkv",
                "size": 100,
                "parsed": "not-json",
            },
            {
                "info_hash": "b" * 40,
                "file_index": "2",
                "title": "Valid.mkv",
                "size": 200,
                "parsed": '{"raw_title":"Valid.mkv"}',
            },
        ]

        with patch(
            "comet.services.debrid.get_cached_availability",
            return_value=rows,
        ):
            cached, updates = await service.check_existing_availability(
                list(torrents), None, None, torrents
            )

        self.assertEqual(cached, set(torrents))
        self.assertNotIn("fileIndex", torrents["a" * 40])
        self.assertNotIn("fileIndex", torrents["b" * 40])
        self.assertIsNone(torrents["a" * 40]["parsed"])
        self.assertEqual(updates["a" * 40]["fileIndex"], 1)
        self.assertNotIn("parsed", updates["a" * 40])
        self.assertEqual(updates["b" * 40]["parsed"].raw_title, "Valid.mkv")
