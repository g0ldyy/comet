import json
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp

from comet.core.sources import MAX_SIGNED_BIGINT
from comet.playback.base import Actionability, Readiness
from comet.playback.providers.torbox_usenet import (
    TorBoxUsenetError,
    TorBoxUsenetFile,
    TorBoxUsenetItem,
    TorBoxUsenetProvider,
    _create_rate_limit,
    _created_item,
    _download_target,
    _file,
    _item,
    cache_hash_from_alias,
    cache_hashes_from_manifest,
)


class TorBoxUsenetProviderTests(unittest.TestCase):
    def test_status_never_promotes_unknown_remote_state(self):
        unknown = TorBoxUsenetProvider.status(TorBoxUsenetItem(1, "future-state"))
        grabbing = TorBoxUsenetProvider.status(TorBoxUsenetItem(1, "grabbing"))
        ready = TorBoxUsenetProvider.status(
            TorBoxUsenetItem(
                1,
                "downloaded",
                files=(
                    type(
                        "File", (), {"file_id": 1, "name": "Example.mkv", "size": 1}
                    )(),
                ),
                download_finished=True,
                download_present=True,
                post_processing=False,
            )
        )
        failed = TorBoxUsenetProvider.status(TorBoxUsenetItem(1, "failed"))

        self.assertEqual(
            (unknown.readiness, unknown.actionability),
            (Readiness.UNKNOWN, Actionability.REMOTE_PREPARE),
        )
        self.assertEqual(
            grabbing.readiness,
            Readiness.UNKNOWN,
        )
        self.assertEqual(
            (ready.readiness, ready.actionability),
            (Readiness.READY, Actionability.SERVER_ON_DEMAND),
        )
        self.assertEqual(
            (failed.readiness, failed.actionability),
            (Readiness.TERMINAL_FAILURE, Actionability.NONE),
        )

    def test_downloaded_status_without_completion_evidence_is_not_ready(self):
        status = TorBoxUsenetProvider.status(TorBoxUsenetItem(1, "downloaded"))

        self.assertEqual(status.readiness, Readiness.UNKNOWN)

    def test_terminal_status_wins_over_activity_but_ready_files_remain_usable(self):
        active = TorBoxUsenetProvider.status(TorBoxUsenetItem(1, "failed", active=True))
        available = TorBoxUsenetProvider.status(
            TorBoxUsenetItem(
                1,
                "failed",
                files=(TorBoxUsenetFile(7, "Example.mkv", 42),),
                download_finished=True,
                download_present=True,
                active=False,
            )
        )

        self.assertEqual(active.readiness, Readiness.TERMINAL_FAILURE)
        self.assertEqual(available.readiness, Readiness.READY)

    def test_selection_chooses_the_largest_matching_video(self):
        item = TorBoxUsenetItem(
            1,
            "downloaded",
            files=(
                type(
                    "File", (), {"file_id": 1, "name": "Example.S01E02.mkv", "size": 10}
                )(),
                type("File", (), {"file_id": 2, "name": "Sample.mkv", "size": 1})(),
                type("File", (), {"file_id": 3, "name": "Example.nfo", "size": 20})(),
            ),
            download_finished=True,
            download_present=True,
            post_processing=False,
        )

        self.assertEqual(TorBoxUsenetProvider.select_file(item, (1, 1, 2)).file_id, 1)
        self.assertEqual(TorBoxUsenetProvider.select_file(item, (0,)).file_id, 1)

    def test_descriptor_accepts_only_brokered_nzb_artifacts(self):
        self.assertEqual(
            TorBoxUsenetProvider.descriptor.accepted_locator_kinds,
            frozenset({"nzb_artifact"}),
        )

    def test_remote_file_sizes_use_the_signed_bigint_domain(self):
        oversized = MAX_SIGNED_BIGINT + 1

        with self.assertRaisesRegex(TorBoxUsenetError, "invalid_response"):
            _file({"id": 1, "name": "Movie.mkv", "size": oversized})

    def test_remote_identifiers_are_bounded_and_hashes_stay_opaque(self):
        oversized = MAX_SIGNED_BIGINT + 1

        with self.assertRaisesRegex(TorBoxUsenetError, "invalid_response"):
            _item({"id": oversized, "download_state": "queued", "files": []})
        with self.assertRaisesRegex(TorBoxUsenetError, "invalid_response"):
            _created_item(oversized)
        self.assertEqual(
            _created_item(
                {"usenetdownload_id": 1, "hash": "Provider:Alias"}
            ).content_hash,
            "Provider:Alias",
        )

    def test_remote_statuses_preserve_future_values(self):
        self.assertEqual(
            _item(
                {
                    "id": 1,
                    "download_state": "failed (processing)",
                    "files": [],
                }
            ).status,
            "failed",
        )
        self.assertEqual(
            _item(
                {
                    "id": 1,
                    "download_state": "Future Upstream State",
                    "files": [],
                }
            ).status,
            "Future Upstream State",
        )
        self.assertEqual(
            _item(
                {
                    "id": 1,
                    "download_state": "failed: incomplete",
                    "files": [],
                }
            ).status,
            "failed: incomplete",
        )

    def test_item_rejects_malformed_consumed_hash_metadata(self):
        with self.assertRaisesRegex(TorBoxUsenetError, "invalid_response"):
            _item(
                {
                    "id": 1,
                    "download_state": "downloading",
                    "hash": [],
                    "alternative_hashes": [
                        {},
                        "Provider:Alias",
                        "Provider:Alias",
                    ],
                    "files": [],
                }
            )

    def test_item_rejects_malformed_consumed_fields(self):
        with self.assertRaisesRegex(TorBoxUsenetError, "invalid_response"):
            _item(
                {
                    "id": 1,
                    "download_state": "downloading",
                    "download_finished": "yes",
                    "files": [
                        {"id": 7, "name": "Movie.mkv", "size": 42},
                        {"id": 7, "name": "duplicate.mkv", "size": 50},
                        {"id": "invalid", "name": "ignored.mkv", "size": 1},
                    ],
                }
            )


