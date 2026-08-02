import unittest
from unittest.mock import AsyncMock, patch

import orjson

from comet.core.sources import TransportKind
from comet.discovery.adapters.easynews import (
    EasynewsSearchAccount,
    EasynewsSearchAdapter,
    EasynewsSearchError,
)
from comet.discovery.models import DiscoveryContext, MediaQuery
from comet.playback.base import Readiness

PROVIDER_ID = "11111111-1111-4111-8111-111111111111"
SOURCE_ID = "22222222-2222-4222-8222-222222222222"


class _Response:
    def __init__(self, status, payload, headers=None):
        self.status = status
        self.headers = headers or {}
        self.document = payload if isinstance(payload, bytes) else orjson.dumps(payload)
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def iter_chunked(self, size):
        for offset in range(0, len(self.document), size):
            yield self.document[offset : offset + size]


class _Session:
    def __init__(self, response):
        self.responses = (
            list(response) if isinstance(response, (list, tuple)) else [response]
        )
        self.calls = []

    def get(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.responses.pop(0)


class EasynewsDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_generated_nzb_uses_and_releases_account_scoped_capacity(self):
        lease = type("Lease", (), {"release": AsyncMock()})()
        governor = type(
            "Governor",
            (),
            {"acquire_concurrency": AsyncMock(return_value=lease)},
        )()
        adapter = EasynewsSearchAdapter(
            object(),
            EasynewsSearchAccount(
                "member",
                "secret",
                None,
                SOURCE_ID,
            ),
            governor=governor,
            governor_scope=b"a" * 32,
        )
        payload = {"account_configuration_id": SOURCE_ID, "hash": "hash"}

        with patch(
            "comet.discovery.adapters.easynews.generate_nzb",
            AsyncMock(return_value=b"<nzb/>"),
        ) as generate:
            document = await adapter.generate_nzb(payload)

        self.assertEqual(document, b"<nzb/>")
        self.assertEqual(
            governor.acquire_concurrency.await_args.args[:2],
            (b"a" * 32, "easynews_generate_nzb"),
        )
        self.assertEqual(
            governor.acquire_concurrency.await_args.kwargs["lease_seconds"],
            125,
        )
        generate.assert_awaited_once_with(
            adapter._session,
            payload,
            "member",
            "secret",
        )
        lease.release.assert_awaited_once()

    async def test_account_bound_rows_become_direct_http_locators(self):
        session = _Session(
            _Response(
                200,
                {
                    "dlFarm": "farm",
                    "dlPort": 443,
                    "data": [
                        {
                            "0": "item",
                            "10": "Example",
                            "11": ".mkv",
                            "rawSize": 42,
                        }
                    ],
                },
            )
        )
        adapter = EasynewsSearchAdapter(
            session,
            EasynewsSearchAccount("member", "secret", PROVIDER_ID),
        )

        result = await adapter.search(
            MediaQuery(
                "tt123",
                "series",
                season=2,
                episode=3,
                title_aliases=("  Example   Show  ",),
            ),
            DiscoveryContext(frozenset({"usenet"}), b"a" * 32),
        )

        self.assertEqual(result.coverage, frozenset({"usenet"}))
        candidate = result.candidates[0]
        self.assertEqual(candidate.transport, TransportKind.USENET)
        self.assertEqual(candidate.identities, ())
        self.assertEqual(
            candidate.locators[0].policy.exact_provider_configuration_id,
            PROVIDER_ID,
        )
        self.assertEqual(candidate.locators[0].download_port, "443")
        args, kwargs = session.calls[0]
        self.assertEqual(
            args,
            ("https://members.easynews.com/2.0/search/solr-search/advanced",),
        )
        self.assertEqual(
            kwargs["params"],
            {
                "gps": "Example Show S02E03",
                "pno": "1",
                "u": "1",
                "safeO": "0",
                "s1": "relevance",
                "s1d": "-",
                "fty[]": "VIDEO",
                "pby": "100",
                "sb": "1",
                "st": "adv",
                "sS": "3",
            },
        )
        self.assertEqual(kwargs["headers"]["Accept"], "application/json")
        self.assertEqual(kwargs["headers"]["Accept-Encoding"], "identity")
        self.assertTrue(kwargs["headers"]["Authorization"].startswith("Basic "))
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["timeout"].total, 8)
        self.assertEqual(kwargs["timeout"].connect, 3)
        self.assertEqual(kwargs["timeout"].sock_read, 5)

    async def test_compact_easynews_rows_use_response_download_coordinates(self):
        session = _Session(
            _Response(
                200,
                {
                    "dlFarm": "farm",
                    "dlPort": 443,
                    "data": [
                        {
                            "0": "post-hash",
                            "10": "Example.Movie.2026",
                            "11": ".mkv",
                            "rawSize": 42,
                            "sig": "signature",
                            "passwd": False,
                        }
                    ],
                },
            )
        )
        adapter = EasynewsSearchAdapter(
            session,
            EasynewsSearchAccount("member", "secret", PROVIDER_ID),
        )

        result = await adapter.search(
            MediaQuery(
                "tt123",
                "movie",
                title_aliases=("Example Movie",),
            ),
            DiscoveryContext(frozenset({"usenet"}), b"a" * 32),
        )

        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        self.assertEqual(candidate.title, "Example.Movie.2026")
        self.assertEqual(candidate.size, 42)
        locator = candidate.locators[0]
        self.assertEqual(locator.file_identifier, "post-hash")
        self.assertEqual(locator.content_hash, "post-hash")
        self.assertEqual(locator.item_identifier, "")
        self.assertEqual(locator.filename, "Example.Movie.2026")
        self.assertEqual(locator.extension, "mkv")
        self.assertEqual(locator.download_farm, "farm")
        self.assertEqual(locator.download_port, "443")
        self.assertIsNone(locator.signature)

    def test_compact_passworded_easynews_rows_are_rejected(self):
        adapter = EasynewsSearchAdapter(
            None,
            EasynewsSearchAccount("member", "secret", PROVIDER_ID),
        )
        payload = {
            "dlFarm": "farm",
            "dlPort": 443,
            "data": [
                {
                    "0": "post-hash",
                    "10": "Example.Movie.2026",
                    "11": ".mkv",
                    "rawSize": 42,
                    "passwd": True,
                }
            ],
        }

        row = adapter._rows(payload)[0]

        self.assertIsNone(
            adapter._candidate(
                row,
                MediaQuery("tt123", "movie"),
                DiscoveryContext(frozenset({"usenet"}), b"a" * 32),
            )
        )

    async def test_generated_nzb_row_is_shared_by_compatible_provider_kinds(self):
        adapter = EasynewsSearchAdapter(
            _Session(
                _Response(
                    200,
                    {
                        "dlFarm": "farm",
                        "dlPort": 443,
                        "data": [
                            {
                                "0": "item",
                                "10": "Example",
                                "11": ".mkv",
                                "sig": "signature",
                                "rawSize": 42,
                            }
                        ],
                    },
                )
            ),
            EasynewsSearchAccount(
                "member",
                "secret",
                None,
                SOURCE_ID,
                frozenset({"stremio_nntp", "nzbdav"}),
            ),
        )

        result = await adapter.search(
            MediaQuery("tt123", "movie"),
            DiscoveryContext(frozenset({"usenet"}), b"a" * 32),
        )

        locator = result.candidates[0].locators[0]
        self.assertEqual(locator.account_configuration_id, SOURCE_ID)
        self.assertEqual(locator.signature, "signature")
        self.assertEqual(locator.byte_size, 42)
        self.assertEqual(
            locator.policy.exact_provider_configuration_id,
            None,
        )
        self.assertEqual(
            locator.policy.allowed_provider_kinds,
            frozenset({"stremio_nntp", "nzbdav"}),
        )

    def test_generated_nzb_row_without_positive_size_is_not_rendered(self):
        adapter = EasynewsSearchAdapter(
            None,
            EasynewsSearchAccount(
                "member",
                "secret",
                None,
                SOURCE_ID,
                frozenset({"stremio_nntp"}),
            ),
        )

        candidate = adapter._candidate(
            {
                "file_id": "item",
                "filename": "Example",
                "extension": "mkv",
                "dlFarm": "farm",
                "dlPort": 443,
            },
            MediaQuery("tt123", "movie"),
            DiscoveryContext(frozenset({"usenet"}), b"a" * 32),
        )

        self.assertIsNone(candidate)

    def test_account_and_candidate_domains_are_closed_before_persistence(self):
        invalid_accounts = (
            ("member", "secret", "provider"),
            (
                "member",
                "secret",
                None,
                None,
                frozenset({"stremio_nntp"}),
            ),
            (
                "member",
                "secret",
                None,
                SOURCE_ID,
                frozenset({"unknown"}),
            ),
        )
        for values in invalid_accounts:
            with self.subTest(values=values), self.assertRaises(ValueError):
                EasynewsSearchAccount(*values)

        adapter = EasynewsSearchAdapter(
            None,
            EasynewsSearchAccount("member", "secret", PROVIDER_ID),
        )
        row = {
            "file_id": "item",
            "filename": "Feature",
            "extension": "mkv",
            "dlFarm": "farm",
            "dlPort": 443,
            "size": 1 << 63,
        }
        query = MediaQuery("tt123", "movie")
        with self.assertRaisesRegex(ValueError, "partition"):
            adapter._candidate(
                row,
                query,
                DiscoveryContext(frozenset({"usenet"})),
            )
        candidate = adapter._candidate(
            row,
            query,
            DiscoveryContext(frozenset({"usenet"}), b"a" * 32),
        )
        self.assertIsNotNone(candidate)
        self.assertIsNone(candidate.size)
        self.assertIsNone(candidate.locators[0].byte_size)

    def test_only_explicit_password_flags_exclude_provider_video_rows(self):
        adapter = EasynewsSearchAdapter(
            None,
            EasynewsSearchAccount("member", "secret", PROVIDER_ID),
        )
        base = {
            "file_id": "item",
            "filename": "Feature",
            "extension": "mkv",
            "dlFarm": "farm",
            "dlPort": 443,
            "size": 42,
        }
        for passworded in (True, 1, "1"):
            fields = {"passwd": passworded}
            with self.subTest(fields=fields):
                self.assertIsNone(
                    adapter._candidate(
                        {**base, **fields},
                        MediaQuery("tt123", "movie"),
                        DiscoveryContext(
                            frozenset({"usenet"}),
                            b"a" * 32,
                        ),
                    )
                )

        for fields in (
            {"filename": "Feature.sample"},
            {"filename": "Feature", "extension": "future-video"},
            {"passwd": "future"},
            {"passworded": "yes"},
        ):
            with self.subTest(fields=fields):
                self.assertIsNotNone(
                    adapter._candidate(
                        {**base, **fields},
                        MediaQuery("tt123", "movie"),
                        DiscoveryContext(frozenset({"usenet"}), b"a" * 32),
                    )
                )

    async def test_non_usenet_branches_do_not_call_easynews(self):
        adapter = EasynewsSearchAdapter(
            None,
            EasynewsSearchAccount("member", "secret", PROVIDER_ID),
        )

        result = await adapter.search(
            MediaQuery("tt123", "movie"), DiscoveryContext(frozenset({"bittorrent"}))
        )

        self.assertEqual(result.candidates, ())

    async def test_database_lease_wraps_only_the_easynews_call(self):
        lease = type("Lease", (), {"release": AsyncMock()})()
        governor = type(
            "Governor",
            (),
            {"acquire_concurrency": AsyncMock(return_value=lease)},
        )()
        adapter = EasynewsSearchAdapter(
            _Session(_Response(200, {"data": []})),
            EasynewsSearchAccount("member", "secret", PROVIDER_ID),
            governor=governor,
            governor_scope=b"a" * 32,
        )

        result = await adapter.search(
            MediaQuery("tt123", "movie"),
            DiscoveryContext(frozenset({"usenet"}), b"a" * 32, trace_id="trace"),
        )

        self.assertEqual(result.coverage, frozenset({"usenet"}))
        self.assertEqual(
            governor.acquire_concurrency.await_args.kwargs["owner_request_id"],
            "trace",
        )
        lease.release.assert_awaited_once()

    async def test_exhausted_database_lease_is_exposed_without_network_work(self):
        session = _Session(_Response(200, {"data": []}))
        governor = type(
            "Governor",
            (),
            {"acquire_concurrency": AsyncMock(return_value=None)},
        )()
        adapter = EasynewsSearchAdapter(
            session,
            EasynewsSearchAccount("member", "secret", PROVIDER_ID),
            governor=governor,
            governor_scope=b"a" * 32,
        )

        with self.assertRaisesRegex(EasynewsSearchError, "easynews_search_busy"):
            await adapter.search(
                MediaQuery("tt123", "movie"),
                DiscoveryContext(frozenset({"usenet"}), b"a" * 32),
            )

        self.assertEqual(session.calls, [])
        self.assertEqual(governor.acquire_concurrency.await_count, 1)

    async def test_unusable_rows_do_not_discard_later_valid_results(self):
        first_lease = type("Lease", (), {"release": AsyncMock()})()
        governor = type(
            "Governor",
            (),
            {"acquire_concurrency": AsyncMock(return_value=first_lease)},
        )()
        session = _Session(
            _Response(
                200,
                {
                    "dlFarm": "farm",
                    "dlPort": 443,
                    "data": 101
                    * [
                        {
                            "id": "obsolete",
                            "filename": "Movie",
                            "extension": "mkv",
                        }
                    ]
                    + [
                        {
                            "0": "item",
                            "10": "Movie",
                            "11": "mkv",
                            "rawSize": 42,
                        }
                    ],
                },
            ),
        )
        record_failure = AsyncMock()
        adapter = EasynewsSearchAdapter(
            session,
            EasynewsSearchAccount(
                "member",
                "secret",
                PROVIDER_ID,
                SOURCE_ID,
            ),
            governor=governor,
            governor_scope=b"a" * 32,
            runtime_failure_recorder=record_failure,
        )

        result = await adapter.search(
            MediaQuery("tt123", "movie", title_aliases=("Movie",)),
            DiscoveryContext(
                frozenset({"usenet"}),
                b"a" * 32,
                trace_id="trace",
            ),
        )

        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0].title, "Movie")
        self.assertEqual(result.coverage, frozenset({"usenet"}))
        self.assertEqual(
            [call.args[1] for call in governor.acquire_concurrency.await_args_list],
            ["easynews_search_v2"],
        )
        self.assertEqual(
            [
                call.kwargs["limit"]
                for call in governor.acquire_concurrency.await_args_list
            ],
            [2],
        )
        first_lease.release.assert_awaited_once()
        self.assertEqual(
            session.calls[0][0][0],
            "https://members.easynews.com/2.0/search/solr-search/advanced",
        )
        record_failure.assert_not_awaited()

    async def test_search_uses_metadata_title_without_a_second_length_limit(self):
        title = "x" * 513
        session = _Session(_Response(200, {"data": []}))
        adapter = EasynewsSearchAdapter(
            session,
            EasynewsSearchAccount("member", "secret", PROVIDER_ID),
        )

        await adapter.search(
            MediaQuery("tt123", "movie", title_aliases=(title,)),
            DiscoveryContext(frozenset({"usenet"}), b"a" * 32),
        )

        self.assertEqual(session.calls[0][1]["params"]["gps"], title)

    async def test_auth_failure_never_falls_back_or_exposes_a_response(self):
        session = _Session(
            [
                _Response(401, b"secret upstream body"),
                _Response(200, {"data": []}),
            ]
        )
        record_failure = AsyncMock()
        adapter = EasynewsSearchAdapter(
            session,
            EasynewsSearchAccount(
                "member",
                "secret",
                PROVIDER_ID,
                SOURCE_ID,
            ),
            runtime_failure_recorder=record_failure,
        )

        with self.assertRaisesRegex(EasynewsSearchError, "easynews_auth_failed"):
            await adapter.search(
                MediaQuery("tt123", "movie", title_aliases=("Movie",)),
                DiscoveryContext(frozenset({"usenet"}), b"a" * 32),
            )

        self.assertEqual(len(session.calls), 1)
        record_failure.assert_awaited_once_with(
            SOURCE_ID,
            "auth_failed",
            "credentials_rejected",
            None,
        )

    async def test_rate_limit_records_only_bounded_transient_source_evidence(self):
        session = _Session(
            _Response(
                429,
                b"private upstream body",
                {"Retry-After": "9999"},
            )
        )
        record_failure = AsyncMock()
        adapter = EasynewsSearchAdapter(
            session,
            EasynewsSearchAccount(
                "member",
                "secret",
                PROVIDER_ID,
                SOURCE_ID,
            ),
            runtime_failure_recorder=record_failure,
        )

        with self.assertRaisesRegex(
            EasynewsSearchError,
            "easynews_search_rate_limited",
        ):
            await adapter.search(
                MediaQuery("tt123", "movie", title_aliases=("Movie",)),
                DiscoveryContext(frozenset({"usenet"}), b"a" * 32),
            )

        record_failure.assert_awaited_once_with(
            SOURCE_ID,
            "transiently_unreachable",
            "easynews_search_rate_limited",
            300,
        )

    async def test_validation_proves_authentication_and_the_v2_codec(self):
        session = _Session(_Response(200, {"data": []}))
        adapter = EasynewsSearchAdapter(
            session,
            EasynewsSearchAccount("member", "secret", None),
        )

        status = await adapter.validate_config()

        self.assertEqual(status.readiness, Readiness.READY)
        self.assertEqual(
            session.calls[0][1]["params"]["gps"],
            "comet-capability-validation",
        )

    async def test_validation_classifies_rejected_credentials(self):
        session = _Session(_Response(403, b"private upstream body"))
        adapter = EasynewsSearchAdapter(
            session,
            EasynewsSearchAccount("member", "secret", None),
        )

        status = await adapter.validate_config()

        self.assertEqual(status.readiness, Readiness.TERMINAL_FAILURE)
        self.assertEqual(status.code, "credentials_rejected")
        self.assertEqual(len(session.calls), 1)

    async def test_duplicate_json_keys_use_normal_last_value_semantics(self):
        session = _Session(
            [
                _Response(200, b'{"data":[],"data":[]}'),
                _Response(200, b"x" * 25),
            ]
        )
        adapter = EasynewsSearchAdapter(
            session,
            EasynewsSearchAccount("member", "secret", PROVIDER_ID),
        )

        with patch(
            "comet.discovery.adapters.easynews._MAX_JSON_BYTES",
            24,
        ):
            result = await adapter.search(
                MediaQuery("tt123", "movie", title_aliases=("Movie",)),
                DiscoveryContext(frozenset({"usenet"}), b"a" * 32),
            )

        self.assertEqual(result.coverage, frozenset({"usenet"}))
        self.assertEqual(result.diagnostics, ())
        self.assertEqual(len(session.calls), 1)
