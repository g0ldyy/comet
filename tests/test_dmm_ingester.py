import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from comet.services.dmm_ingester import extract_zip_sync, process_file_sync


class DmmArchiveTests(unittest.TestCase):
    def test_hashlist_decode_distinguishes_retryable_failure_from_valid_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertIsNone(process_file_sync(root / "missing.html"))

            irrelevant = root / "irrelevant.html"
            irrelevant.write_text("no hashlist here")
            self.assertEqual(process_file_sync(irrelevant), [])

            hashlist = root / "hashlist.html"
            hashlist.write_text('hashlist#payload"')
            with patch(
                "comet.services.dmm_ingester.decompressFromEncodedURIComponent",
                return_value=None,
            ):
                self.assertIsNone(process_file_sync(hashlist))
            with patch(
                "comet.services.dmm_ingester.decompressFromEncodedURIComponent",
                return_value='{"unexpected": []}',
            ):
                self.assertIsNone(process_file_sync(hashlist))
            with patch(
                "comet.services.dmm_ingester.decompressFromEncodedURIComponent",
                return_value="[]",
            ):
                self.assertEqual(process_file_sync(hashlist), [])

    def test_hashlist_decode_isolates_malformed_items(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mixed.html"
            path.write_text('hashlist#payload"')
            payload = """[
                null,
                {"filename": 42},
                {"filename": "Bad.Size.2026", "hash": "bbbb", "bytes": "1"},
                {
                    "filename": "Valid.Movie.2026.1080p.WEB-DL",
                    "hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "bytes": 1
                }
            ]"""

            with patch(
                "comet.services.dmm_ingester.decompressFromEncodedURIComponent",
                return_value=payload,
            ):
                self.assertEqual(
                    process_file_sync(path),
                    [
                        {
                            "hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                            "filename": "Valid.Movie.2026.1080p.WEB-DL",
                            "size": 1,
                            "parsed_title": "Valid Movie",
                            "parsed_year": 2026,
                        }
                    ],
                )

    def test_extract_rejects_path_traversal_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "dmm.zip"
            target = root / "target"
            with zipfile.ZipFile(archive, "w") as zip_file:
                zip_file.writestr("valid/data.html", "valid")
                zip_file.writestr("../escaped.html", "escaped")

            with self.assertRaisesRegex(ValueError, "Unsafe DMM archive member"):
                extract_zip_sync(archive, target)

            self.assertFalse(target.exists())
            self.assertFalse((root / "escaped.html").exists())

    def test_extract_rejects_symlink_members(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "dmm.zip"
            target = root / "target"
            link = zipfile.ZipInfo("link")
            link.create_system = 3
            link.external_attr = 0o120777 << 16
            with zipfile.ZipFile(archive, "w") as zip_file:
                zip_file.writestr(link, "../outside")

            with self.assertRaisesRegex(ValueError, "Unsafe DMM archive member"):
                extract_zip_sync(archive, target)

            self.assertFalse(target.exists())

    def test_extract_accepts_current_nested_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "dmm.zip"
            target = root / "target"
            with zipfile.ZipFile(archive, "w") as zip_file:
                zip_file.writestr("hashlists/data.html", "valid")

            extract_zip_sync(archive, target)

            self.assertEqual((target / "hashlists" / "data.html").read_text(), "valid")
