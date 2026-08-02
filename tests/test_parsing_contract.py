import unittest
from types import SimpleNamespace

from comet.utils.parsing import (
    MediaScope,
    parse_media_id,
    parse_optional_int,
    resolve_media_scope,
)


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

    def test_optional_integer_accepts_only_current_path_form(self):
        self.assertIsNone(parse_optional_int("n"))
        self.assertIsNone(parse_optional_int(None))
        self.assertEqual(parse_optional_int("0"), 0)
        self.assertEqual(parse_optional_int("12"), 12)
        self.assertEqual(parse_optional_int("65535"), 65_535)

        for value in (
            "-1",
            "+1",
            "01",
            " 1",
            "1.0",
            "65536",
            "9" * 10_000,
            True,
            1,
        ):
            with self.subTest(value=value):
                self.assertIsNone(parse_optional_int(value))

    def test_media_ids_reject_values_outside_shared_storage_domains(self):
        for media_type, media_id in (
            ("series", "tt1234567:65536"),
            ("series", "tt1234567:1:65536"),
            ("movie", f"kitsu:{2**63}"),
            ("movie", f"kitsu:{'9' * 10_000}"),
        ):
            with self.subTest(media_id=media_id), self.assertRaises(ValueError):
                parse_media_id(media_type, media_id)


if __name__ == "__main__":
    unittest.main()


class AirDateYearGuardTests(unittest.TestCase):
    """str.isdigit() accepts digits int() then rejects; the year guard must screen ASCII."""

    @staticmethod
    def _parsed():
        return SimpleNamespace(
            seasons=(), episodes=(), date=None, year=2026, complete=True
        )

    def _match(self, air_date):
        from comet.utils.parsing import match_parsed_episode_target

        return match_parsed_episode_target(
            self._parsed(),
            1,
            2,
            target_air_date=air_date,
            reject_unknown_episode_files=True,
        )

    def test_non_ascii_air_date_year_is_rejected_not_raised(self):
        for air_date in ("\u00b2026-01-02", "\u0662026-01-02", "\uff12026-01-02"):
            self.assertFalse(self._match(air_date))

    def test_ascii_air_date_year_still_matches_and_mismatches(self):
        self.assertTrue(self._match("2026-01-02"))
        self.assertFalse(self._match("2019-01-02"))
