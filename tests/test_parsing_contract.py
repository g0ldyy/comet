import unittest
from types import SimpleNamespace

from comet.utils.parsing import (
    MediaScope,
    parse_media_id,
    parse_optional_int,
    resolve_media_scope,
)
from comet.utils.torrent_cache import build_torrent_cache_where


class MediaIdContractTests(unittest.TestCase):
    def test_current_imdb_and_kitsu_shapes(self):
        self.assertEqual(
            parse_media_id("movie", "tt1234567"), ("tt1234567", None, None)
        )
        self.assertEqual(
            parse_media_id("series", "tt1234567"),
            ("tt1234567", None, None),
        )
        self.assertEqual(
            parse_media_id("series", "tt1234567:2"),
            ("tt1234567", 2, None),
        )
        self.assertEqual(
            parse_media_id("series", "tt1234567:0:2"),
            ("tt1234567", 0, 2),
        )
        self.assertEqual(parse_media_id("movie", "kitsu:123"), ("123", 1, None))
        self.assertEqual(
            parse_media_id("series", "kitsu:123:4"),
            ("123", 1, 4),
        )

    def test_legacy_and_noncanonical_media_ids_are_rejected(self):
        invalid = (
            ("movie", "imdb_id:tt1234567"),
            ("movie", "tt123"),
            ("movie", "tt1234567:1:2"),
            ("series", "tt1234567:01"),
            ("series", "tt1234567:-1"),
            ("series", "tt1234567:01:2"),
            ("series", "tt1234567:-1:2"),
            ("movie", "kitsu:"),
            ("movie", "kitsu:0"),
            ("movie", "kitsu:123:4"),
            ("series", "kitsu:123:bad"),
            ("anime", "kitsu:123"),
        )

        for media_type, media_id in invalid:
            with self.subTest(media_type=media_type, media_id=media_id):
                with self.assertRaises(ValueError):
                    parse_media_id(media_type, media_id)

    def test_media_scope_resolution_is_unambiguous(self):
        self.assertIs(resolve_media_scope("movie", None, None), MediaScope.MOVIE)
        self.assertIs(resolve_media_scope("series", None, None), MediaScope.SERIES)
        self.assertIs(resolve_media_scope("series", 2, None), MediaScope.SEASON)
        self.assertIs(resolve_media_scope("series", 2, 1), MediaScope.EPISODE)

    def test_aggregate_scopes_include_known_descendants(self):
        episode = SimpleNamespace(seasons=[2], episodes=[3])
        bundle = SimpleNamespace(seasons=[2], episodes=[1, 2])
        pack = SimpleNamespace(seasons=[2], episodes=[])

        self.assertTrue(MediaScope.SERIES.matches_parsed(episode, None, None))
        self.assertTrue(MediaScope.SEASON.matches_parsed(episode, 2, None))
        self.assertFalse(MediaScope.SEASON.matches_parsed(episode, 1, None))
        self.assertGreater(
            MediaScope.SEASON.granularity_priority(pack),
            MediaScope.SEASON.granularity_priority(bundle),
        )
        self.assertGreater(
            MediaScope.SEASON.granularity_priority(bundle),
            MediaScope.SEASON.granularity_priority(episode),
        )

    def test_torrent_cache_scope_queries_follow_the_media_hierarchy(self):
        series_where, series_params = build_torrent_cache_where(
            "tt1234567", MediaScope.SERIES, None, None
        )
        season_where, season_params = build_torrent_cache_where(
            "tt1234567", MediaScope.SEASON, 2, None
        )
        episode_where, episode_params = build_torrent_cache_where(
            "tt1234567", MediaScope.EPISODE, 2, 3
        )

        self.assertNotIn("season =", series_where)
        self.assertNotIn("episode =", series_where)
        self.assertEqual(series_params, {"media_id": "tt1234567"})
        self.assertIn("season =", season_where)
        self.assertNotIn("episode =", season_where)
        self.assertEqual(
            season_params,
            {"media_id": "tt1234567", "season": 2},
        )
        self.assertIn("season =", episode_where)
        self.assertIn("episode =", episode_where)
        self.assertEqual(
            episode_params,
            {"media_id": "tt1234567", "season": 2, "episode": 3},
        )

    def test_optional_integer_accepts_only_current_path_form(self):
        self.assertIsNone(parse_optional_int("n"))
        self.assertIsNone(parse_optional_int(None))
        self.assertEqual(parse_optional_int("0"), 0)
        self.assertEqual(parse_optional_int("12"), 12)

        for value in ("-1", "+1", "01", " 1", "1.0", True, 1):
            with self.subTest(value=value):
                self.assertIsNone(parse_optional_int(value))


if __name__ == "__main__":
    unittest.main()
