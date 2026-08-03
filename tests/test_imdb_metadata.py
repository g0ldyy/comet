import unittest

from comet.metadata.imdb import (
    _extract_cinemeta_metadata,
    _extract_imdb_metadata,
)


class ImdbMetadataTests(unittest.TestCase):
    def test_imdb_extractor_selects_the_expected_result(self):
        payload = {
            "d": [
                {
                    "id": "tt7654321",
                    "l": "Valid",
                    "y": 2026,
                    "yr": "2026-2028",
                },
            ]
        }

        self.assertEqual(_extract_imdb_metadata(payload), ("Valid", 2026, 2028))
        self.assertEqual(
            _extract_imdb_metadata(payload, "tt1234567"),
            (None, None, None),
        )

    def test_cinemeta_extractor_reads_the_current_meta_object(self):
        self.assertEqual(
            _extract_cinemeta_metadata(
                {"meta": {"name": "Valid", "releaseInfo": "2024-2026"}}
            ),
            ("Valid", 2024, 2026),
        )
