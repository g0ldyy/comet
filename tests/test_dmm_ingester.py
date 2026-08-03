import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

from comet.services.dmm_ingester import (
    DMMIngester,
    extract_zip_sync,
    process_file_sync,
)
from comet.utils.lzstring import decompressFromEncodedURIComponent


class DmmArchiveTests(unittest.TestCase):
    def test_lz_decoder_has_exact_input_and_output_boundaries(self):
        encoded = "BIUwNmD2AEDukCcwBMg"
        self.assertEqual(
            decompressFromEncodedURIComponent(encoded, maximum=11),
            "Hello world",
        )
        self.assertIsNone(decompressFromEncodedURIComponent(encoded, maximum=10))
        self.assertIsNone(decompressFromEncodedURIComponent("invalid%character"))
        self.assertIsNone(decompressFromEncodedURIComponent("A"))
        with patch("comet.utils.lzstring._MAX_COMPRESSED_CHARACTERS", 4):
            self.assertIsNone(decompressFromEncodedURIComponent("A" * 5))

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
            ) as decompress:
                self.assertEqual(process_file_sync(hashlist), [])
                self.assertEqual(
                    decompress.call_args.kwargs["maximum"],
                    64 * 1024 * 1024,
                )

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

    def test_hashlist_parser_failure_propagates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hashlist.html"
            path.write_text('hashlist#payload"')
            payload = """[{
                "filename": "Valid.Movie.2026.1080p.WEB-DL",
                "hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "bytes": 1
            }]"""

            with (
                patch(
                    "comet.services.dmm_ingester.decompressFromEncodedURIComponent",
                    return_value=payload,
                ),
                patch(
                    "comet.services.dmm_ingester.RTN.parse",
                    side_effect=RuntimeError("parser failure"),
                ),
                self.assertRaisesRegex(RuntimeError, "parser failure"),
            ):
                process_file_sync(path)

    def test_hashlist_decode_bounds_file_and_item_cardinality(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hashlist.html"
            path.write_text('hashlist#payload"')
            with patch(
                "comet.services.dmm_ingester._MAX_ARCHIVE_MEMBER_BYTES",
                8,
            ):
                self.assertIsNone(process_file_sync(path))

            with (
                patch(
                    "comet.services.dmm_ingester.decompressFromEncodedURIComponent",
                    return_value="[{}, {}, {}]",
                ),
                patch("comet.services.dmm_ingester._MAX_HASHLIST_ITEMS", 2),
            ):
                self.assertIsNone(process_file_sync(path))

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
            self.assertEqual(
                (target / "hashlists" / "data.html").stat().st_mode & 0o777,
                0o600,
            )

    def test_extract_accepts_highly_compressible_bounded_member(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "dmm.zip"
            target = root / "target"
            content = b"0" * (1024 * 1024)
            with zipfile.ZipFile(
                archive,
                "w",
                compression=zipfile.ZIP_DEFLATED,
            ) as zip_file:
                zip_file.writestr("hashlists/data.html", content)

            extract_zip_sync(archive, target)

            self.assertEqual((target / "hashlists" / "data.html").read_bytes(), content)

    def test_extract_rejects_oversized_or_duplicate_members_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "dmm.zip"
            target = root / "target"
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with zipfile.ZipFile(archive, "w") as zip_file:
                    zip_file.writestr("one.html", "12345")
                    zip_file.writestr("one.html", "duplicate")

            with self.assertRaisesRegex(ValueError, "Unsafe DMM archive member"):
                extract_zip_sync(archive, target)

            self.assertFalse(target.exists())

            with zipfile.ZipFile(archive, "w") as zip_file:
                zip_file.writestr("large.html", "12345")
            with (
                patch(
                    "comet.services.dmm_ingester._MAX_ARCHIVE_MEMBER_BYTES",
                    4,
                ),
                self.assertRaisesRegex(ValueError, "Unsafe DMM archive member"),
            ):
                extract_zip_sync(archive, target)

            self.assertFalse(target.exists())


class DmmDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_is_fixed_origin_and_bounded_during_transfer(self):
        class Content:
            async def read(self, _size):
                return b"12345"

        class Response:
            status = 200
            content = Content()

            def __init__(self):
                self.headers = {}

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            def get(self, _url, **kwargs):
                self.request_kwargs = kwargs
                return Response()

        session = Session()
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch(
                    "comet.services.dmm_ingester.aiohttp.ClientSession",
                    return_value=session,
                ) as session_factory,
                patch(
                    "comet.services.dmm_ingester.TEMP_DIR",
                    str(Path(directory) / "temporary"),
                ),
                patch("comet.services.dmm_ingester._MAX_DOWNLOAD_BYTES", 4),
            ):
                await DMMIngester()._ingest_cycle()

        self.assertFalse(session.request_kwargs["allow_redirects"])
        self.assertEqual(
            session.request_kwargs["headers"]["Accept-Encoding"],
            "identity",
        )
        self.assertNotIn("auto_decompress", session_factory.call_args.kwargs)
