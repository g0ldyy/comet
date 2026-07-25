import unittest

from comet.metadata.imdb import (
    _extract_cinemeta_metadata,
    _extract_imdb_metadata,
)


class ImdbMetadataTests(unittest.TestCase):
    def test_imdb_extractor_isolates_invalid_results(self):
        payload = {
            "d": [
                None,
                {"id": 42, "l": "wrong ID"},
                {"id": "tt-episode/one", "l": "episode"},
                {"id": "tt123", "l": ["wrong title"]},
                {"id": "tt456", "l": "Valid", "y": 2026, "yr": "2026-2028"},
            ]
        }

        self.assertEqual(_extract_imdb_metadata(payload), ("Valid", 2026, 2028))
        self.assertEqual(
            _extract_imdb_metadata({"d": {"id": "tt123"}}), (None, None, None)
        )

    def test_cinemeta_extractor_requires_current_meta_object(self):
        self.assertEqual(
            _extract_cinemeta_metadata({"meta": []}),
            (None, None, None),
        )
        self.assertEqual(
            _extract_cinemeta_metadata(
                {"meta": {"name": "Valid", "releaseInfo": "2024-2026"}}
            ),
            ("Valid", 2024, 2026),
        )
