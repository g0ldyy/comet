import base64
import json
import unittest
from unittest.mock import AsyncMock, patch

from comet.core.sources import MAX_SIGNED_BIGINT
from comet.playback.base import Actionability, Readiness
from comet.playback.providers.stremthru_newz import (
    StremThruNewzError,
    StremThruNewzProvider,
    _remote_file,
    _status,
    options,
)


class _JsonResponse:
    def __init__(
        self,
        status,
        payload,
        *,
        content_type="application/json",
        headers=None,
    ):
        self.status = status
        self._body = (
            payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        )
        self._read = False
        self.content = self
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(self._body)),
            **(headers or {}),
        }

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def read(self, _maximum):
        if self._read:
            return b""
        self._read = True
        return self._body


class StremThruNewzProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_statuses_remain_opaque(self):
        self.assertEqual(
            tuple(
                _status(status)
                for status in (
                    "cached",
                    "downloaded",
                    "queued",
                    "downloading",
                    "processing",
                    "failed",
                    "invalid",
                    "unknown",
                    "ready",
                    "failure_pending",
                )
            ),
            (
                "cached",
                "downloaded",
                "queued",
                "downloading",
                "processing",
                "failed",
                "invalid",
                "unknown",
                "ready",
                "failure_pending",
            ),
        )

    def test_remote_file_fields_follow_consumed_storage_bounds(self):
        remote_file = _remote_file(
            {
                "index": MAX_SIGNED_BIGINT,
                "name": "é" * 1024,
                "size": MAX_SIGNED_BIGINT,
                "link": "é" * 4096,
            }
        )
        self.assertEqual(remote_file.index, MAX_SIGNED_BIGINT)
        self.assertEqual(len(remote_file.name), 1024)

        with self.assertRaisesRegex(
            RuntimeError,
            "stremthru_invalid_response",
        ):
            _remote_file(
                {
                    "index": 0,
                    "name": "Movie.mkv",
                    "path": "Movie.mkv",
                    "size": MAX_SIGNED_BIGINT + 1,
                    "link": "locked",
                }
            )

    def test_options_normalize_raw_and_encoded_authorization(self):
        raw = {
            "baseUrl": "https://bridge.example/api/",
            "authToken": "user:pass",
        }
        encoded = {**raw, "authToken": base64.b64encode(b"user:pass").decode()}

        assert options(raw) == options(encoded)

    def test_options_reject_public_http(self):
        with self.assertRaises(ValueError):
            options({"baseUrl": "http://bridge.example", "authToken": "user:pass"})
        with patch(
            "comet.playback.providers.stremthru_newz.settings.USENET_PRIVATE_UPSTREAM_ORIGINS",
            ["http://bridge.example:80"],
        ):
            assert (
                options(
                    {"baseUrl": "http://bridge.example", "authToken": "user:pass"}
                ).base_url
                == "http://bridge.example"
            )
        with self.assertRaises(ValueError):
            options(
                {
                    "baseUrl": "https://bridge.example:99999",
                    "authToken": "user:pass",
                }
            )

    def test_options_reject_control_characters_before_url_normalization(self):
        with self.assertRaises(ValueError):
            options({"baseUrl": "https://bridge.example\n", "authToken": "user:pass"})
        with self.assertRaises(ValueError):
            options({"baseUrl": "https://@bridge.example", "authToken": "user:pass"})
        self.assertEqual(
            options(
                {"baseUrl": "https://bridge.example", "authToken": " user:pass"}
            ).authorization,
            "IHVzZXI6cGFzcw==",
        )

    async def test_unavailable_session_is_retryable_without_creating_a_job(self):
        provider = StremThruNewzProvider(
            None, {"baseUrl": "https://bridge.example", "authToken": "user:pass"}
        )

        with patch(
            "comet.usenet.provider_exports.settings.PUBLIC_BASE_URL",
            "https://comet.example",
        ):
            status = await provider.validate_config({})

        self.assertEqual(status.readiness, Readiness.RETRYABLE_FAILURE)
        self.assertEqual(status.actionability, Actionability.REMOTE_PREPARE)

    async def test_invalid_validation_payload_is_not_classified_as_retryable(self):
        class Session:
            def get(self, *_args, **_kwargs):
                return _JsonResponse(200, b"{")

        provider = StremThruNewzProvider(
            Session(), {"baseUrl": "https://bridge.example", "authToken": "user:pass"}
        )
        with patch(
            "comet.usenet.provider_exports.settings.PUBLIC_BASE_URL",
            "https://comet.example",
        ):
            status = await provider.validate_config({})

        self.assertEqual(status.readiness, Readiness.TERMINAL_FAILURE)
        self.assertEqual(status.code, "validation_failed")

    async def test_missing_operator_export_base_rejects_the_binding(self):
        provider = StremThruNewzProvider(
            None, {"baseUrl": "https://bridge.example", "authToken": "user:pass"}
        )

        with (
            patch("comet.usenet.provider_exports.settings.PUBLIC_BASE_URL", None),
            patch(
                "comet.usenet.provider_exports.settings.USENET_EXPORT_BASE_URL", None
            ),
        ):
            status = await provider.validate_config({})

        self.assertEqual(status.readiness, Readiness.TERMINAL_FAILURE)
        self.assertEqual(status.code, "nzb_export_base_url_required")

    async def test_native_user_with_empty_email_is_available(self):
        class Session:
            def get(self, *_args, **_kwargs):
                return _JsonResponse(
                    200,
                    {
                        "data": {
                            "id": "user",
                            "email": "",
                            "subscription_status": "premium",
                            "has_usenet": True,
                        }
                    },
                )

        provider = StremThruNewzProvider(
            Session(), {"baseUrl": "https://bridge.example", "authToken": "user:pass"}
        )
        with patch(
            "comet.usenet.provider_exports.settings.PUBLIC_BASE_URL",
            "https://comet.example",
        ):
            status = await provider.validate_config({})

        self.assertEqual(status.readiness, Readiness.REQUIRES_PREPARE)

    async def test_submission_accepts_provider_export_urls_with_queries(self):
        class Session:
            def __init__(self):
                self.requests = []

            def post(self, *_args, **kwargs):
                self.requests.append(kwargs)
                return _JsonResponse(
                    201,
                    {
                        "data": {
                            "id": "remote-1",
                            "hash": "hash-1",
                            "status": "queued",
                        }
                    },
                )

        session = Session()
        provider = StremThruNewzProvider(
            session, {"baseUrl": "https://bridge.example", "authToken": "user:pass"}
        )
        submission = await provider.submit_export(
            "https://comet.example/nzb/export/v1/nx1." + "a" * 32 + ".nzb"
        )

        self.assertEqual(submission.remote_id, "remote-1")
        self.assertEqual(submission.remote_hash, "hash-1")
        queried = await provider.submit_export(
            "https://comet.example/nzb/export/v1/nx1." + "a" * 32 + ".nzb?other=1"
        )
        self.assertEqual(queried.remote_id, "remote-1")
        for request in session.requests:
            timeout = request["timeout"]
            self.assertEqual(timeout.total, 110)
            self.assertIsNone(timeout.connect)
            self.assertIsNone(timeout.sock_read)

    async def test_submission_preserves_character_bounded_opaque_ids(self):
        class Session:
            def post(self, *_args, **_kwargs):
                return _JsonResponse(
                    201,
                    {
                        "data": {
                            "id": "é" * 512,
                            "hash": "hash-1",
                            "status": "queued",
                        }
                    },
                )

        submission = await StremThruNewzProvider(
            Session(),
            {"baseUrl": "https://bridge.example", "authToken": "user:pass"},
        ).submit_export("https://comet.example/export.nzb")

        self.assertEqual(submission.status, "queued")
        self.assertEqual(len(submission.remote_id), 512)

    async def test_shared_governor_wraps_network_io_but_not_local_json_decode(self):
        class Session:
            def post(self, *_args, **_kwargs):
                return _JsonResponse(
                    201,
                    {
                        "data": {
                            "id": "remote-1",
                            "hash": "hash-1",
                            "status": "queued",
                        }
                    },
                )

        lease = type("Lease", (), {"release": AsyncMock()})()
        governor = type(
            "Governor",
            (),
            {"acquire_concurrency": AsyncMock(return_value=lease)},
        )()
        provider = StremThruNewzProvider(
            Session(),
            {"baseUrl": "https://bridge.example", "authToken": "user:pass"},
            governor=governor,
            governor_scope=b"s" * 32,
        )

        def decode_after_release(body):
            lease.release.assert_awaited_once_with()
            return {
                "id": "remote-1",
                "hash": "hash-1",
                "status": "queued",
            }

        with patch(
            "comet.playback.providers.stremthru_newz.decode_provider_data",
            side_effect=decode_after_release,
        ):
            await provider.submit_export(
                "https://comet.example/nzb/export/v1/nx1." + "a" * 32 + ".nzb"
            )

        governor.acquire_concurrency.assert_awaited_once()
        call = governor.acquire_concurrency.await_args
        self.assertEqual(call.args, (b"s" * 32, "stremthru-control"))
        self.assertEqual(call.kwargs["limit"], 4)
        self.assertEqual(call.kwargs["lease_seconds"], 12)
        self.assertEqual(len(call.kwargs["owner_request_id"]), 32)

    async def test_shared_governor_rejects_before_network_when_account_is_busy(self):
        class Session:
            def post(self, *_args, **_kwargs):
                raise AssertionError("busy provider must not receive a request")

        governor = type(
            "Governor",
            (),
            {"acquire_concurrency": AsyncMock(return_value=None)},
        )()
        provider = StremThruNewzProvider(
            Session(),
            {"baseUrl": "https://bridge.example", "authToken": "user:pass"},
            governor=governor,
            governor_scope=b"s" * 32,
        )

        with self.assertRaises(StremThruNewzError) as raised:
            await provider.submit_export(
                "https://comet.example/nzb/export/v1/nx1." + "a" * 32 + ".nzb"
            )

        self.assertEqual(raised.exception.code, "stremthru_busy")
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.retry_after, 1)
        self.assertTrue(raised.exception.mutation_rejected)

    async def test_submission_accepts_normal_json_and_ignores_advisory_framing(self):
        responses = [
            _JsonResponse(
                201,
                b'{"data":{"id":"one","id":"two","hash":"hash","status":"queued"}}',
            ),
            _JsonResponse(
                201,
                {"data": {"id": "one", "hash": "hash", "status": "queued"}},
                headers={"Content-Length": str(2 * 1024 * 1024 + 1)},
            ),
            _JsonResponse(
                201,
                {
                    "data": {
                        "id": "one",
                        "hash": "hash",
                        "status": "queued",
                    },
                    "metadata": {},
                },
            ),
        ]

        class Session:
            def post(self, *_args, **_kwargs):
                return responses.pop(0)

        provider = StremThruNewzProvider(
            Session(),
            {"baseUrl": "https://bridge.example", "authToken": "user:pass"},
        )
        self.assertEqual(
            [
                (
                    await provider.submit_export(
                        "https://comet.example/nzb/export/v1/nx1." + "a" * 32 + ".nzb"
                    )
                ).remote_id
                for _case in range(3)
            ],
            ["two", "one", "one"],
        )

    async def test_submission_classifies_rate_limit_and_retry_after(self):
        class Session:
            def post(self, *_args, **_kwargs):
                return _JsonResponse(
                    429,
                    {"data": {}},
                    headers={"Retry-After": "999"},
                )

        provider = StremThruNewzProvider(
            Session(),
            {"baseUrl": "https://bridge.example", "authToken": "user:pass"},
        )
        with self.assertRaises(StremThruNewzError) as raised:
            await provider.submit_export(
                "https://comet.example/nzb/export/v1/nx1." + "a" * 32 + ".nzb"
            )

        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.retry_after, 300)
        self.assertTrue(raised.exception.mutation_rejected)

    async def test_exact_item_requires_the_ledger_identity_and_unique_file_indexes(
        self,
    ):
        class Session:
            def get(self, *_args, **_kwargs):
                return _JsonResponse(
                    200,
                    {
                        "data": {
                            "id": "remote-1",
                            "hash": "hash-1",
                            "name": "Example",
                            "size": 42,
                            "status": "provider-ready-v2",
                            "added_at": "2026-07-27T12:00:00Z",
                            "files": [
                                {
                                    "index": -1,
                                    "name": "Example.mkv",
                                    "path": "Example.mkv",
                                    "size": 42,
                                    "link": "locked-1",
                                }
                            ],
                        }
                    },
                )

        provider = StremThruNewzProvider(
            Session(), {"baseUrl": "https://bridge.example", "authToken": "user:pass"}
        )
        item = await provider.get_item("remote-1", "hash-1")

        self.assertFalse(item.terminal)
        self.assertEqual(item.status, "provider-ready-v2")
        self.assertEqual(item.files[0].index, 0)
        self.assertEqual(provider.select_file(item, (0,)).locked_link, "locked-1")
        with self.assertRaisesRegex(RuntimeError, "invalid_response"):
            await provider.get_item("remote-1", "wrong-hash")

    async def test_exact_item_ignores_unconsumed_listing_metadata(self):
        class Session:
            def get(self, *_args, **_kwargs):
                return _JsonResponse(
                    200,
                    {
                        "data": {
                            "id": "remote-1",
                            "hash": "hash-1",
                            "name": None,
                            "size": "unknown",
                            "status": "downloaded",
                            "added_at": "not-a-date",
                            "files": [
                                {
                                    "index": 2,
                                    "name": "Example.mkv",
                                    "path": "folder\\Example.mkv",
                                    "size": 42,
                                    "link": "locked-1",
                                    "video_hash": "not consumed",
                                }
                            ],
                        }
                    },
                )

        provider = StremThruNewzProvider(
            Session(), {"baseUrl": "https://bridge.example", "authToken": "user:pass"}
        )

        item = await provider.get_item("remote-1", "hash-1")

        self.assertEqual(provider.select_file(item, (0,)).locked_link, "locked-1")

    async def test_native_multi_file_sentinel_indexes_become_stable_ordinals(self):
        class Session:
            def get(self, *_args, **_kwargs):
                return _JsonResponse(
                    200,
                    {
                        "data": {
                            "id": "remote-1",
                            "hash": "hash-1",
                            "status": "downloaded",
                            "files": [
                                {
                                    "index": -1,
                                    "name": "Example.S01E01.mkv",
                                    "path": "/Example.S01E01.mkv",
                                    "size": 41,
                                    "link": "locked-1",
                                },
                                {
                                    "index": -1,
                                    "name": "Example.S01E02.mkv",
                                    "path": "/Example.S01E02.mkv",
                                    "size": 42,
                                    "link": "locked-2",
                                },
                            ],
                        }
                    },
                )

        provider = StremThruNewzProvider(
            Session(), {"baseUrl": "https://bridge.example", "authToken": "user:pass"}
        )
        item = await provider.get_item("remote-1", "hash-1")

        self.assertEqual(tuple(file.index for file in item.files), (0, 1))
        self.assertEqual(
            provider.select_file(item, (1, 1, 2)).locked_link,
            "locked-2",
        )

    async def test_link_generation_keeps_the_url_in_memory_and_enforces_origins(self):
        class Session:
            def post(self, *_args, **_kwargs):
                return _JsonResponse(
                    200,
                    {
                        "data": {
                            "link": (
                                "https://cdn.example:443/v0/store/newz/stream/"
                                "header.payload.signature/Example.mkv"
                            )
                        }
                    },
                )

        provider = StremThruNewzProvider(
            Session(),
            {
                "baseUrl": "https://bridge.example",
                "authToken": "user:pass",
                "allowedMediaOrigins": ["https://cdn.example"],
            },
        )

        generated = await provider.generate_link("locked-1")

        expected = (
            "https://cdn.example:443/v0/store/newz/stream/"
            "header.payload.signature/Example.mkv"
        )
        self.assertEqual(generated.url, expected)

    async def test_link_generation_accepts_provider_cdn_and_query_formats(self):
        responses = [
            _JsonResponse(200, {"data": {"link": "https://cdn.example/media/file"}}),
            _JsonResponse(
                200,
                {
                    "data": {
                        "link": (
                            "https://cdn.example/v0/store/newz/stream/"
                            "header.payload.signature/Example.mkv?token=other"
                        )
                    }
                },
            ),
        ]

        class Session:
            def post(self, *_args, **_kwargs):
                return responses.pop(0)

        provider = StremThruNewzProvider(
            Session(),
            {
                "baseUrl": "https://bridge.example",
                "authToken": "user:pass",
                "allowedMediaOrigins": ["https://cdn.example"],
            },
        )

        self.assertEqual(
            [(await provider.generate_link("locked-1")).url for _case in range(2)],
            [
                "https://cdn.example/media/file",
                (
                    "https://cdn.example/v0/store/newz/stream/"
                    "header.payload.signature/Example.mkv?token=other"
                ),
            ],
        )

    def test_selection_uses_the_canonical_video_selector(self):
        provider = StremThruNewzProvider(
            None, {"baseUrl": "https://bridge.example", "authToken": "user:pass"}
        )
        item = provider_item = type(
            "Item",
            (),
            {
                "status": "downloaded",
                "files": (
                    type(
                        "File",
                        (),
                        {"name": "Example.S01E02.1080p.mkv", "size": 10},
                    )(),
                    type(
                        "File",
                        (),
                        {"name": "Example.S01E03.1080p.mkv", "size": 9},
                    )(),
                    type("File", (), {"name": "Example.nfo", "size": 20})(),
                ),
            },
        )()

        self.assertIs(provider.select_file(item, (1, 1, 2)), provider_item.files[0])
        self.assertIs(provider.select_file(item, (0,)), provider_item.files[0])
        with self.assertRaisesRegex(RuntimeError, "ambiguous"):
            provider.select_file(item, (1, 1, 4))
