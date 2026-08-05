import unittest

from comet.usenet.archive_passwords import resolve_archive_passphrase


class ArchivePasswordTests(unittest.TestCase):
    def test_metadata_takes_precedence_and_is_trimmed(self):
        self.assertEqual(
            resolve_archive_passphrase(
                {"password": "  metadata secret  "}, "Release {{title secret}}"
            ),
            "metadata secret",
        )

    def test_extracts_double_and_single_brace_title_tokens(self):
        self.assertEqual(
            resolve_archive_passphrase({}, "Release {{double secret}} 2026"),
            "double secret",
        )
        self.assertEqual(
            resolve_archive_passphrase({}, "Release {single secret} 2026"),
            "single secret",
        )

    def test_rejects_empty_unclosed_control_and_oversized_tokens(self):
        self.assertIsNone(resolve_archive_passphrase({}, "Release {}"))
        self.assertIsNone(resolve_archive_passphrase({}, "Release {{}}"))
        self.assertIsNone(resolve_archive_passphrase({}, "Release {unclosed"))
        self.assertIsNone(resolve_archive_passphrase({}, "Release {line\nbreak}"))
        self.assertIsNone(
            resolve_archive_passphrase({}, "Release {" + "x" * 4097 + "}")
        )


if __name__ == "__main__":
    unittest.main()
