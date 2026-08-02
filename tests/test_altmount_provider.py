import hashlib
import json
import unittest
from unittest.mock import patch
from urllib.parse import quote

from comet.playback.altmount_contract import (
    valid_altmount_last_modified,
    valid_altmount_strong_etag,
    valid_altmount_virtual_path,
)
from comet.playback.base import Readiness
from comet.playback.providers.altmount import (
    AltMountError,
    AltMountProvider,
    AltMountSelectedFile,
)


class _Response:
    def __init__(self, status, payload=None):
        self.status, self.payload = status, payload or {}
        self._body = json.dumps(self.payload).encode()
        self.content = self
        self.headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(self._body)),
        }

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def read(self, _maximum=None):
        body, self._body = self._body, b""
        return body


class _Session:
    def __init__(self, status, payload=None):
        self.response = _Response(status, payload)

    def post(self, *_args, **_kwargs):
        return self.response


class AltMountProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_native_preflight_requires_the_documented_no_file_response(
        self,
    ):
        config = {"internalBaseUrl": "https://altmount.example", "apiKey": "key"}
        valid = await AltMountProvider(_Session(422)).validate_config(config)
        rejected = await AltMountProvider(_Session(401)).validate_config(config)
        forbidden = await AltMountProvider(_Session(403)).validate_config(config)
        unexpected = await AltMountProvider(_Session(402)).validate_config(config)

        self.assertEqual(valid.readiness, Readiness.REQUIRES_PREPARE)
        self.assertEqual(rejected.code, "credentials_rejected")
        self.assertEqual(forbidden.code, "credentials_rejected")
        self.assertEqual(unexpected.code, "validation_incomplete")

    async def test_native_api_is_required(self):
        config = {
            "internalBaseUrl": "https://altmount.example",
            "apiKey": "key",
        }

        status = await AltMountProvider(_Session(404)).validate_config(config)

        self.assertEqual(status.readiness, Readiness.TERMINAL_FAILURE)
        self.assertEqual(status.code, "native_api_required")

    async def test_native_upload_requires_a_valid_streamable_queue_response(self):
        key = "key"
        config = {"internalBaseUrl": "https://altmount.example", "apiKey": key}
        url = (
            "https://altmount.example/api/files/stream?path=folder%2Fvideo.mkv&download_key="
            + hashlib.sha256(key.encode()).hexdigest()
        )
        provider = AltMountProvider(
            _Session(
                200,
                {
                    "streams": [{"url": url, "future": "ignored"}],
                    "_queue_item_id": "ignored",
                    "_queue_status": {"ignored": True},
                    "_cached": "ignored",
                    "_future": "ignored",
                },
            )
        )

        result = await provider.submit_artifact(
            config,
            b"<nzb />",
            "a" * 64,
            (0,),
        )

        self.assertEqual(result.files[0].virtual_path, "folder/video.mkv")
        self.assertEqual(provider.stream_url(config, result.files[0].virtual_path), url)

    async def test_native_timeout_and_rate_limit_remain_retryable(self):
        config = {"internalBaseUrl": "https://altmount.example", "apiKey": "key"}

        for status, code in (
            (408, "altmount_unavailable"),
            (429, "altmount_rate_limited"),
        ):
            with self.subTest(status=status):
                with self.assertRaisesRegex(RuntimeError, code):
                    await AltMountProvider(_Session(status)).submit_artifact(
                        config,
                        b"<nzb />",
                        "a" * 64,
                        (0,),
                    )

    async def test_native_upload_rejects_oversized_nzb_before_network(self):
        config = {"internalBaseUrl": "https://altmount.example", "apiKey": "key"}

        with (
            patch(
                "comet.playback.providers.altmount.MAX_NZB_METADATA_BYTES",
                3,
            ),
            self.assertRaisesRegex(ValueError, "artifact submission"),
        ):
            await AltMountProvider(object()).submit_artifact(
                config,
                b"1234",
                "a" * 64,
                (0,),
            )

    def test_episode_filename_preserves_the_exact_selection(self):
        self.assertEqual(
            AltMountProvider.filename_for("a" * 64, (1, 1, 2)),
            "Comet.S01E02." + "a" * 64 + ".nzb",
        )

    def test_file_selection_requires_one_unambiguous_video_without_fetching_it(self):
        files = tuple(
            type(
                "File",
                (),
                {
                    "virtual_path": path,
                    "title": path,
                    "name": path,
                },
            )()
            for path in ("sample.mkv", "movie.mkv")
        )
        result = type("Result", (), {"files": files})()
        with self.assertRaisesRegex(
            RuntimeError,
            "altmount_file_selection_ambiguous",
        ):
            AltMountProvider.select_file(result, (0,))

        one_file = type("Result", (), {"files": (files[1],)})()
        self.assertEqual(
            AltMountProvider.select_file(one_file, (0,)),
            AltMountSelectedFile("movie.mkv"),
        )

    def test_http_origins_require_an_exact_operator_allowlist(self):
        config = {"internalBaseUrl": "http://altmount.local:8080", "apiKey": "key"}

        self.assertIsNone(AltMountProvider._options(config))
        with patch(
            "comet.playback.providers.altmount.settings.USENET_PRIVATE_UPSTREAM_ORIGINS",
            ["http://altmount.local:8080"],
        ):
            self.assertIsNotNone(AltMountProvider._options(config))
        self.assertIsNone(
            AltMountProvider._options(
                {
                    "internalBaseUrl": "https://altmount.local:99999",
                    "apiKey": "key",
                }
            )
        )
        self.assertIsNone(
            AltMountProvider._options(
                {"internalBaseUrl": "https://altmount.local:0", "apiKey": "key"}
            )
        )
        self.assertIsNone(
            AltMountProvider._options(
                {"internalBaseUrl": "https://[invalid", "apiKey": "key"}
            )
        )

    def test_ignores_provider_download_key_format(self):
        hostile = (
            "https://altmount.example/api/files/stream?path=folder%2Fvideo.mkv"
            "&download_key=" + quote("é" * 64)
        )

        self.assertEqual(
            AltMountProvider._stream_paths([{"url": hostile}]),
            ("folder/video.mkv",),
        )

    def test_duplicate_provider_streams_collapse_to_one_file(self):
        stream = {
            "url": ("https://altmount.example/api/files/stream?path=folder%2Fvideo.mkv")
        }

        self.assertEqual(
            AltMountProvider._stream_paths([stream, stream]),
            ("folder/video.mkv",),
        )

    def test_rejects_invalid_provider_streams(self):
        self.assertIsNone(
            AltMountProvider._options(
                {"internalBaseUrl": "https://altmount.example", "apiKey": "bad\nkey"}
            )
        )
        with self.assertRaisesRegex(AltMountError, "invalid_response"):
            AltMountProvider._stream_paths(
                [
                    {"path": "folder\\video.mkv"},
                    {"foreign": "metadata"},
                    {
                        "url": (
                            "https://altmount.example/api/files/stream"
                            "?path=folder%ZZvideo.mkv&download_key="
                            + hashlib.sha256(b"key").hexdigest()
                        )
                    },
                ],
            )

    def test_categories_are_opaque_utf8_values(self):
        category = "films/" + "é" * 80

        self.assertEqual(
            AltMountProvider.category_for({"category": category}),
            category,
        )

    def test_configuration_and_representation_text_use_exact_wire_domains(self):
        self.assertIsNotNone(
            AltMountProvider._options(
                {
                    "internalBaseUrl": "https://altmount.example",
                    "apiKey": "clé",
                }
            )
        )
        self.assertIsNone(
            AltMountProvider._options(
                {
                    "internalBaseUrl": "https://@altmount.example",
                    "apiKey": "key",
                }
            )
        )
        self.assertFalse(valid_altmount_virtual_path("é" * 1025))
        self.assertFalse(valid_altmount_virtual_path("video\x7f.mkv"))
        self.assertTrue(valid_altmount_strong_etag('"revision"'))
        self.assertFalse(valid_altmount_strong_etag("revision"))
        self.assertFalse(valid_altmount_strong_etag('W/"revision"'))
        self.assertTrue(valid_altmount_last_modified("Wed, 21 Oct 2015 07:28:00 GMT"))
        self.assertFalse(
            valid_altmount_last_modified("Wed, 21 Oct 2015 07:28:00 GMT\x7f")
        )