class _Response:
    def __init__(self, status, payload, *, headers=None):
        self.status = status
        self._payload = payload
        self._body = (
            payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        )
        self._read = False
        self.content = self
        self.headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(self._body)),
            **(headers or {}),
        }

    async def __aenter__(self):
        self._read = False
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self):
        return self._payload

    async def read(self, _maximum):
        if self._read:
            return b""
        self._read = True
        return self._body


class _Session:
    def __init__(self, response):
        self._response = response

    def get(self, *_args, **_kwargs):
        return self._response


class _RecordingSession(_Session):
    def post(self, *_args, **kwargs):
        self.post_kwargs = kwargs
        return self._response


class _RequestSession(_Session):
    def get(self, *_args, **kwargs):
        self.get_kwargs = kwargs
        return self._response


class _ValidationSession:
    def __init__(self, *responses):
        self._responses = iter(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self._responses)


class _FailingSession:
    def post(self, *_args, **_kwargs):
        raise aiohttp.ClientConnectionError


class _FailingRequestSession:
    def get(self, *_args, **_kwargs):
        raise aiohttp.ClientConnectionError(
            "https://api.torbox.app/requestdl?token=account-key"
        )


class TorBoxUsenetValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_codec_preserves_provider_urls(self):
        url = "https://weur.tb-cdn.io/file?token=cdn-signature&expires=1780000000"
        target = _download_target(url)

        self.assertEqual(target.url, url)

    async def test_download_codec_does_not_require_a_guessed_signature_shape(self):
        for url in (
            "https://weur.tb-cdn.io/file",
            "https://weur.tb-cdn.io/file?token=&expires=",
        ):
            with self.subTest(url=url):
                self.assertEqual(
                    _download_target(url).url,
                    url,
                )

    async def test_download_codec_treats_provider_tokens_as_opaque(self):
        for url in (
            "https://cdn.example/file?token=signed&expires=1780000000",
            "https://weur.tb-cdn.io/file?token=account-key&expires=1780000000",
        ):
            with self.subTest(url=url):
                self.assertEqual(_download_target(url).url, url)

    def test_api_key_is_bounded_and_control_free(self):
        for value in ("key\x7f", "key\nvalue"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "API key"):
                    TorBoxUsenetProvider(None, value)
        TorBoxUsenetProvider(None, "clé avec espace")

    async def test_validation_distinguishes_rejected_and_transient_credentials(self):
        rejected = TorBoxUsenetProvider(_Session(_Response(401, {})), "key")
        unavailable = TorBoxUsenetProvider(_Session(_Response(503, {})), "key")
        valid_session = _ValidationSession(
            _Response(
                200,
                {
                    "success": True,
                    "data": {
                        "plan": 2,
                        "is_subscribed": False,
                        "premium_expires_at": "2027-01-01T00:00:00Z",
                    },
                },
            ),
            _Response(200, {"success": True, "data": []}),
        )
        valid = TorBoxUsenetProvider(valid_session, "key")

        rejected_status = await rejected.validate_config({})
        unavailable_status = await unavailable.validate_config({})
        valid_status = await valid.validate_config({})

        self.assertEqual(rejected_status.code, "api_key_rejected")
        self.assertEqual(rejected_status.readiness, Readiness.TERMINAL_FAILURE)
        self.assertEqual(unavailable_status.code, "validation_unavailable")
        self.assertEqual(unavailable_status.readiness, Readiness.RETRYABLE_FAILURE)
        self.assertEqual(valid_status.readiness, Readiness.UNKNOWN)
        self.assertEqual(len(valid_session.calls), 2)
        user_url, user_request = valid_session.calls[0]
        list_url, list_request = valid_session.calls[1]
        self.assertEqual(user_url, "https://api.torbox.app/v1/api/user/me")
        self.assertEqual(list_url, "https://api.torbox.app/v1/api/usenet/mylist")
        self.assertEqual(
            list_request["params"],
            {"offset": "0", "limit": "1", "bypass_cache": "false"},
        )
        for request in (user_request, list_request):
            self.assertFalse(request["allow_redirects"])
            self.assertEqual(request["timeout"].total, 10)
            self.assertEqual(request["headers"]["Accept-Encoding"], "identity")

    async def test_validation_proves_the_configured_key_and_usenet_plan(self):
        session = _ValidationSession(
            _Response(200, {"success": True, "data": {}}),
            _Response(
                400,
                {
                    "success": False,
                    "error": "PLAN_RESTRICTED_FEATURE",
                    "data": None,
                },
            ),
        )
        provider = TorBoxUsenetProvider(session, "constructor-key")

        status = await provider.validate_config({"apiKey": "configured-key"})

        self.assertEqual(status.readiness, Readiness.TERMINAL_FAILURE)
        self.assertEqual(status.code, "usenet_plan_required")
        self.assertTrue(
            all(
                request["headers"]["Authorization"] == "Bearer configured-key"
                for _, request in session.calls
            )
        )

    async def test_validation_accepts_an_extended_success_envelope(self):
        session = _ValidationSession(
            _Response(200, {"data": {}}),
            _Response(200, {"data": []}),
        )
        provider = TorBoxUsenetProvider(session, "key")

        status = await provider.validate_config({})

        self.assertEqual(status.readiness, Readiness.UNKNOWN)

    async def test_validation_ignores_unconsumed_data_shapes(self):
        session = _ValidationSession(
            _Response(
                200,
                b'{"success":true,"data":{},"data":[]}',
            ),
            _Response(200, {"success": True, "data": {}}),
        )
        provider = TorBoxUsenetProvider(session, "key")

        status = await provider.validate_config({})

        self.assertEqual(status.readiness, Readiness.UNKNOWN)
        self.assertEqual(len(session.calls), 2)

    def test_manifest_hashes_are_parser_owned_ordered_and_deduplicated(self):
        self.assertEqual(
            cache_hashes_from_manifest(
                [
                    {"first_segment_md5": "b" * 32},
                    {"first_segment_md5": "a" * 32},
                    {"first_segment_md5": "b" * 32},
                ]
            ),
            ("b" * 32, "a" * 32),
        )
        self.assertEqual(
            cache_hashes_from_manifest([{"postings": []}]),
            (),
        )
        with self.assertRaisesRegex(ValueError, "manifest"):
            cache_hashes_from_manifest([{"first_segment_md5": "A" * 32}])
        self.assertEqual(cache_hash_from_alias("c" * 32), ("c" * 32,))
        self.assertEqual(cache_hash_from_alias("opaque"), ())

    async def test_create_codec_uses_the_current_identifier(self):
        session = _RecordingSession(
            _Response(
                201,
                {
                    "success": True,
                    "data": {
                        "usenetdownload_id": 7,
                        "id": 8,
                    },
                },
            )
        )
        provider = TorBoxUsenetProvider(session, "key")

        self.assertEqual(
            (await provider.submit_artifact(b"<nzb/>")).usenet_id,
            7,
        )
        timeout = session.post_kwargs["timeout"]
        self.assertEqual(timeout.total, 110)
        self.assertIsNone(timeout.connect)
        self.assertIsNone(timeout.sock_read)

    async def test_current_list_shape_proves_inactive_completed_item_ready(self):
        provider = TorBoxUsenetProvider(
            _RequestSession(
                _Response(
                    200,
                    {
                        "success": True,
                        "data": {
                            "id": 7,
                            "download_state": "completed",
                            "active": False,
                            "download_finished": True,
                            "download_present": True,
                            "hash": "a" * 32,
                            "files": [
                                {
                                    "id": 3,
                                    "name": "Example.mkv",
                                    "size": 42,
                                }
                            ],
                        },
                    },
                )
            ),
            "key",
        )

        item = await provider.get_item(7)

        self.assertEqual(item.status, "completed")
        self.assertFalse(item.active)
        self.assertEqual(item.content_hash, "a" * 32)
        self.assertEqual(
            provider.status(item).readiness,
            Readiness.READY,
        )

    async def test_item_lookup_ignores_unrelated_rows(self):
        expected = {
            "id": 7,
            "download_state": "completed",
            "active": False,
            "download_finished": True,
            "download_present": True,
            "files": [{"id": 3, "name": "Example.mkv", "size": 42}],
        }
        for additional in ({"invalid": True}, {**expected, "id": 8}):
            provider = TorBoxUsenetProvider(
                _RequestSession(
                    _Response(
                        200,
                        {"success": True, "data": [expected, additional]},
                    )
                ),
                "key",
            )
            with self.subTest(additional=additional):
                self.assertEqual((await provider.get_item(7)).usenet_id, 7)

    async def test_library_lookup_reuses_only_an_exact_account_hash(self):
        session = _RequestSession(
            _Response(
                200,
                {
                    "success": True,
                    "data": [
                        {
                            "id": 7,
                            "download_state": "downloading",
                            "hash": "b" * 32,
                            "files": [],
                        },
                        {
                            "id": 8,
                            "download_state": "downloading",
                            "hash": "a" * 32,
                            "files": [],
                        },
                    ],
                },
            )
        )
        provider = TorBoxUsenetProvider(session, "key")

        item = await provider.find_existing(("b" * 32,))

        self.assertIsNotNone(item)
        self.assertEqual(item.usenet_id, 7)
        self.assertEqual(
            session.get_kwargs["params"],
            {
                "offset": "0",
                "limit": "1000",
                "bypass_cache": "true",
            },
        )
        self.assertFalse(session.get_kwargs["allow_redirects"])

    async def test_library_lookup_reuses_a_torbox_alternative_hash(self):
        provider = TorBoxUsenetProvider(
            _RequestSession(
                _Response(
                    200,
                    {
                        "success": True,
                        "data": [
                            {
                                "id": 7,
                                "download_state": "completed",
                                "active": False,
                                "download_finished": True,
                                "download_present": True,
                                "hash": "a" * 32,
                                "alternative_hashes": ["B" * 32, "provider:v2:alias"],
                                "files": [{"id": 3, "name": "Example.mkv", "size": 42}],
                            }
                        ],
                    },
                )
            ),
            "key",
        )

        item = await provider.find_existing(("b" * 32,))

        self.assertIsNotNone(item)
        self.assertEqual(item.usenet_id, 7)
        self.assertEqual(
            item.alternative_hashes,
            ("b" * 32, "provider:v2:alias"),
        )

    async def test_library_lookup_does_not_guess_from_name_and_size(self):
        provider = TorBoxUsenetProvider(
            _RequestSession(
                _Response(
                    200,
                    {
                        "success": True,
                        "data": [
                            {
                                "id": 7,
                                "name": "Exact.Release.Name",
                                "size": 42,
                                "download_state": "completed",
                                "active": False,
                                "download_finished": True,
                                "download_present": True,
                                "hash": "a" * 32,
                                "alternative_hashes": [],
                                "files": [
                                    {
                                        "id": 3,
                                        "name": "Exact.Release.Name.mkv",
                                        "size": 42,
                                    }
                                ],
                            }
                        ],
                    },
                )
            ),
            "key",
        )

        item = await provider.find_existing(("b" * 32,))

        self.assertIsNone(item)

    async def test_library_lookup_never_adopts_a_failed_exact_hash(self):
        provider = TorBoxUsenetProvider(
            _RequestSession(
                _Response(
                    200,
                    {
                        "success": True,
                        "data": [
                            {
                                "id": 7,
                                "download_state": "failed",
                                "hash": "b" * 32,
                                "files": [],
                            }
                        ],
                    },
                )
            ),
            "key",
        )

        self.assertIsNone(await provider.find_existing(("b" * 32,)))

    async def test_library_lookup_paginates_before_proving_absence(self):
        first_page = [
            {
                "id": index,
                "download_state": "downloading",
                "hash": "a" * 32,
                "name": "Metadata Match" if index == 0 else "Other",
                "size": 42,
                "files": [],
            }
            for index in range(1000)
        ]
        session = _ValidationSession(
            _Response(200, {"success": True, "data": first_page}),
            _Response(
                200,
                {
                    "success": True,
                    "data": [
                        {
                            "id": 999,
                            "download_state": "downloading",
                            "hash": "a" * 32,
                            "files": [],
                        },
                        {
                            "id": 1000,
                            "download_state": "downloading",
                            "hash": "b" * 32,
                            "files": [],
                        },
                    ],
                },
            ),
        )
        provider = TorBoxUsenetProvider(session, "key")

        item = await provider.find_existing(("b" * 32,))

        self.assertEqual(item.usenet_id, 1000)
        self.assertEqual(
            [request["params"]["offset"] for _, request in session.calls],
            ["0", "1000"],
        )

    async def test_library_lookup_rejects_an_oversized_page(self):
        page = [
            {
                "id": index,
                "download_state": "downloading",
                "hash": "a" * 32,
                "files": [],
            }
            for index in range(1001)
        ]
        provider = TorBoxUsenetProvider(
            _RequestSession(_Response(200, {"success": True, "data": page})),
            "key",
        )

        with self.assertRaisesRegex(TorBoxUsenetError, "invalid_response"):
            await provider.find_existing(("b" * 32,))

    async def test_owned_cleanup_uses_exact_control_body_and_endpoint_budget(self):
        session = _RecordingSession(_Response(200, {"success": True, "data": None}))
        permit = type("Permit", (), {"used": 2})()
        governor = type(
            "Governor",
            (),
            {
                "acquire_window": AsyncMock(return_value=permit),
                "tighten_window": AsyncMock(),
            },
        )()
        provider = TorBoxUsenetProvider(
            session,
            "key",
            governor=governor,
            governor_scope=b"a" * 32,
        )

        await provider.delete_owned(17)

        self.assertEqual(
            session.post_kwargs["json"],
            {"usenet_id": 17, "operation": "delete", "all": False},
        )
        self.assertFalse(session.post_kwargs["allow_redirects"])
        self.assertEqual(session.post_kwargs["headers"]["Accept-Encoding"], "identity")
        governor.acquire_window.assert_awaited_once_with(
            b"a" * 32,
            "torbox_api:usenet_control",
            limit=300,
            window_seconds=60,
        )
        governor.tighten_window.assert_not_awaited()

    async def test_owned_cleanup_never_accepts_a_false_success_envelope(self):
        provider = TorBoxUsenetProvider(
            _RecordingSession(
                _Response(
                    200,
                    {
                        "success": False,
                        "error": "DOWNLOAD_NOT_FOUND",
                        "data": None,
                    },
                )
            ),
            "key",
        )

        with self.assertRaisesRegex(RuntimeError, "torbox_invalid_response"):
            await provider.delete_owned(17)

    async def test_owned_cleanup_accepts_empty_success(self):
        provider = TorBoxUsenetProvider(
            _RecordingSession(_Response(204, None)),
            "key",
        )

        await provider.delete_owned(17)

    async def test_owned_cleanup_treats_exact_missing_response_as_complete(self):
        session = _RecordingSession(
            _Response(
                404,
                {
                    "success": False,
                    "error": "DOWNLOAD_NOT_FOUND",
                    "data": None,
                },
            )
        )
        provider = TorBoxUsenetProvider(
            session,
            "key",
        )

        await provider.delete_owned(17)

        self.assertEqual(
            session.post_kwargs["json"],
            {"usenet_id": 17, "operation": "delete", "all": False},
        )

    async def test_artifact_submission_rejects_oversized_documents_and_preserves_name(
        self,
    ):
        session = _RecordingSession(_Response(200, {"success": True, "data": 7}))
        provider = TorBoxUsenetProvider(session, "key")

        with (
            patch(
                "comet.playback.providers.torbox_usenet.MAX_NZB_METADATA_BYTES",
                3,
            ),
            self.assertRaisesRegex(ValueError, "NZB document"),
        ):
            await provider.submit_artifact(b"1234")
        name = 'Dune: Part Two / "IMAX"'
        await provider.submit_artifact(b"1", name=name)

        submitted_name = next(
            value
            for parameters, _headers, value in session.post_kwargs["data"]._fields
            if parameters["name"] == "name"
        )
        self.assertEqual(submitted_name, name)

    async def test_artifact_creates_share_the_account_hourly_budget(self):
        response = _Response(
            200,
            {"success": True, "data": 7},
            headers={"X-RateLimit-Limit": "40"},
        )
        provider = TorBoxUsenetProvider(
            _RecordingSession(response),
            "key",
        )
        permit = type("Permit", (), {"used": 2})()
        governor = type(
            "Governor",
            (),
            {
                "acquire_window": AsyncMock(return_value=permit),
                "tighten_window": AsyncMock(),
            },
        )()
        scope = b"a" * 32

        await provider.submit_artifact(
            b"<nzb/>",
            governor=governor,
            governor_scope=scope,
        )
        await provider.submit_artifact(
            b"<nzb/>",
            governor=governor,
            governor_scope=scope,
        )

        self.assertEqual(
            [call.args[1] for call in governor.acquire_window.await_args_list],
            ["torbox_usenet_create", "torbox_usenet_create"],
        )
        self.assertTrue(
            all(
                call.args[0] == scope
                and call.kwargs == {"limit": 60, "window_seconds": 60 * 60}
                for call in governor.acquire_window.await_args_list
            )
        )
        self.assertEqual(
            [call.kwargs["limit"] for call in governor.tighten_window.await_args_list],
            [40, 40],
        )

    async def test_exhausted_create_budget_stops_before_network(self):
        session = _RecordingSession(_Response(200, {"success": True, "data": 7}))
        provider = TorBoxUsenetProvider(session, "key")
        governor = type(
            "Governor",
            (),
            {
                "acquire_window": AsyncMock(return_value=None),
                "tighten_window": AsyncMock(),
            },
        )()

        with self.assertRaisesRegex(
            RuntimeError,
            "torbox_create_rate_limited",
        ):
            await provider.submit_artifact(
                b"<nzb/>",
                governor=governor,
                governor_scope=b"a" * 32,
            )

        self.assertFalse(hasattr(session, "post_kwargs"))
        governor.tighten_window.assert_not_awaited()

    async def test_create_transport_failures_use_the_safe_provider_error(self):
        provider = TorBoxUsenetProvider(_FailingSession(), "key")

        with self.assertRaisesRegex(RuntimeError, "torbox_unavailable"):
            await provider.submit_artifact(b"<nzb/>")

    async def test_create_429_tightens_the_current_window(self):
        provider = TorBoxUsenetProvider(
            _RecordingSession(_Response(429, {})),
            "key",
        )
        permit = type("Permit", (), {"used": 7})()
        governor = type(
            "Governor",
            (),
            {
                "acquire_window": AsyncMock(return_value=permit),
                "tighten_window": AsyncMock(),
            },
        )()

        with self.assertRaisesRegex(
            RuntimeError,
            "torbox_create_rate_limited",
        ):
            await provider.submit_artifact(
                b"<nzb/>",
                governor=governor,
                governor_scope=b"a" * 32,
            )

        governor.tighten_window.assert_awaited_once_with(
            b"a" * 32,
            "torbox_usenet_create",
            limit=7,
            window_seconds=60 * 60,
        )

    async def test_exhausted_account_budget_stops_before_network(self):
        session = _RequestSession(_Response(200, {"success": True, "data": {}}))
        governor = type(
            "Governor",
            (),
            {
                "acquire_window": AsyncMock(return_value=None),
                "tighten_window": AsyncMock(),
            },
        )()
        scope = b"a" * 32
        provider = TorBoxUsenetProvider(
            session,
            "key",
            governor=governor,
            governor_scope=scope,
        )

        with self.assertRaisesRegex(RuntimeError, "torbox_rate_limited"):
            await provider.get_item(7)

        self.assertFalse(hasattr(session, "get_kwargs"))
        governor.acquire_window.assert_awaited_once_with(
            scope,
            "torbox_api:usenet_mylist",
            limit=300,
            window_seconds=60,
        )
        governor.tighten_window.assert_not_awaited()

    async def test_api_429_closes_the_shared_account_window(self):
        session = _RequestSession(_Response(429, {}))
        permit = type("Permit", (), {"used": 7})()
        governor = type(
            "Governor",
            (),
            {
                "acquire_window": AsyncMock(return_value=permit),
                "tighten_window": AsyncMock(),
            },
        )()
        scope = b"a" * 32
        provider = TorBoxUsenetProvider(
            session,
            "key",
            governor=governor,
            governor_scope=scope,
        )

        with self.assertRaisesRegex(RuntimeError, "torbox_unavailable"):
            await provider.get_item(7)

        governor.tighten_window.assert_awaited_once_with(
            scope,
            "torbox_api:usenet_mylist",
            limit=7,
            window_seconds=60,
        )

    def test_create_rate_limit_ignores_pathologically_long_numbers(self):
        self.assertIsNone(_create_rate_limit({"X-RateLimit-Limit": "9" * 5000}))

    def test_only_public_ipv4_addresses_are_sent_to_torbox(self):
        self.assertIsNone(TorBoxUsenetProvider(None, "key", "10.0.0.1")._public_ipv4())
        self.assertIsNone(
            TorBoxUsenetProvider(None, "key", "2606:4700:4700::1111")._public_ipv4()
        )
        self.assertEqual(
            TorBoxUsenetProvider(None, "key", "8.8.8.8")._public_ipv4(),
            "8.8.8.8",
        )

    async def test_download_link_rejects_url_userinfo(self):
        session = _RequestSession(
            _Response(200, {"success": True, "data": "https://key@cdn.example/file"})
        )
        provider = TorBoxUsenetProvider(session, "key")
        item = TorBoxUsenetItem(
            1,
            "downloaded",
            files=(TorBoxUsenetFile(1, "Movie.mkv", 1),),
            download_finished=True,
            download_present=True,
            post_processing=False,
        )

        with self.assertRaisesRegex(Exception, "invalid_response"):
            await provider.request_download(item.usenet_id, file_id=1)

    async def test_download_link_preserves_the_opaque_provider_token(self):
        url = "https://tb-cdn.example/dld/file?token=account-key"
        session = _RequestSession(_Response(200, {"success": True, "data": url}))
        provider = TorBoxUsenetProvider(session, "account-key")

        target = await provider.request_download(1, file_id=2)

        self.assertEqual(target.url, url)

    async def test_download_transport_failure_drops_the_secret_bearing_cause(self):
        provider = TorBoxUsenetProvider(
            _FailingRequestSession(),
            "account-key",
        )
        item = TorBoxUsenetItem(
            1,
            "downloaded",
            files=(TorBoxUsenetFile(1, "Movie.mkv", 1),),
            download_finished=True,
            download_present=True,
            post_processing=False,
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "torbox_unavailable",
        ) as raised:
            await provider.request_download(item.usenet_id, file_id=1)

        self.assertIsNone(raised.exception.__cause__)
        self.assertNotIn("account-key", str(raised.exception))
