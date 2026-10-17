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

    def test_hashlist_decode_reads_stored_list_by_id(self):
        list_id = "88ef286f-8582-434e-8d3b-2a61a678cf82"
        torrents = (
            '[{"filename": "Valid.Movie.2026.1080p.WEB-DL", "hash": "aa", "bytes": 1}]'
        )
        iframe = (
            '<iframe src="https://debridmediamanager.com/hashlist#payload"></iframe>'
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "lists").mkdir()
            page = root / f"{list_id}.html"
            page.write_text(
                f'<iframe src="https://debridmediamanager.com/hashlist#id={list_id}"></iframe>'
            )

            self.assertIsNone(process_file_sync(page))

            for stored in ("payload\n", f"<html>{iframe}</html>"):
                (root / "lists" / f"{list_id}.txt").write_text(stored)
                with (
                    patch(
                        "comet.services.dmm_ingester.decompressFromEncodedURIComponent",
                        side_effect=lambda data: (
                            torrents if data == "payload" else None
                        ),
                    ),
                    patch("comet.services.dmm_ingester.RTN.parse"),
                ):
                    result = process_file_sync(page)
                self.assertEqual([r["hash"] for r in result], ["aa"])

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
