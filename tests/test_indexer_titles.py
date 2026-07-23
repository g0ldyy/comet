import unittest

from comet.scrapers.base import deduplicate_torrents
from comet.scrapers.models import ScrapeRequest
from comet.utils.languages import select_indexer_titles


class IndexerTitleTests(unittest.TestCase):
    def test_torrents_are_deduplicated_by_hash_and_file(self):
        torrents = [
            {"infoHash": "A" * 40, "fileIndex": 1, "title": "Canonical"},
            {"infoHash": "a" * 40, "fileIndex": 1, "title": "Localized"},
            {"infoHash": "a" * 40, "fileIndex": 2, "title": "Second file"},
        ]

        self.assertEqual(
            deduplicate_torrents(torrents),
            [torrents[0], torrents[2]],
        )

    def test_default_includes_canonical_and_configured_languages(self):
        aliases = {
            "us": ["The Life Ahead"],
            "lang:fr": ["La Vie devant soi", "la vie devant soi"],
            "lang:it": ["La vita davanti a sé"],
            "br": ["Rosa e Momo"],
            "ez": ["Unattributed title"],
        }

        self.assertEqual(
            select_indexer_titles("The Life Ahead", aliases, ["it", "fr"]),
            (
                "The Life Ahead",
                "La vita davanti a se",
                "La Vie devant soi",
            ),
        )

    def test_latin_diacritics_are_removed_without_an_alternative_alias(self):
        self.assertEqual(
            select_indexer_titles(
                "A Prophet",
                {
                    "original:fr": ["Un prophète"],
                    "lang:fr": ["Un prophète", "Un prophete"],
                },
                ["fr"],
                include_canonical=False,
            ),
            ("Un prophete",),
        )

        self.assertEqual(
            select_indexer_titles(
                "Dune: Part Two",
                {"lang:fr": ["Dune : Deuxième partie"]},
                ["fr"],
                include_canonical=False,
                include_original=False,
            ),
            ("Dune : Deuxieme partie",),
        )

    def test_non_latin_diacritics_are_preserved(self):
        self.assertEqual(
            select_indexer_titles("が Й", {}, []),
            ("が Й",),
        )

    def test_original_and_localized_titles_are_selected_without_canonical_title(self):
        aliases = {
            "original:it": ["La vita davanti a sé"],
            "lang:fr": ["La Vie devant soi"],
            "lang:en": ["The Life Ahead"],
        }

        self.assertEqual(
            select_indexer_titles("The Life Ahead", aliases, ["fr"]),
            ("The Life Ahead", "La vita davanti a se", "La Vie devant soi"),
        )
        self.assertEqual(
            select_indexer_titles(
                "The Life Ahead",
                aliases,
                ["fr"],
                include_canonical=False,
            ),
            ("La vita davanti a se", "La Vie devant soi"),
        )

    def test_anime_uses_one_original_and_one_localized_title(self):
        aliases = {
            "original": ["Kono Subarashii Sekai ni Shukufuku wo! Movie"],
            "ez": [f"Unclassified synonym {index}" for index in range(49)],
            "lang:fr": ["Konosuba : La légende de Crimson"],
        }

        self.assertEqual(
            select_indexer_titles("Konosuba! Legend of Crimson", aliases, ["fr"]),
            (
                "Konosuba! Legend of Crimson",
                "Kono Subarashii Sekai ni Shukufuku wo! Movie",
                "Konosuba : La legende de Crimson",
            ),
        )

    def test_anime_canonical_title_is_the_bounded_fallback(self):
        self.assertEqual(
            select_indexer_titles(
                "Main",
                {
                    "original": ["Romaji"],
                    "ez": ["Main", "Romaji", "Japanese", "Russian"],
                },
                ["fr"],
                include_canonical=False,
            ),
            ("Romaji",),
        )

    def test_original_title_does_not_require_localized_languages(self):
        self.assertEqual(
            select_indexer_titles(
                "  The   Life Ahead  ",
                {"original:it": ["La vita davanti a sé"]},
                [],
            ),
            ("The Life Ahead", "La vita davanti a se"),
        )

    def test_every_title_source_can_be_disabled_independently(self):
        aliases = {
            "original:it": ["La vita davanti a sé"],
            "lang:fr": ["La Vie devant soi"],
        }

        self.assertEqual(
            select_indexer_titles(
                "The Life Ahead",
                aliases,
                ["fr"],
                include_original=False,
            ),
            ("The Life Ahead", "La Vie devant soi"),
        )
        self.assertEqual(
            select_indexer_titles(
                "The Life Ahead",
                aliases,
                ["fr"],
                include_canonical=False,
                include_original=False,
            ),
            ("La Vie devant soi",),
        )

    def test_empty_selection_safely_falls_back_to_canonical_title(self):
        self.assertEqual(
            select_indexer_titles(
                "The Life Ahead",
                {},
                [],
                include_canonical=False,
                include_original=False,
            ),
            ("The Life Ahead",),
        )

    def test_episode_variants_are_generated_for_every_title(self):
        request = ScrapeRequest(
            media_type="series",
            media_id="tt123:2:3",
            media_only_id="tt123",
            title="English",
            season=2,
            episode=3,
            search_titles=("English", "Localized"),
        )

        self.assertEqual(
            request.title_queries(include_episode_variants=True),
            (
                "English",
                "English S02",
                "English S02E03",
                "Localized",
                "Localized S02",
                "Localized S02E03",
            ),
        )
