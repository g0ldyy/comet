import unittest

from comet.metadata.kitsu import _extract_kitsu_metadata


class KitsuMetadataTests(unittest.TestCase):
    def test_kitsu_fields_degrade_independently(self):
        payload = {
            "data": {
                "attributes": {
                    "canonicalTitle": "Valid title",
                    "startDate": ["invalid"],
                    "endDate": "2026-07-22",
                }
            }
        }

        self.assertEqual(
            _extract_kitsu_metadata(payload),
            ("Valid title", None, 2026),
        )

    def test_kitsu_title_uses_ordered_current_fallbacks(self):
        payload = {
            "data": {
                "attributes": {
                    "canonicalTitle": ["invalid"],
                    "titles": {"en": None, "en_jp": "English", "ja_jp": "Japanese"},
                    "startDate": "2025-01-01",
                    "endDate": "2024-01-01",
                }
            }
        }

        self.assertEqual(
            _extract_kitsu_metadata(payload),
            ("English", 2025, None),
        )
        self.assertEqual(_extract_kitsu_metadata({"data": []}), (None, None, None))
