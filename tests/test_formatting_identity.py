import unittest
from decimal import Decimal

from comet.utils.formatting import format_bytes, size_to_bytes
from comet.utils.media_ids import normalize_cache_media_ids
from comet.utils.status_keys import normalize_status_key


class FormattingIdentityContractTests(unittest.TestCase):
    def test_byte_formatting_rejects_nonfinite_negative_and_coerced_values(self):
        self.assertEqual(format_bytes(0), "0.0 B")
        self.assertEqual(format_bytes(Decimal("1536")), "1.5 KB")

        for value in (True, "1024", -1, float("nan"), float("inf")):
            with self.subTest(value=value):
                self.assertIsNone(format_bytes(value))

    def test_size_parser_requires_two_finite_nonnegative_current_fields(self):
        self.assertEqual(size_to_bytes("1.5 GB"), 1_610_612_736)
        self.assertEqual(size_to_bytes("0 B"), 0)

        for value in (
            None,
            42,
            "",
            "1",
            "1 GB extra",
            "bad GB",
            "-1 GB",
            "nan GB",
            "inf GB",
            "1 PB",
        ):
            with self.subTest(value=value):
                self.assertIsNone(size_to_bytes(value))

    def test_cache_media_ids_filter_corrupt_entries_without_aliasing_primary(self):
        self.assertEqual(
            normalize_cache_media_ids(
                "tt1234567",
                ["kitsu:123", None, {}, "", "kitsu:123"],
            ),
            ["tt1234567", "kitsu:123"],
        )
        self.assertEqual(normalize_cache_media_ids("tt1234567", None), ["tt1234567"])

        for primary_id in (None, "", 42):
            with self.subTest(primary_id=primary_id), self.assertRaises(ValueError):
                normalize_cache_media_ids(primary_id, None)
        with self.assertRaises(TypeError):
            normalize_cache_media_ids("tt1234567", "kitsu:123")

    def test_status_keys_do_not_coerce_non_string_error_codes(self):
        self.assertEqual(normalize_status_key(" store/error-code "), "STORE_ERROR_CODE")
        for value in (None, "", 0, 404, True, ["ERROR"]):
            with self.subTest(value=value):
                self.assertIsNone(normalize_status_key(value))


if __name__ == "__main__":
    unittest.main()
