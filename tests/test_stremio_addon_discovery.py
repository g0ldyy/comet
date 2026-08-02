import json
import unittest
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from comet.core.models import DiscoverySourceEntry
from comet.core.sources import LocatorKind, TransportKind
from comet.discovery.adapters.stremio_addon import (
    StremioAddonAdapter,
    _json_object,
    stremio_addon_configuration,
)
from comet.discovery.models import DiscoveryContext, MediaQuery
from comet.playback.base import Readiness
from comet.usenet.outbound import OutboundUrlError

SOURCE_ID = "11111111-1111-4111-8111-111111111111"


def _artifact():
    return type(
        "Artifact",
        (),
        {
            "artifact_sha256": "a" * 64,
            "nm1": "nm1:" + "b" * 64,
            "expires_at": 2_000_000_000,
        },
    )()


def _broker(artifact=None):
    return type(
        "Broker",
        (),
        {"ingest_bytes": AsyncMock(return_value=artifact or _artifact())},
    )()


class StremioAddonDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_signed_nzb_url_is_eagerly_brokered_without_persisting_hints(self):
        configuration = stremio_addon_configuration(
            SOURCE_ID,
            {
                "baseUrl": "https://addon.example/config",
                "authorization": "Bearer addon-secret",
            },
        )
        artifact = _artifact()
        broker = _broker(artifact)
        stream_response = (
            b'{"streams":[{"name":"Upstream","title":"Example.S02E03",'
            b'"nzbUrl":"https://signed.example/release.nzb?token=secret",'
            b'"servers":[{"host":"news.secret","password":"password"}],'
            b'"fileIdx":99,"fileMustInclude":"(?s).*"},'
            b'{"nzbUrl":"https://signed.example/release.nzb?token=secret"}]}'
        )
        adapter = StremioAddonAdapter(configuration, broker=broker)

        with patch(
            "comet.discovery.adapters.stremio_addon.fetch_http_bytes",
            AsyncMock(side_effect=[stream_response, b"<nzb/>"]),
        ) as fetch:
            result = await adapter.search(
                MediaQuery(
                    "tt123",
                    "series",
                    season=2,
                    episode=3,
                    title_aliases=("Example",),
                ),
                DiscoveryContext(frozenset({"usenet"}), b"a" * 32),
            )

        self.assertEqual(result.coverage, frozenset({"usenet"}))
        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        self.assertEqual(candidate.transport, TransportKind.USENET)
        self.assertEqual(candidate.title, "Example.S02E03")
        self.assertEqual(candidate.locators[0].kind, LocatorKind.NZB_ARTIFACT)
        self.assertEqual(
            candidate.locators[0].artifact_sha256,
            artifact.artifact_sha256,
        )
        self.assertEqual(
            candidate.locators[0].policy.owner_configuration_partition,
            b"a" * 32,
        )
        self.assertEqual(
            candidate.locators[0].policy.expires_at,
            2_000_000_000,
        )
        serialized = repr(candidate)
        self.assertNotIn("signed.example", serialized)
        self.assertNotIn("news.secret", serialized)
        self.assertNotIn("fileMustInclude", serialized)
        broker.ingest_bytes.assert_awaited_once_with(
            b"<nzb/>",
            owner_configuration_partition=b"a" * 32,
        )
        self.assertEqual(
            fetch.await_args_list[0].args[0],
            "https://addon.example/config/stream/series/tt123:2:3.json",
        )
        self.assertEqual(
            fetch.await_args_list[1].args[0],
            "https://signed.example/release.nzb?token=secret",
        )
        self.assertEqual(
            fetch.await_args_list[0].kwargs["origin_headers"],
            {"Authorization": "Bearer addon-secret"},
        )

    async def test_irreducible_url_failure_is_exposed(self):
        adapter = StremioAddonAdapter(
            stremio_addon_configuration(
                SOURCE_ID,
                {"baseUrl": "https://addon.example"},
            ),
            broker=object(),
        )
        stream_response = (
            b'{"streams":[{"nzbUrl":"https://signed.example/release.nzb"}]}'
        )

        with patch(
            "comet.discovery.adapters.stremio_addon.fetch_http_bytes",
            AsyncMock(
                side_effect=[
                    stream_response,
                    OutboundUrlError("unavailable"),
                ]
            ),
        ):
            with self.assertRaises(OutboundUrlError):
                await adapter.search(
                    MediaQuery("tt123", "movie", title_aliases=("Example",)),
                    DiscoveryContext(frozenset({"usenet"}), b"a" * 32),
                )

    async def test_non_usenet_branch_does_not_call_the_upstream_addon(self):
        adapter = StremioAddonAdapter(
            stremio_addon_configuration(
                SOURCE_ID,
                {"baseUrl": "https://addon.example"},
            ),
        )
        with patch(
            "comet.discovery.adapters.stremio_addon.fetch_http_bytes",
            AsyncMock(),
        ) as fetch:
            result = await adapter.search(
                MediaQuery("tt123", "movie"),
                DiscoveryContext(frozenset({"bittorrent"})),
            )

        self.assertEqual(result.candidates, ())
        fetch.assert_not_awaited()

    async def test_owned_ingestion_requires_an_exact_account_partition(self):
        adapter = StremioAddonAdapter(
            stremio_addon_configuration(
                SOURCE_ID,
                {"baseUrl": "https://addon.example"},
            ),
            broker=object(),
        )
        with patch(
            "comet.discovery.adapters.stremio_addon.fetch_http_bytes",
            AsyncMock(),
        ) as fetch:
            result = await adapter.search(
                MediaQuery("tt123", "movie"),
                DiscoveryContext(frozenset({"usenet"}), b"a" * 31),
            )

        self.assertEqual(result.coverage, frozenset())
        self.assertIn("brokerage", result.diagnostics[0])
        fetch.assert_not_awaited()

    async def test_expired_search_deadline_does_not_start_network_work(self):
        adapter = StremioAddonAdapter(
            stremio_addon_configuration(
                SOURCE_ID,
                {"baseUrl": "https://addon.example"},
            ),
            broker=object(),
        )
        with patch(
            "comet.discovery.adapters.stremio_addon.fetch_http_bytes",
            AsyncMock(),
        ) as fetch:
            result = await adapter.search(
                MediaQuery("tt123", "movie"),
                DiscoveryContext(
                    frozenset({"usenet"}),
                    b"a" * 32,
                    hard_deadline=0,
                ),
            )

        self.assertEqual(result.coverage, frozenset())
        self.assertIn("deadline", result.diagnostics[0])
        fetch.assert_not_awaited()

    async def test_manifest_validation_requires_the_standard_stream_resource(self):
        adapter = StremioAddonAdapter(
            stremio_addon_configuration(
                SOURCE_ID,
                {"manifestUrl": "https://addon.example/manifest.json"},
            )
        )
        with patch(
            "comet.discovery.adapters.stremio_addon.fetch_http_bytes",
            AsyncMock(return_value=b'{"resources":["stream"]}'),
        ):
            status = await adapter.validate_config()

        self.assertEqual(status.readiness, Readiness.READY)

    async def test_nzb_failure_is_not_hidden_behind_partial_results(self):
        adapter = StremioAddonAdapter(
            stremio_addon_configuration(
                SOURCE_ID,
                {"baseUrl": "https://addon.example"},
            ),
            broker=_broker(),
        )
        response = (
            b'{"streams":['
            b'{"nzbUrl":"https://files.example/one.nzb"},'
            b'{"nzbUrl":"https://files.example/two.nzb"}'
            b"]}"
        )

        with patch(
            "comet.discovery.adapters.stremio_addon.fetch_http_bytes",
            AsyncMock(
                side_effect=(
                    response,
                    b"<nzb/>",
                    OutboundUrlError("unavailable"),
                )
            ),
        ):
            with self.assertRaises(OutboundUrlError):
                await adapter.search(
                    MediaQuery("tt123", "movie"),
                    DiscoveryContext(frozenset({"usenet"}), b"a" * 32),
                )

    async def test_nzb_attempts_are_bounded_independently_of_the_deadline(self):
        adapter = StremioAddonAdapter(
            stremio_addon_configuration(
                SOURCE_ID,
                {"baseUrl": "https://addon.example", "maxResults": 10},
            ),
            broker=_broker(),
        )
        response = json.dumps(
            {
                "streams": [
                    {"nzbUrl": f"https://files.example/{index}.nzb"}
                    for index in range(100)
                ]
            }
        ).encode()
        fetch = AsyncMock(
            side_effect=(
                response,
                *(b"<nzb/>" for _ in range(20)),
            )
        )
        with patch(
            "comet.discovery.adapters.stremio_addon.fetch_http_bytes",
            fetch,
        ):
            result = await adapter.search(
                MediaQuery("tt123", "movie"),
                DiscoveryContext(frozenset({"usenet"}), b"a" * 32),
            )

        self.assertEqual(fetch.await_count, 21)
        self.assertEqual(result.coverage, frozenset())
        self.assertIn("incomplete", result.diagnostics[0])

    async def test_non_nzb_streams_are_ignored_but_announced_nzbs_are_exact(self):
        adapter = StremioAddonAdapter(
            stremio_addon_configuration(
                SOURCE_ID,
                {"baseUrl": "https://addon.example"},
            ),
            broker=_broker(),
        )
        response = (
            b'{"streams":['
            b'{"url":"https://video.example/movie.mp4"},'
            b'{"title":"  Exact title  ","nzbUrl":"https://files.example/one"}'
            b"]}"
        )

        with patch(
            "comet.discovery.adapters.stremio_addon.fetch_http_bytes",
            AsyncMock(side_effect=(response, b"<nzb/>")),
        ):
            result = await adapter.search(
                MediaQuery("tt123", "movie"),
                DiscoveryContext(frozenset({"usenet"}), b"a" * 32),
            )

        self.assertEqual(result.candidates[0].title, "  Exact title  ")

    async def test_unusable_items_do_not_discard_a_valid_stream(self):
        adapter = StremioAddonAdapter(
            stremio_addon_configuration(
                SOURCE_ID,
                {"baseUrl": "https://addon.example"},
            ),
            broker=_broker(),
        )
        streams = [None, {"nzbUrl": 42}] + [
            {"url": f"https://video.example/{index}"} for index in range(100)
        ]
        streams.append(
            {
                "title": "x" * 1_025,
                "nzbUrl": "https://files.example/release",
            }
        )
        response = json.dumps({"streams": streams}).encode()

        with patch(
            "comet.discovery.adapters.stremio_addon.fetch_http_bytes",
            AsyncMock(side_effect=(response, b"<nzb/>")),
        ):
            result = await adapter.search(
                MediaQuery(
                    "tt123",
                    "movie",
                    title_aliases=("Persistable title",),
                ),
                DiscoveryContext(frozenset({"usenet"}), b"a" * 32),
            )

        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0].title, "Persistable title")
        self.assertEqual(result.coverage, frozenset({"usenet"}))

    async def test_private_nzb_fetches_are_confined_to_the_addon_origin(self):
        with patch(
            "comet.discovery.adapters.stremio_addon.settings."
            "USENET_PRIVATE_UPSTREAM_ORIGINS",
            [
                "http://addon.local:8080",
                "http://unrelated.local:8080",
            ],
        ):
            adapter = StremioAddonAdapter(
                stremio_addon_configuration(
                    SOURCE_ID,
                    {"baseUrl": "http://addon.local:8080/config"},
                )
            )
            with patch(
                "comet.discovery.adapters.stremio_addon.fetch_http_bytes",
                AsyncMock(return_value=b"{}"),
            ) as fetch:
                await adapter._fetch(
                    "http://unrelated.local:8080/release.nzb",
                    maximum=1024,
                    accept="application/x-nzb",
                )

        self.assertEqual(
            fetch.await_args.kwargs["allowed_private_origins"],
            frozenset({"http://addon.local:8080"}),
        )


