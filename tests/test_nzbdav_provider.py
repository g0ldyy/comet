import json
import unittest
import uuid
from unittest.mock import patch

import aiohttp

from comet.core.sources import MAX_SIGNED_BIGINT
from comet.playback.base import Readiness
from comet.playback.providers.nzbdav import (
    NzbDavError,
    NzbDavProvider,
    parse_webdav_entries,
)


class _Response:
    def __init__(self, status, payload=None):
        self.status, self.payload = status, payload or {}
        self._body = (
            self.payload
            if isinstance(self.payload, bytes)
            else json.dumps(self.payload).encode()
        )
        self._read = False
        self.content = self
        self.headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(self._body)),
        }

    async def __aenter__(self):
        self._read = False
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self):
        return self.payload

    async def read(self, _maximum=None):
        if self._read:
            return b""
        self._read = True
        return self._body


class _Session:
    def __init__(self, sab, dav, post=None, history=None, queue=None):
        self.sab = _Response(sab, {"version": "4.5.3"})
        self.categories = _Response(
            sab,
            {"categories": ["movies", "tv"]},
        )
        self.dav, self.post_response = _Response(dav), post
        self.history_response = history
        self.queue_response = queue or _Response(200, {"queue": {"slots": []}})

    def get(self, *_args, **kwargs):
        mode = (kwargs.get("params") or {}).get("mode")
        if mode == "history":
            return self.history_response
        if mode == "queue":
            return self.queue_response
        if mode == "get_cats":
            return self.categories
        return self.sab

    def request(self, *_args, **_kwargs):
        return self.dav

    def post(self, *_args, **_kwargs):
        return self.post_response


class NzbDavProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_categories_are_transport_specific_and_opaque(self):
        options = {
            "movieCategory": "films",
            "seriesCategory": "shows",
        }

        self.assertEqual(NzbDavProvider.category_for(options, (0,)), "films")
        self.assertEqual(
            NzbDavProvider.category_for(options, (1, 1, 2)),
            "shows",
        )
        self.assertEqual(
            NzbDavProvider.category_for({"movieCategory": "films 4K"}, (0,)),
            "films 4K",
        )
        self.assertEqual(
            NzbDavProvider.category_for({"movieCategory": "é" * 65}, (0,)),
            "é" * 65,
        )

    async def test_requires_independent_sab_and_webdav_validation(self):
        options = {
            "internalBaseUrl": "https://bridge.example",
            "sabApiKey": "key",
            "webdavUsername": "user",
            "webdavPassword": "password",
        }
        incomplete = await NzbDavProvider(_Session(200, 404)).validate_config(options)
        valid = await NzbDavProvider(_Session(200, 207)).validate_config(options)

        self.assertEqual(incomplete.code, "validation_incomplete")
        self.assertEqual(valid.readiness, Readiness.REQUIRES_PREPARE)

    async def test_validation_explains_an_unallowlisted_internal_http_origin(self):
        options = {
            "internalBaseUrl": "http://nzbdav:3000/",
            "sabApiKey": "key",
            "webdavUsername": "user",
            "webdavPassword": "password",
        }

        status = await NzbDavProvider(_Session(200, 207)).validate_config(options)

        self.assertEqual(status.code, "private_upstream_origin_required")

    async def test_validation_distinguishes_an_invalid_url_from_missing_fields(self):
        options = {
            "internalBaseUrl": "nzbdav:3000",
            "sabApiKey": "key",
            "webdavUsername": "user",
            "webdavPassword": "password",
        }

        status = await NzbDavProvider(_Session(200, 207)).validate_config(options)

        self.assertEqual(status.code, "configuration_invalid")

    async def test_validation_requires_both_configured_sab_categories(self):
        options = {
            "internalBaseUrl": "https://bridge.example",
            "streamBaseUrl": "https://media.example",
            "sabApiKey": "key",
            "webdavUsername": "user",
            "webdavPassword": "password",
            "movieCategory": "films",
            "seriesCategory": "shows",
        }
        session = _Session(200, 207)
        session.categories = _Response(200, {"categories": ["films"]})

        status = await NzbDavProvider(session).validate_config(options)

        self.assertEqual(status.code, "validation_incomplete")

    async def test_validation_ignores_unconsumed_category_entries(self):
        options = {
            "internalBaseUrl": "https://bridge.example",
            "sabApiKey": "key",
            "webdavUsername": "user",
            "webdavPassword": "password",
        }
        session = _Session(200, 207)
        session.categories = _Response(
            200,
            {"categories": [{}, "movies", "tv"]},
        )

        status = await NzbDavProvider(session).validate_config(options)

        self.assertEqual(status.readiness, Readiness.REQUIRES_PREPARE)

    async def test_validation_stops_after_sab_authentication_rejection(self):
        class Session:
            def __init__(self):
                self.requests = 0

            def get(self, *_args, **_kwargs):
                self.requests += 1
                return _Response(401)

            def request(self, *_args, **_kwargs):
                raise AssertionError("WebDAV credentials must not be sent")

        session = Session()
        status = await NzbDavProvider(session).validate_config(
            {
                "internalBaseUrl": "https://bridge.example",
                "sabApiKey": "key",
                "webdavUsername": "user",
                "webdavPassword": "password",
            }
        )

        self.assertEqual(status.code, "credentials_rejected")
        self.assertEqual(session.requests, 1)

    async def test_submits_only_a_deterministically_named_brokered_file(self):
        artifact_sha256 = "a" * 64
        job_id = str(uuid.uuid4())
        provider = NzbDavProvider(
            _Session(200, 207, _Response(200, {"status": True, "nzo_ids": [job_id]}))
        )
        options = {
            "internalBaseUrl": "https://bridge.example",
            "sabApiKey": "key",
            "webdavUsername": "user",
            "webdavPassword": "password",
        }

        submitted = await provider.submit_artifact(
            options, b"<nzb />", artifact_sha256, "movies"
        )

        self.assertEqual(submitted, job_id)

    async def test_submission_rejects_response_without_job_id(self):
        provider = NzbDavProvider(
            _Session(
                200,
                207,
                _Response(
                    200,
                    b'{"status":true,"nzo_ids":[]}',
                ),
            )
        )
        options = {
            "internalBaseUrl": "https://bridge.example",
            "streamBaseUrl": "https://media.example",
            "sabApiKey": "key",
            "webdavUsername": "user",
            "webdavPassword": "password",
        }

        with self.assertRaisesRegex(RuntimeError, "invalid_response"):
            await provider.submit_artifact(
                options,
                b"<nzb />",
                "a" * 64,
                "movies",
            )

    async def test_submission_rejects_oversized_nzb_before_network(self):
        provider = NzbDavProvider(object())
        options = {
            "internalBaseUrl": "https://bridge.example",
            "sabApiKey": "key",
            "webdavUsername": "user",
            "webdavPassword": "password",
        }

        with (
            patch(
                "comet.playback.providers.nzbdav.MAX_NZB_METADATA_BYTES",
                3,
            ),
            self.assertRaisesRegex(ValueError, "artifact submission"),
        ):
            await provider.submit_artifact(
                options,
                b"1234",
                "a" * 64,
                "movies",
            )

    async def test_submission_distinguishes_rejection_from_ambiguous_outcomes(self):
        options = {
            "internalBaseUrl": "https://bridge.example",
            "sabApiKey": "key",
            "webdavUsername": "user",
            "webdavPassword": "password",
        }
        cases = (
            (400, True, "submission_failed"),
            (401, True, "credentials_rejected"),
            (429, True, "rate_limited"),
            (302, False, "submission_failed"),
            (503, False, "unavailable"),
        )

        for status, rejected, code in cases:
            with self.subTest(status=status):
                provider = NzbDavProvider(_Session(200, 207, _Response(status)))
                with self.assertRaisesRegex(NzbDavError, code) as raised:
                    await provider.submit_artifact(
                        options,
                        b"<nzb />",
                        "a" * 64,
                        "movies",
                    )
                self.assertEqual(raised.exception.mutation_rejected, rejected)

    async def test_submission_hides_transport_exception_context(self):
        class Session:
            def post(self, *_args, **_kwargs):
                raise aiohttp.ClientConnectionError("secret-bearing request")

        provider = NzbDavProvider(Session())
        options = {
            "internalBaseUrl": "https://bridge.example",
            "sabApiKey": "key",
            "webdavUsername": "user",
            "webdavPassword": "password",
        }

        with self.assertRaisesRegex(NzbDavError, "unavailable") as raised:
            await provider.submit_artifact(
                options,
                b"<nzb />",
                "a" * 64,
                "movies",
            )
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)

    async def test_poll_accepts_only_the_expected_completed_job_name(self):
        artifact_sha256 = "a" * 64
        job_id = str(uuid.uuid4())
        history = _Response(
            200,
            {
                "history": {
                    "slots": [
                        {
                            "nzo_id": job_id,
                            "category": "movies",
                            "status": "Completed",
                            "nzb_name": f"comet-{artifact_sha256}.nzb",
                        }
                    ]
                }
            },
        )
        provider = NzbDavProvider(_Session(200, 207, history=history))
        options = {
            "internalBaseUrl": "https://bridge.example",
            "sabApiKey": "key",
            "webdavUsername": "user",
            "webdavPassword": "password",
        }

        result = await provider.poll_artifact(
            options, job_id, artifact_sha256, "movies"
        )

        self.assertEqual(result.verified_name, f"comet-{artifact_sha256}")
        self.assertEqual(result.status.readiness, Readiness.REQUIRES_PREPARE)
        self.assertTrue(result.observed)

    async def test_poll_stops_at_the_exact_active_queue_job(self):
        artifact_sha256 = "a" * 64
        job_id = str(uuid.uuid4())
        queue = _Response(
            200,
            {
                "queue": {
                    "slots": [
                        {
                            "nzo_id": job_id,
                            "cat": "movies",
                            "filename": f"comet-{artifact_sha256}.nzb",
                        }
                    ]
                }
            },
        )
        provider = NzbDavProvider(_Session(200, 207, queue=queue, history=None))

        result = await provider.poll_artifact(
            {
                "internalBaseUrl": "https://bridge.example",
                "sabApiKey": "key",
                "webdavUsername": "user",
                "webdavPassword": "password",
            },
            job_id,
            artifact_sha256,
            "movies",
        )

        self.assertEqual(result.status.readiness, Readiness.PREPARING)
        self.assertTrue(result.observed)

    async def test_poll_reports_an_exact_job_absent_from_queue_and_history(self):
        job_id = str(uuid.uuid4())
        provider = NzbDavProvider(
            _Session(
                200,
                207,
                history=_Response(200, {"history": {"slots": []}}),
            )
        )

        result = await provider.poll_artifact(
            {
                "internalBaseUrl": "https://bridge.example",
                "sabApiKey": "key",
                "webdavUsername": "user",
                "webdavPassword": "password",
            },
            job_id,
            "a" * 64,
            "movies",
        )

        self.assertEqual(result.status.readiness, Readiness.TERMINAL_FAILURE)
        self.assertEqual(result.status.code, "remote_item_missing")
        self.assertFalse(result.observed)

    async def test_poll_does_not_treat_an_unavailable_queue_as_absence(self):
        job_id = str(uuid.uuid4())
        provider = NzbDavProvider(
            _Session(
                200,
                207,
                queue=_Response(503),
                history=None,
            )
        )

        with self.assertRaisesRegex(NzbDavError, "unavailable") as raised:
            await provider.poll_artifact(
                {
                    "internalBaseUrl": "https://bridge.example",
                    "sabApiKey": "key",
                    "webdavUsername": "user",
                    "webdavPassword": "password",
                },
                job_id,
                "a" * 64,
                "movies",
            )

        self.assertTrue(raised.exception.retryable)

    async def test_poll_rejects_a_rebound_queue_job(self):
        job_id = str(uuid.uuid4())
        provider = NzbDavProvider(
            _Session(
                200,
                207,
                queue=_Response(
                    200,
                    {
                        "queue": {
                            "slots": [
                                {
                                    "nzo_id": job_id,
                                    "cat": "movies",
                                    "filename": "foreign.nzb",
                                }
                            ]
                        }
                    },
                ),
            )
        )

        result = await provider.poll_artifact(
            {
                "internalBaseUrl": "https://bridge.example",
                "sabApiKey": "key",
                "webdavUsername": "user",
                "webdavPassword": "password",
            },
            job_id,
            "a" * 64,
            "movies",
        )

        self.assertEqual(result.status.readiness, Readiness.TERMINAL_FAILURE)
        self.assertEqual(result.status.code, "job_mismatch")
        self.assertTrue(result.observed)

    async def test_poll_uses_the_current_native_category_field(self):
        job_id = str(uuid.uuid4())
        provider = NzbDavProvider(
            _Session(
                200,
                207,
                queue=_Response(
                    200,
                    {
                        "queue": {
                            "slots": [
                                {
                                    "nzo_id": job_id,
                                    "cat": "movies",
                                    "category": "foreign",
                                    "filename": "comet-" + "a" * 64 + ".nzb",
                                }
                            ]
                        }
                    },
                ),
            )
        )

        result = await provider.poll_artifact(
            {
                "internalBaseUrl": "https://bridge.example",
                "sabApiKey": "key",
                "webdavUsername": "user",
                "webdavPassword": "password",
            },
            job_id,
            "a" * 64,
            "movies",
        )

        self.assertEqual(result.status.readiness, Readiness.PREPARING)

    async def test_poll_recognizes_documented_history_preparation_states(self):
        job_id = str(uuid.uuid4())
        history = _Response(
            200,
            {
                "history": {
                    "slots": [
                        {
                            "nzo_id": job_id,
                            "category": "movies",
                            "status": "Queued",
                            "nzb_name": "comet-" + "a" * 64 + ".nzb",
                        }
                    ]
                }
            },
        )
        provider = NzbDavProvider(_Session(200, 207, history=history))

        result = await provider.poll_artifact(
            {
                "internalBaseUrl": "https://bridge.example",
                "sabApiKey": "key",
                "webdavUsername": "user",
                "webdavPassword": "password",
            },
            job_id,
            "a" * 64,
            "movies",
        )

        self.assertEqual(result.status.readiness, Readiness.PREPARING)

    async def test_poll_does_not_promote_future_failed_prefixes(self):
        job_id = str(uuid.uuid4())
        history = _Response(
            200,
            {
                "history": {
                    "slots": [
                        {
                            "nzo_id": job_id,
                            "category": "movies",
                            "status": "FailedButRetrying",
                            "nzb_name": "comet-" + "a" * 64 + ".nzb",
                        }
                    ]
                }
            },
        )
        provider = NzbDavProvider(_Session(200, 207, history=history))

        result = await provider.poll_artifact(
            {
                "internalBaseUrl": "https://bridge.example",
                "sabApiKey": "key",
                "webdavUsername": "user",
                "webdavPassword": "password",
            },
            job_id,
            "a" * 64,
            "movies",
        )

        self.assertEqual(result.status.readiness, Readiness.PREPARING)

    async def test_reconciliation_rejects_a_matching_invalid_queue_uuid(self):
        job_id = str(uuid.uuid4())

        class Session:
            def get(self, *_args, **kwargs):
                mode = kwargs["params"]["mode"]
                if mode == "queue":
                    return _Response(
                        200,
                        {
                            "queue": {
                                "slots": [
                                    {
                                        "nzo_id": "invalid",
                                        "filename": "comet-" + "a" * 64 + ".nzb",
                                        "cat": "movies",
                                    },
                                    {
                                        "nzo_id": job_id,
                                        "filename": "comet-" + "a" * 64 + ".nzb",
                                        "cat": "movies",
                                    },
                                ]
                            }
                        },
                    )
                return _Response(200, {"history": {"slots": []}})

        provider = NzbDavProvider(Session())
        with self.assertRaisesRegex(NzbDavError, "invalid_response") as raised:
            await provider.reconcile_artifact(
                {
                    "internalBaseUrl": "https://bridge.example",
                    "sabApiKey": "key",
                    "webdavUsername": "user",
                    "webdavPassword": "password",
                },
                "a" * 64,
                "movies",
            )

        self.assertTrue(raised.exception.retryable)

    async def test_reconciliation_deterministically_adopts_one_live_match(self):
        job_ids = [str(uuid.uuid4()), str(uuid.uuid4())]

        class Session:
            def get(self, *_args, **kwargs):
                mode = kwargs["params"]["mode"]
                if mode == "queue":
                    return _Response(
                        200,
                        {
                            "queue": {
                                "slots": [
                                    {
                                        "nzo_id": job_id,
                                        "filename": "comet-" + "a" * 64 + ".nzb",
                                        "category": "movies",
                                    }
                                    for job_id in job_ids
                                ]
                            }
                        },
                    )
                return _Response(200, {"history": {"slots": []}})

        provider = NzbDavProvider(Session())
        result = await provider.reconcile_artifact(
            {
                "internalBaseUrl": "https://bridge.example",
                "sabApiKey": "key",
                "webdavUsername": "user",
                "webdavPassword": "password",
            },
            "a" * 64,
            "movies",
        )
        self.assertIn(result.job_id, job_ids)
        self.assertEqual(result.status, "queued")

    async def test_reconciliation_paginates_before_proving_absence(self):
        job_id = str(uuid.uuid4())
        calls = []

        class Session:
            def get(self, *_args, **kwargs):
                params = kwargs["params"]
                calls.append((params["mode"], params["start"]))
                if params["mode"] == "history":
                    return _Response(200, {"history": {"slots": []}})
                if params["start"] == "0":
                    return _Response(
                        200,
                        {
                            "queue": {
                                "slots": [
                                    {
                                        "nzo_id": str(uuid.uuid4()),
                                        "filename": f"foreign-{index}.nzb",
                                        "cat": "movies",
                                    }
                                    for index in range(200)
                                ]
                            }
                        },
                    )
                return _Response(
                    200,
                    {
                        "queue": {
                            "slots": [
                                {
                                    "nzo_id": job_id,
                                    "filename": "comet-" + "a" * 64 + ".nzb",
                                    "cat": "movies",
                                }
                            ]
                        }
                    },
                )

        result = await NzbDavProvider(Session()).reconcile_artifact(
            {
                "internalBaseUrl": "https://bridge.example",
                "sabApiKey": "key",
                "webdavUsername": "user",
                "webdavPassword": "password",
            },
            "a" * 64,
            "movies",
        )

        self.assertEqual(result.job_id, job_id)
        self.assertEqual(
            calls,
            [("queue", "0"), ("queue", "200"), ("history", "0")],
        )

    async def test_reconciliation_seals_an_exact_failed_history_job(self):
        job_id = str(uuid.uuid4())

        class Session:
            def get(self, *_args, **kwargs):
                if kwargs["params"]["mode"] == "queue":
                    return _Response(200, {"queue": {"slots": []}})
                return _Response(
                    200,
                    {
                        "history": {
                            "slots": [
                                {
                                    "nzo_id": job_id,
                                    "nzb_name": "comet-" + "a" * 64 + ".nzb",
                                    "category": "movies",
                                    "status": "Failed",
                                    "completed": 10,
                                }
                            ]
                        }
                    },
                )

        result = await NzbDavProvider(Session()).reconcile_artifact(
            {
                "internalBaseUrl": "https://bridge.example",
                "sabApiKey": "key",
                "webdavUsername": "user",
                "webdavPassword": "password",
            },
            "a" * 64,
            "movies",
        )

        self.assertEqual((result.job_id, result.status), (job_id, "failed"))

    async def test_reconciliation_does_not_promote_future_failed_prefixes(self):
        job_id = str(uuid.uuid4())

        class Session:
            def get(self, *_args, **kwargs):
                if kwargs["params"]["mode"] == "queue":
                    return _Response(200, {"queue": {"slots": []}})
                return _Response(
                    200,
                    {
                        "history": {
                            "slots": [
                                {
                                    "nzo_id": job_id,
                                    "nzb_name": "comet-" + "a" * 64 + ".nzb",
                                    "category": "movies",
                                    "status": "FailedButRetrying",
                                }
                            ]
                        }
                    },
                )

        result = await NzbDavProvider(Session()).reconcile_artifact(
            {
                "internalBaseUrl": "https://bridge.example",
                "sabApiKey": "key",
                "webdavUsername": "user",
                "webdavPassword": "password",
            },
            "a" * 64,
            "movies",
        )

        self.assertEqual((result.job_id, result.status), (job_id, "queued"))

    async def test_reconciliation_fails_closed_at_the_scan_bound(self):
        calls = []

        class Session:
            def get(self, *_args, **kwargs):
                params = kwargs["params"]
                calls.append((params["mode"], params["start"]))
                return _Response(
                    200,
                    {
                        params["mode"]: {
                            "slots": [
                                {
                                    "nzo_id": str(uuid.uuid4()),
                                    "filename": "foreign.nzb",
                                    "cat": "movies",
                                }
                                for _index in range(2)
                            ]
                        }
                    },
                )

        with (
            patch("comet.playback.providers.nzbdav._SAB_PAGE_SIZE", 2),
            patch(
                "comet.playback.providers.nzbdav._MAX_SAB_RECONCILIATION_ITEMS",
                4,
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "nzbdav_reconciliation_unavailable",
            ),
        ):
            await NzbDavProvider(Session()).reconcile_artifact(
                {
                    "internalBaseUrl": "https://bridge.example",
                    "sabApiKey": "key",
                    "webdavUsername": "user",
                    "webdavPassword": "password",
                },
                "a" * 64,
                "movies",
            )

        self.assertEqual(calls, [("queue", "0"), ("queue", "2")])

    def test_verified_content_root_is_confined_to_the_exact_completed_job(self):
        options = {
            "internalBaseUrl": "https://bridge.example",
            "sabApiKey": "key",
            "webdavUsername": "user",
            "webdavPassword": "password",
        }
        artifact_sha256 = "a" * 64

        root = NzbDavProvider.verified_content_root(
            options, f"comet-{artifact_sha256}", "comet"
        )

        self.assertEqual(
            root,
            f"https://bridge.example/content/comet/comet-{artifact_sha256}",
        )
        with self.assertRaises(ValueError):
            NzbDavProvider.verified_content_root(options, "../outside", "comet")

    def test_http_internal_origin_requires_the_operator_allowlist(self):
        options = {
            "internalBaseUrl": "http://bridge.local:8080",
            "sabApiKey": "key",
            "webdavUsername": "user",
            "webdavPassword": "password",
        }

        self.assertIsNone(NzbDavProvider._options(options))
        with patch(
            "comet.playback.providers.nzbdav.settings.USENET_PRIVATE_UPSTREAM_ORIGINS",
            ["http://bridge.local:8080"],
        ):
            self.assertIsNotNone(NzbDavProvider._options(options))
        self.assertIsNone(
            NzbDavProvider._options(
                {**options, "internalBaseUrl": "https://bridge.local:99999"}
            )
        )
        self.assertIsNone(
            NzbDavProvider._options(
                {**options, "internalBaseUrl": "https://bridge.local:0"}
            )
        )

    def test_webdav_codec_accepts_only_files_below_the_completed_job(self):
        root = "https://bridge.example/content/comet/comet-" + "a" * 64
        response = b"""<?xml version='1.0'?>
        <d:multistatus xmlns:d='DAV:'>
          <d:response><d:href>/content/comet/comet-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/video.mkv</d:href>
            <d:propstat><d:prop><d:getcontentlength>42</d:getcontentlength></d:prop></d:propstat>
          </d:response>
        </d:multistatus>"""

        entries = parse_webdav_entries(root, response)

        self.assertEqual(entries[0].relative_path, "video.mkv")
        self.assertEqual(entries[0].byte_size, 42)
        self.assertEqual(
            parse_webdav_entries(
                root,
                response.replace(
                    b"<d:href>/content/",
                    b"<d:href>http://localhost:8080/content/",
                ),
            ),
            entries,
        )
        self.assertEqual(
            parse_webdav_entries(
                root,
                response.replace(
                    b">42</d:getcontentlength>", b">0042</d:getcontentlength>"
                ),
            )[0].byte_size,
            42,
        )
        extended = response.replace(
            b"<d:propstat><d:prop><d:getcontentlength>42</d:getcontentlength>"
            b"</d:prop></d:propstat>",
            b"<d:propstat><d:status>HTTP/3 201 Created</d:status>"
            b"<d:prop><d:getcontentlength>42</d:getcontentlength></d:prop>"
            b"</d:propstat><d:propstat><d:status>HTTP/3 299 Extension</d:status>"
            b"<d:prop><d:displayname>video</d:displayname></d:prop></d:propstat>",
        )
        self.assertEqual(parse_webdav_entries(root, extended), entries)
        duplicate = response.replace(
            b"</d:multistatus>",
            response[
                response.index(b"<d:response>") : response.index(b"</d:response>")
                + len(b"</d:response>")
            ]
            + b"</d:multistatus>",
        )
        self.assertEqual(parse_webdav_entries(root, duplicate), entries)
        self.assertEqual(
            parse_webdav_entries(
                root,
                response.replace(
                    b"?>",
                    b'?><!DOCTYPE multistatus SYSTEM "dav.dtd">',
                    1,
                ),
            ),
            entries,
        )
        with self.assertRaises(ValueError):
            parse_webdav_entries(
                root,
                response.replace(
                    b">42</d:getcontentlength>",
                    (
                        b">"
                        + str(MAX_SIGNED_BIGINT + 1).encode()
                        + b"</d:getcontentlength>"
                    ),
                ),
            )
        with self.assertRaises(ValueError):
            parse_webdav_entries(
                root,
                response.replace(b"/content/comet/", b"/outside/"),
            )
        with self.assertRaises(ValueError):
            parse_webdav_entries(
                root, response.replace(b"video.mkv", b"folder%5Cvideo.mkv")
            )
        with self.assertRaises(ValueError):
            parse_webdav_entries(root, response.replace(b"video.mkv", b"video%ZZ.mkv"))
        with self.assertRaises(ValueError):
            parse_webdav_entries(
                root,
                response.replace(
                    b"<d:propstat><d:prop>",
                    (
                        b"<d:propstat>"
                        b"<d:status>HTTP/1.1 404 Not Found</d:status>"
                        b"<d:prop>"
                    ),
                ),
            )

    async def test_completed_file_uses_the_canonical_video_selector(self):
        artifact_sha256 = "a" * 64
        body = (
            b"<d:multistatus xmlns:d='DAV:'><d:response><d:href>/content/comet/comet-"
            + artifact_sha256.encode()
            + b"/video.mkv</d:href><d:propstat><d:prop><d:getcontentlength>42</d:getcontentlength></d:prop></d:propstat></d:response></d:multistatus>"
        )
        body = body.replace(
            b"</d:multistatus>",
            (
                b"<d:response><d:href>/content/comet/comet-"
                + artifact_sha256.encode()
                + b"/sample.mkv</d:href><d:propstat><d:prop><d:getcontentlength>1</d:getcontentlength></d:prop></d:propstat></d:response></d:multistatus>"
            ),
        )
        options = {
            "internalBaseUrl": "https://bridge.example",
            "sabApiKey": "key",
            "webdavUsername": "user",
            "webdavPassword": "password",
        }
        provider = NzbDavProvider(_Session(200, 207))
        provider._session.dav = _Response(207, body)

        selected = await provider.completed_file(
            options,
            f"comet-{artifact_sha256}",
            "comet",
            (0,),
        )

        self.assertEqual(selected.relative_path, "video.mkv")

    async def test_propfind_requires_complete_exactly_framed_xml(self):
        artifact_sha256 = "a" * 64
        body = (
            b"<d:multistatus xmlns:d='DAV:'><d:response><d:href>/content/comet/comet-"
            + artifact_sha256.encode()
            + b"/video.mkv</d:href><d:propstat><d:prop><d:getcontentlength>42</d:getcontentlength></d:prop></d:propstat></d:response></d:multistatus>"
        )

        class Content:
            def __init__(self):
                self.chunks = [body[:20], body[20:], b"trailing"]

            async def read(self, _maximum):
                return self.chunks.pop(0) if self.chunks else b""

        response = _Response(207, body)
        response.content = Content()
        response.headers["Content-Length"] = str(len(body))
        provider = NzbDavProvider(_Session(200, 207))
        provider._session.dav = response

        with self.assertRaisesRegex(RuntimeError, "invalid_response"):
            await provider.completed_file(
                {
                    "internalBaseUrl": "https://bridge.example",
                    "sabApiKey": "key",
                    "webdavUsername": "user",
                    "webdavPassword": "password",
                },
                f"comet-{artifact_sha256}",
                "comet",
                (0,),
            )

    async def test_propfind_reads_the_actual_body_regardless_of_encoding_header(self):
        response = _Response(207, b"encoded")
        response.headers["Content-Encoding"] = "gzip"
        provider = NzbDavProvider(_Session(200, 207))
        provider._session.dav = response

        self.assertEqual(
            await provider._propfind(
                {
                    "internalBaseUrl": "https://bridge.example",
                    "sabApiKey": "key",
                    "webdavUsername": "user",
                    "webdavPassword": "password",
                },
                "https://bridge.example/content/comet/comet-" + "a" * 64,
                "infinity",
            ),
            (207, b"encoded"),
        )

    async def test_completed_file_falls_back_to_bounded_depth_one_walk(self):
        artifact_sha256 = "a" * 64
        root_path = f"/content/comet/comet-{artifact_sha256}"
        root_body = f"""<d:multistatus xmlns:d='DAV:'>
          <d:response><d:href>{root_path}/folder/</d:href>
            <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat>
          </d:response>
        </d:multistatus>""".encode()
        folder_body = f"""<d:multistatus xmlns:d='DAV:'>
          <d:response><d:href>{root_path}/folder/</d:href>
            <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat>
          </d:response>
          <d:response><d:href>{root_path}/folder/Example.S01E02.mkv</d:href>
            <d:propstat><d:prop><d:getcontentlength>42</d:getcontentlength></d:prop></d:propstat>
          </d:response>
        </d:multistatus>""".encode()

        class Session:
            def __init__(self):
                self.responses = [
                    _Response(405),
                    _Response(207, root_body),
                    _Response(207, folder_body),
                ]
                self.requests = []

            def request(self, _method, url, **kwargs):
                self.requests.append((url, kwargs["headers"]["Depth"]))
                return self.responses.pop(0)

        session = Session()
        provider = NzbDavProvider(session)
        selected = await provider.completed_file(
            {
                "internalBaseUrl": "https://bridge.example",
                "sabApiKey": "key",
                "webdavUsername": "user",
                "webdavPassword": "password",
            },
            f"comet-{artifact_sha256}",
            "comet",
            (1, 1, 2),
        )

        self.assertEqual(selected.relative_path, "folder/Example.S01E02.mkv")
        self.assertEqual(
            [depth for _url, depth in session.requests],
            ["infinity", "1", "1"],
        )

    def test_completed_file_url_escapes_each_verified_relative_path_segment(self):
        options = {
            "internalBaseUrl": "https://bridge.example",
            "sabApiKey": "key",
            "webdavUsername": "user",
            "webdavPassword": "password",
        }
        artifact_sha256 = "a" * 64

        url = NzbDavProvider.completed_file_url(
            options, f"comet-{artifact_sha256}", "comet", "folder name/video #1.mkv"
        )

        self.assertEqual(
            url,
            f"https://bridge.example/content/comet/comet-{artifact_sha256}/folder%20name/video%20%231.mkv",
        )
        with self.assertRaises(ValueError):
            NzbDavProvider.completed_file_url(
                options, f"comet-{artifact_sha256}", "comet", "folder\\video.mkv"
            )

    def test_completed_target_returns_an_authenticated_direct_url(self):
        options = {
            "internalBaseUrl": "https://bridge.example",
            "streamBaseUrl": "https://media.example",
            "sabApiKey": "key",
            "webdavUsername": "user",
            "webdavPassword": "password",
        }
        target = NzbDavProvider.direct_download_url(
            options,
            "comet-" + "a" * 64,
            "movies",
            "folder/video.mkv",
        )

        self.assertEqual(
            target,
            "https://user:password@media.example/content/movies/comet-"
            + "a" * 64
            + "/folder/video.mkv",
        )

    def test_rejects_control_characters_in_bridge_credentials(self):
        self.assertIsNone(
            NzbDavProvider._options(
                {
                    "internalBaseUrl": "https://bridge.example",
                    "sabApiKey": "key",
                    "webdavUsername": "user\nname",
                    "webdavPassword": "password",
                }
            )
        )
        self.assertIsNone(
            NzbDavProvider._options(
                {
                    "internalBaseUrl": "https://bridge.example",
                    "sabApiKey": "clé",
                    "webdavUsername": "user",
                    "webdavPassword": "password",
                }
            )
        )
        self.assertIsNone(
            NzbDavProvider._options(
                {
                    "internalBaseUrl": "https://bridge.example",
                    "sabApiKey": "key",
                    "webdavUsername": "user:name",
                    "webdavPassword": "password",
                }
            )
        )
        for invalid in (
            "user\x7f",
            "é" * 513,
            "\ud800",
        ):
            with self.subTest(invalid=ascii(invalid)):
                self.assertIsNone(
                    NzbDavProvider._options(
                        {
                            "internalBaseUrl": "https://bridge.example",
                            "sabApiKey": "key",
                            "webdavUsername": invalid,
                            "webdavPassword": "password",
                        }
                    )
                )
        self.assertIsNone(
            NzbDavProvider._options(
                {
                    "internalBaseUrl": "https://@bridge.example",
                    "sabApiKey": "key",
                    "webdavUsername": "user",
                    "webdavPassword": "password",
                }
            )
        )
