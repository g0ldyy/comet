import unittest

from comet.discovery.torrent_base import deduplicate_torrents
from comet.discovery.torrent_models import ScrapeRequest
from comet.utils.languages import MAX_INDEXER_TITLES, select_indexer_titles


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

    def test_indexer_titles_are_capped_in_stable_priority_order(self):
        languages = ("aa", "ab", "ac", "ad", "ae", "af", "ag", "ah", "ai", "aj")
        aliases = {
            "original": ["Original"],
            **{
                f"lang:{language}": [f"Localized {index}"]
                for index, language in enumerate(languages)
            },
        }

        self.assertEqual(
            select_indexer_titles("Canonical", aliases, list(languages)),
            (
                "Canonical",
                "Original",
                "Localized 0",
                "Localized 1",
                "Localized 2",
                "Localized 3",
                "Localized 4",
                "Localized 5",
            ),
        )

    def test_unsafe_indexer_titles_are_rejected_before_fallback(self):
        oversized = "界" * 171

        self.assertEqual(
            select_indexer_titles(
                "Safe fallback",
                {
                    "original": ["unsafe\x00title"],
                    "lang:fr": [oversized, "unsafe\ntitle", "Titre sûr"],
                },
                ["fr"],
                include_canonical=False,
            ),
            ("Titre sur",),
        )
        self.assertEqual(
            select_indexer_titles(
                "Safe fallback",
                {"original": ["unsafe\x7ftitle"]},
                [],
                include_canonical=False,
            ),
            ("Safe fallback",),
        )

    def test_direct_request_titles_are_capped_before_variant_expansion(self):
        titles = tuple(f"Title {index}" for index in range(MAX_INDEXER_TITLES + 5))
        request = ScrapeRequest(
            media_type="series",
            media_id="tt123:2:3",
            media_only_id="tt123",
            title="Canonical",
            season=2,
            episode=3,
            search_titles=titles,
        )

        self.assertEqual(request.query_titles, titles[:MAX_INDEXER_TITLES])
        self.assertEqual(
            len(request.title_queries(include_episode_variants=True)),
            MAX_INDEXER_TITLES * 3,
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

    def test_scoped_queries_avoid_broad_episode_searches(self):
        request = ScrapeRequest(
            media_type="series",
            media_id="tt123:2:3",
            media_only_id="tt123",
            title="Canonical",
            season=2,
            episode=3,
            search_titles=("Canonical", "Original"),
        )

        self.assertEqual(
            request.scoped_query_titles(),
            ("Canonical S02E03", "Original S02E03"),
        )

    def test_season_variants_do_not_require_an_episode(self):
        request = ScrapeRequest(
            media_type="series",
            media_id="tt123:2",
            media_only_id="tt123",
            title="English",
            season=2,
            search_titles=("English", "Localized"),
        )

        self.assertEqual(
            request.title_queries(include_episode_variants=True),
            (
                "English",
                "English S02",
                "Localized",
                "Localized S02",
            ),
        )