class StremioAddonCodecTests(unittest.TestCase):
    def test_source_schema_requires_a_url_and_bounded_eager_results(self):
        for options in (
            {},
            {"baseUrl": "https://addon.example", "maxResults": 11},
        ):
            with self.subTest(options=options), self.assertRaises(ValidationError):
                DiscoverySourceEntry(
                    configurationId="11111111-1111-4111-8111-111111111111",
                    kind="stremio_addon",
                    options=options,
                )
        for options in (
            {
                "baseUrl": "https://addon.example",
                "manifestUrl": "https://addon.example/manifest.json",
            },
            {"baseUrl": "https://addon.example", "token": "obsolete"},
            {"baseUrl": "https://addon.example", "label": "Duplicate name"},
        ):
            DiscoverySourceEntry(
                configurationId="11111111-1111-4111-8111-111111111111",
                kind="stremio_addon",
                options=options,
            )

    def test_configuration_rejects_only_insecure_urls(self):
        for options in (
            {"baseUrl": "http://public.example"},
            {"baseUrl": "https://user:pass@addon.example"},
            {
                "baseUrl": "https://addon.example",
                "authorization": "Bearer secret\x7f",
            },
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                stremio_addon_configuration(SOURCE_ID, options)
        for options in (
            {
                "baseUrl": "https://addon.example",
                "manifestUrl": "https://addon.example/manifest.json",
            },
            {"manifestUrl": "https://addon.example/not-manifest.json"},
            {"baseUrl": "https://addon.example", "token": "obsolete"},
            {"baseUrl": "https://addon.example/a/%2f/../b"},
        ):
            stremio_addon_configuration(SOURCE_ID, options)

        with self.assertRaises(ValueError):
            stremio_addon_configuration(
                "source",
                {"baseUrl": "https://addon.example"},
            )

    def test_json_codec_uses_normal_duplicate_key_semantics(self):
        self.assertEqual(
            _json_object(b'{"streams":[1],"streams":[]}'),
            {"streams": []},
        )
        with self.assertRaises(ValueError):
            _json_object(b'{"value":NaN}')
