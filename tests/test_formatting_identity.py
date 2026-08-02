import unittest
from decimal import Decimal

from RTN import ParsedData

from comet.utils.formatting import (
    format_audio_info,
    format_bytes,
    format_group_info,
    format_quality_info,
    format_video_info,
    get_language_emoji,
    normalize_info_hash,
    size_to_bytes,
)
from comet.utils.media_ids import normalize_cache_media_ids
from comet.utils.status_keys import normalize_status_key
from comet.utils.year import parse_year, parse_year_range


class FormattingIdentityContractTests(unittest.TestCase):
    def test_punjabi_uses_the_indian_flag(self):
        self.assertEqual(get_language_emoji("pa"), "🇮🇳")

    def test_byte_formatting_rejects_nonfinite_negative_and_coerced_values(self):
        self.assertEqual(format_bytes(0), "0.0 B")
        self.assertEqual(format_bytes(Decimal(1536)), "1.5 KB")

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
            "1e100 TB",
            "1 PB",
        ):
            with self.subTest(value=value):
                self.assertIsNone(size_to_bytes(value))

    def test_info_hash_normalization_preserves_supported_encodings(self):
        canonical = "0123456789abcdef0123456789abcdef01234567"
        base32 = "AERUKZ4JVPG66AJDIVTYTK6N54ASGRLH"
        ascii_hex = canonical.encode("ascii").hex()

        self.assertEqual(normalize_info_hash(canonical.upper()), canonical)
        self.assertEqual(normalize_info_hash(base32), canonical)
        self.assertEqual(normalize_info_hash(ascii_hex), canonical)
        self.assertEqual(normalize_info_hash("!" * 32), "!" * 32)
        self.assertEqual(normalize_info_hash("zz" * 40), "zz" * 40)

    def test_integer_years_accept_the_complete_four_digit_media_domain(self):
        self.assertEqual(parse_year(1800), 1800)
        self.assertEqual(parse_year(2100), 2100)
        self.assertEqual(parse_year("Released in 9999"), 9999)
        self.assertEqual(parse_year_range(9999), (9999, None))

        for value in (True, 1799, 10_000, -1):
            with self.subTest(value=value):
                self.assertIsNone(parse_year(value))
                self.assertEqual(parse_year_range(value), (None, None))

    def test_parsed_data_formatting_uses_the_exact_current_fields(self):
        parsed = ParsedData(
            raw_title="Movie.2026",
            codec="hevc",
            hdr=["DV", "HDR"],
            bit_depth="10bit",
            audio=["Dolby Digital Plus"],
            channels=["5.1"],
            quality="WEB-DL",
            edition="Director's Cut",
            proper=True,
            repack=True,
            upscaled=True,
            remastered=True,
            extended=True,
            group="ReleaseGroup",
        )

        self.assertEqual(format_video_info(parsed), "hevc • DV • HDR • 10bit")
        self.assertEqual(format_audio_info(parsed), "Dolby Digital Plus • 5.1")
        self.assertEqual(
            format_quality_info(parsed),
            "WEB-DL • Director's Cut • PROPER • REPACK • UPSCALED • REMASTERED • EXTENDED",
        )
        self.assertEqual(format_group_info(parsed), "ReleaseGroup")

    def test_cache_media_ids_filter_corrupt_entries_without_aliasing_primary(self):
        self.assertEqual(
            normalize_cache_media_ids(
                "tt1234567",
                ["kitsu:123", None, {}, "", "kitsu:123"],
            ),
            ["tt1234567", "kitsu:123"],
        )
        self.assertEqual(normalize_cache_media_ids("tt1234567", None), ["tt1234567"])
        self.assertEqual(
            len(
                normalize_cache_media_ids(
                    "tt1234567",
                    [f"kitsu:{index}" for index in range(100)],
                )
            ),
            64,
        )

        for primary_id in (None, "", "é" * 65, "\ud800", 42):
            with self.subTest(primary_id=primary_id), self.assertRaises(ValueError):
                normalize_cache_media_ids(primary_id, None)
        with self.assertRaises(TypeError):
            normalize_cache_media_ids("tt1234567", "kitsu:123")

    def test_status_keys_do_not_coerce_non_string_error_codes(self):
        self.assertEqual(normalize_status_key(" store/error-code "), "STORE_ERROR_CODE")
        for value in (None, "", "x" * 129, 0, 404, True, ["ERROR"]):
            with self.subTest(value=value):
                self.assertIsNone(normalize_status_key(value))


if __name__ == "__main__":
    unittest.main()


def test_archive_member_identity_encodings_agree_and_are_pinned():
    """The bytes and hex forms must stay one derivation; divergence corrupts asset IDs."""
    from comet.usenet.identity import archive_member_id, archive_member_identity

    set_identity = "a" * 64
    member = archive_member_id(set_identity, "Season 01/ep01.mkv", 1234)
    assert (
        archive_member_identity(set_identity, "Season 01/ep01.mkv", 1234)
        == member.hex()
    )
    assert len(member) == 32
    # length-framing must make these distinct rather than colliding on concatenation
    assert archive_member_id(set_identity, "ab", 1) != archive_member_id(
        set_identity, "a", 1
    )
    assert archive_member_id(set_identity, "a", 1) != archive_member_id(
        set_identity, "a", 2
    )
    assert archive_member_id("b" * 64, "a", 1) != archive_member_id(
        set_identity, "a", 1
    )
    # pinned value: a change here is a wire-visible identity change, not a refactor
    assert (
        member.hex()
        == "bea3cf4cd323bf7d0a36769cda739907a431b34d1d7fef28e72840b6bc21794d"
    )
