import unittest

from comet.utils.parsing import parse_media_id, parse_optional_int


class MediaIdContractTests(unittest.TestCase):
    def test_current_imdb_and_kitsu_shapes(self):
        self.assertEqual(
            parse_media_id("movie", "tt1234567"), ("tt1234567", None, None)
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
            ("series", "tt1234567"),
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
