import unittest
from unittest.mock import patch

from RTN import parse

from comet.services.filtering import (
    _clone_parsed,
    _normalize_aliases,
    filter_worker,
    exact_alias_match,
    settings,
)


class AliasFilteringTests(unittest.TestCase):
    def test_cached_parse_clone_detaches_mutated_languages(self):
        cached = parse("Movie.2024.MULTI.FRENCH.1080p.WEB-DL")
        clone = _clone_parsed(cached)

        clone.languages.append("de")

        self.assertNotIn("de", cached.languages)
        self.assertIn("de", clone.languages)

    def test_empty_alias_does_not_match_every_title(self):
        self.assertFalse(exact_alias_match("unrelated title", [""]))

    def test_short_or_partial_alias_does_not_bypass_title_matching(self):
        self.assertFalse(exact_alias_match("quality release", ["it"]))
        self.assertFalse(exact_alias_match("friends swapped places", ["swap"]))
        self.assertFalse(exact_alias_match("friends swapped places", ["swapped"]))
        self.assertTrue(exact_alias_match("swapped", ["swapped"]))

    def test_alias_normalization_keeps_only_current_unique_entries(self):
        self.assertEqual(
            _normalize_aliases(
                {
                    "": ["Ignored"],
                    "en": "Ignored",
                    "fr": [None, "", "  ", " Titre ", "Titre", 1],
                }
            ),
            {"fr": ["Titre"]},
        )
        self.assertEqual(_normalize_aliases([]), {})

    def test_empty_alias_cannot_bypass_worker_title_matching(self):
        torrents = [
            {
                "title": "Completely.Different.2024.1080p.WEB-DL.x264",
                "infoHash": "1" * 40,
            }
        ]

        actual = filter_worker(
            torrents,
            "The Matrix",
            1999,
            0,
            "movie",
            {"ez": [""]},
            False,
        )

        self.assertEqual(actual, [])

    def test_language_scoped_alias_sets_the_exact_language(self):
        torrent = {
            "title": "Il.Postino.2020.1080p.WEB-DL",
            "infoHash": "1" * 40,
        }

        with patch.object(settings, "SMART_LANGUAGE_DETECTION", True):
            actual = filter_worker(
                [torrent],
                "The Postman",
                2020,
                None,
                "movie",
                {"lang:it": ["Il Postino"]},
                False,
            )

        self.assertEqual(actual[0]["parsed"].languages, ["it"])


if __name__ == "__main__":
    unittest.main()
