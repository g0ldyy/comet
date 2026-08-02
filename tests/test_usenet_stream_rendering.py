import base64
import unittest
import uuid
from unittest.mock import AsyncMock, patch

import orjson
from fastapi import BackgroundTasks, Request
from RTN import parse

from comet.api.endpoints.stream import (
    _render_server_usenet_options,
    _render_stremio_nntp_options,
    stream,
)
from comet.core.capabilities import EligibleProvider
from comet.core.sources import (
    LocatorKind,
    LocatorPolicy,
    NzbArtifactRef,
    RealNzbRef,
    ReleaseCandidate,
    ReleaseScope,
    TorrentLocator,
    TransportKind,
)
from comet.playback.presentation import ProviderOption
from comet.playback.repository import RenderedCandidateIds
from comet.playback.tokens import CapabilityCodec
from comet.services.media_search import MediaSearchResult, MediaSearchStatus
from comet.utils.parsing import MediaScope

ROOT = base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("=")


class StreamCandidateFactory:
    @staticmethod
    def _usenet_candidate(
        candidate_id: str,
        title: str,
        *,
        source: str = "Usenet",
        locators=None,
        size: int | None = 1_000_000_000,
    ) -> ReleaseCandidate:
        if locators is None:
            locators = (
                NzbArtifactRef(
                    locator_id=f"{candidate_id}:nzb",
                    kind=LocatorKind.NZB_ARTIFACT,
                    policy=LocatorPolicy(frozenset({"stremio_nntp"})),
                    artifact_sha256="b" * 64,
                    manifest_identity="nm1:" + "c" * 64,
                ),
            )
        return ReleaseCandidate(
            candidate_id=candidate_id,
            media_id="tt1234567",
            scope=ReleaseScope.MOVIE,
            transport=TransportKind.USENET,
            title=title,
            locators=tuple(locators),
            size=size,
            source=source,
            parsed=parse(title),
        )

    @staticmethod
    def _option(
        candidate: ReleaseCandidate,
        kind: str,
        configuration_id: str = "provider",
        *,
        position: int = 0,
    ) -> ProviderOption:
        return ProviderOption(
            candidate.candidate_id,
            EligibleProvider(configuration_id, kind, position),
            candidate.locators,
        )


class UsenetStreamRenderingTests(unittest.IsolatedAsyncioTestCase):
    async def test_v2_debrid_stream_uses_the_stable_signed_playback_path(self):
        provider_id = str(uuid.uuid4())
        info_hash = "a" * 40
        title = "Movie.2026.1080p.WEB-DL-GROUP"
        locator = TorrentLocator(
            locator_id="torrent-locator",
            kind=LocatorKind.TORRENT,
            policy=LocatorPolicy(frozenset({"realdebrid"})),
            info_hash=info_hash,
            selection_title=title,
            selection_parsed_json="{}",
        )
        candidate = ReleaseCandidate(
            candidate_id=f"btih:{info_hash}",
            media_id="tt1234567",
            scope=ReleaseScope.MOVIE,
            transport=TransportKind.BITTORRENT,
            title=title,
            locators=(locator,),
            size=1_000_000,
            parsed=parse(title),
        )
        provider = EligibleProvider(provider_id, "realdebrid", 0)
        option = ProviderOption(
            candidate.candidate_id,
            provider,
            candidate.locators,
        )
        result = MediaSearchResult(
            MediaSearchStatus.OK,
            metadata={"title": "Movie"},
            media_scope=MediaScope.MOVIE,
            media_only_id="tt1234567",
            torrents={
                info_hash: {
                    "title": title,
                    "size": 1_000_000,
                    "seeders": 10,
                    "tracker": "Public",
                    "parsed": parse(title),
                }
            },
            ranked_info_hashes=[info_hash],
            service_cache_status={info_hash: {provider_id: True}},
            candidates=(candidate,),
            provider_options=(option,),
            provider_capabilities={
                (candidate.candidate_id, provider_id): "pi2.signed.capability"
            },
        )
        config = {
            "_debridEntries": [
                {
                    "configurationId": provider_id,
                    "service": "realdebrid",
                    "apiKey": "secret",
                }
            ],
            "_enableTorrent": False,
            "scrapeDebridAccountTorrents": False,
            "debridStreamProxyPassword": "",
            "schemaVersion": 2,
            "maxResultsPerResolution": 0,
            "cachedOnly": False,
            "resultFormat": ["all"],
            "playbackProviders": [
                {
                    "configurationId": provider_id,
                    "enabled": True,
                    "kind": "realdebrid",
                    "displayName": "Living room",
                }
            ],
        }
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "scheme": "https",
                "server": ("comet.example", 443),
                "path": "/config/stream/movie/tt1234567.json",
                "query_string": b"",
                "headers": (),
                "client": ("127.0.0.1", 12345),
            }
        )

        with (
            patch("comet.api.endpoints.stream.config_check", return_value=config),
            patch(
                "comet.api.endpoints.stream.search_media",
                AsyncMock(return_value=result),
            ),
            patch(
                "comet.api.endpoints.stream._render_stremio_nntp_options",
                AsyncMock(return_value=[]),
            ),
            patch("comet.api.endpoints.stream.settings.HTTP_CACHE_ENABLED", False),
            patch(
                "comet.api.endpoints.stream.settings.PUBLIC_BASE_URL",
                "https://comet.example",
            ),
        ):
            response = await stream(
                request,
                "movie",
                "tt1234567",
                BackgroundTasks(),
                b64config="config",
            )

        payload = orjson.loads(response.body)
        self.assertEqual(len(payload["streams"]), 1)
        self.assertEqual(
            payload["streams"][0]["url"],
            "https://comet.example/config/playback/v2/pi2.signed.capability",
        )
        self.assertNotIn(f"/playback/{info_hash}/0/", payload["streams"][0]["url"])

    async def test_v2_renderer_preserves_the_canonical_cross_transport_order(self):
        debrid_id = str(uuid.uuid4())
        usenet_id = str(uuid.uuid4())
        info_hash = "a" * 40
        title = "Movie.2026.1080p.WEB-DL-GROUP"
        torrent = ReleaseCandidate(
            candidate_id=f"btih:{info_hash}",
            media_id="tt1234567",
            scope=ReleaseScope.MOVIE,
            transport=TransportKind.BITTORRENT,
            title=title,
            locators=(
                TorrentLocator(
                    locator_id="torrent-locator",
                    kind=LocatorKind.TORRENT,
                    policy=LocatorPolicy(frozenset({"realdebrid"})),
                    info_hash=info_hash,
                ),
            ),
            size=1_000_000,
            parsed=parse(title),
        )
        usenet = StreamCandidateFactory._usenet_candidate("usenet", title)
        usenet_option = ProviderOption(
            usenet.candidate_id,
            EligibleProvider(usenet_id, "nzbdav", 0),
            usenet.locators,
        )
        debrid_option = ProviderOption(
            torrent.candidate_id,
            EligibleProvider(debrid_id, "realdebrid", 1),
            torrent.locators,
        )
        result = MediaSearchResult(
            MediaSearchStatus.OK,
            metadata={"title": "Movie"},
            media_scope=MediaScope.MOVIE,
            media_only_id="tt1234567",
            torrents={
                info_hash: {
                    "title": title,
                    "size": 1_000_000,
                    "seeders": 10,
                    "tracker": "Public",
                    "parsed": parse(title),
                }
            },
            ranked_info_hashes=[info_hash],
            service_cache_status={},
            candidates=(torrent, usenet),
            provider_options=(usenet_option, debrid_option),
            provider_capabilities={
                (usenet.candidate_id, usenet_id): "pi2.usenet",
                (torrent.candidate_id, debrid_id): "pi2.debrid",
            },
        )
        config = {
            "_debridEntries": [
                {
                    "configurationId": debrid_id,
                    "service": "realdebrid",
                    "apiKey": "secret",
                }
            ],
            "_enableTorrent": False,
            "scrapeDebridAccountTorrents": False,
            "debridStreamProxyPassword": "",
            "schemaVersion": 2,
            "maxResultsPerResolution": 0,
            "cachedOnly": False,
            "resultFormat": ["all"],
            "playbackProviders": [
                {
                    "configurationId": usenet_id,
                    "enabled": True,
                    "kind": "nzbdav",
                    "displayName": "NzbDAV",
                },
                {
                    "configurationId": debrid_id,
                    "enabled": True,
                    "kind": "realdebrid",
                    "displayName": "Real-Debrid",
                },
            ],
        }
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "scheme": "https",
                "server": ("comet.example", 443),
                "path": "/config/stream/movie/tt1234567.json",
                "query_string": b"",
                "headers": (),
                "client": ("127.0.0.1", 12345),
            }
        )

        with (
            patch("comet.api.endpoints.stream.config_check", return_value=config),
            patch(
                "comet.api.endpoints.stream.search_media",
                AsyncMock(return_value=result),
            ),
            patch(
                "comet.api.endpoints.stream._render_stremio_nntp_options",
                AsyncMock(return_value={}),
            ),
            patch("comet.api.endpoints.stream.settings.HTTP_CACHE_ENABLED", False),
            patch(
                "comet.api.endpoints.stream.settings.PUBLIC_BASE_URL",
                "https://comet.example",
            ),
        ):
            response = await stream(
                request,
                "movie",
                "tt1234567",
                BackgroundTasks(),
                b64config="config",
            )

        streams = orjson.loads(response.body)["streams"]
        self.assertEqual(
            [entry["url"].rsplit("/", 1)[-1] for entry in streams],
            ["pi2.usenet", "pi2.debrid"],
        )

    async def test_renders_capability_failure_with_guided_reconfiguration(self):
        result = MediaSearchResult(
            MediaSearchStatus.OK,
            metadata={"title": "Movie"},
            media_scope=MediaScope.MOVIE,
            media_only_id="tt1234567",
            discovery_diagnostics=("TorBox Usenet: authentication failed",),
        )
        config = {
            "_debridEntries": [],
            "_enableTorrent": False,
            "scrapeDebridAccountTorrents": False,
            "debridStreamProxyPassword": "",
            "schemaVersion": 2,
            "maxResultsPerResolution": 0,
            "cachedOnly": False,
            "resultFormat": ["all"],
            "playbackProviders": [],
        }
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "scheme": "https",
                "server": ("comet.example", 443),
                "path": "/config/stream/movie/tt1234567.json",
                "query_string": b"",
                "headers": (),
                "client": ("127.0.0.1", 12345),
            }
        )

        with (
            patch(
                "comet.api.endpoints.stream.config_check",
                return_value=config,
            ),
            patch(
                "comet.api.endpoints.stream.search_media",
                AsyncMock(return_value=result),
            ),
            patch(
                "comet.api.endpoints.stream._render_stremio_nntp_options",
                AsyncMock(return_value=[]),
            ),
            patch("comet.api.endpoints.stream.settings.HTTP_CACHE_ENABLED", False),
            patch(
                "comet.api.endpoints.stream.settings.PUBLIC_BASE_URL",
                "https://comet.example",
            ),
        ):
            response = await stream(
                request,
                "movie",
                "tt1234567",
                BackgroundTasks(),
                b64config="config",
            )

        payload = orjson.loads(response.body)
        self.assertEqual(len(payload["streams"]), 1)
        diagnostic = payload["streams"][0]
        self.assertEqual(diagnostic["name"], "[⚠️] Comet setup")
        self.assertEqual(
            diagnostic["description"],
            "TorBox Usenet: authentication failed. "
            "Open the addon configuration to review this connection.",
        )
        self.assertTrue(diagnostic["url"].endswith("/config/configure"))

    async def test_hybrid_direct_torrent_sorting_does_not_drop_usenet(self):
        candidate = StreamCandidateFactory._usenet_candidate(
            "usenet",
            "Movie.2026.1080p.WEB-DL-GROUP",
            source="My indexer",
        )
        provider = EligibleProvider(
            "11111111-1111-4111-8111-111111111111",
            "nzbdav",
            0,
        )
        option = ProviderOption(
            candidate.candidate_id,
            provider,
            candidate.locators,
        )
        result = MediaSearchResult(
            MediaSearchStatus.OK,
            metadata={"title": "Movie"},
            media_scope=MediaScope.MOVIE,
            media_only_id="tt1234567",
            is_torrent_only=True,
            candidates=(candidate,),
            provider_options=(option,),
            provider_capabilities={
                (candidate.candidate_id, provider.configuration_id): "pi2.capability"
            },
        )
        config = {
            "_debridEntries": [],
            "_enableTorrent": True,
            "scrapeDebridAccountTorrents": False,
            "debridStreamProxyPassword": "",
            "schemaVersion": 2,
            "enabledTransports": ("usenet",),
            "maxResultsPerResolution": 0,
            "cachedOnly": False,
            "resultFormat": ["all"],
            "playbackProviders": [
                {
                    "configurationId": provider.configuration_id,
                    "enabled": True,
                    "kind": "nzbdav",
                    "displayName": "NzbDAV",
                }
            ],
        }
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "scheme": "https",
                "server": ("comet.example", 443),
                "path": "/config/stream/movie/tt1234567.json",
                "query_string": b"",
                "headers": (),
                "client": ("127.0.0.1", 12345),
            }
        )

        with (
            patch(
                "comet.api.endpoints.stream.config_check",
                return_value=config,
            ),
            patch(
                "comet.api.endpoints.stream.search_media",
                AsyncMock(return_value=result),
            ),
            patch(
                "comet.api.endpoints.stream._render_stremio_nntp_options",
                AsyncMock(return_value=[]),
            ),
            patch("comet.api.endpoints.stream.settings.HTTP_CACHE_ENABLED", False),
            patch(
                "comet.api.endpoints.stream.settings.PUBLIC_BASE_URL",
                "https://comet.example",
            ),
        ):
            response = await stream(
                request,
                "movie",
                "tt1234567",
                BackgroundTasks(),
                b64config="config",
            )

        payload = orjson.loads(response.body)
        self.assertEqual(len(payload["streams"]), 1)
        usenet_stream = payload["streams"][0]
        self.assertEqual(usenet_stream["name"], "[NzbDAV📰] Comet 1080p")
        self.assertEqual(
            usenet_stream["description"],
            "📄 Movie.2026.1080p.WEB-DL-GROUP\n"
            "⭐ WEB-DL | 🏷️ GROUP\n"
            "💾 953.7 MB 🔎 My indexer",
        )
        self.assertEqual(
            usenet_stream["behaviorHints"],
            {
                "bingeGroup": ("comet|11111111-1111-4111-8111-111111111111|usenet"),
                "filename": "Movie.2026.1080p.WEB-DL-GROUP",
                "videoSize": 1_000_000_000,
            },
        )

    async def test_renders_replay_safe_real_nzb_as_a_lazy_ni2_handoff(self):
        provider_id = str(uuid.uuid4())
        candidate_id = str(uuid.uuid4())
        locators = tuple(
            RealNzbRef(
                locator_id=f"source-locator-{index}",
                kind=LocatorKind.REAL_NZB,
                policy=LocatorPolicy(
                    frozenset({"stremio_nntp"}),
                    owner_configuration_partition=b"a" * 32,
                ),
                adapter_configuration_id=str(uuid.uuid4()),
                remote_guid=f"opaque-guid-{index}",
            )
            for index in range(4)
        )
        rendered_locator_ids = tuple(str(uuid.uuid4()) for _locator in locators)
        candidate = StreamCandidateFactory._usenet_candidate(
            "candidate",
            "Movie.2024.1080p",
            locators=locators,
        )
        option = StreamCandidateFactory._option(
            candidate,
            "stremio_nntp",
            provider_id,
        )
        result = MediaSearchResult(
            MediaSearchStatus.OK,
            candidates=(candidate,),
            provider_options=(option,),
            rendered_candidate_ids={
                "candidate": RenderedCandidateIds(
                    candidate_id,
                    {
                        locator.locator_id: rendered_id
                        for locator, rendered_id in zip(
                            locators,
                            rendered_locator_ids,
                            strict=True,
                        )
                    },
                )
            },
        )
        config = {
            "schemaVersion": 2,
            "accounts": {
                "nntp-account": {
                    "kind": "nntp",
                    "servers": [
                        {
                            "host": "news.example",
                            "port": 563,
                            "tls_mode": "implicit_tls",
                            "username": "user",
                            "password": "pass",
                            "connections": 1,
                        }
                    ],
                }
            },
            "resultFormat": ["all"],
            "playbackProviders": [
                {
                    "configurationId": provider_id,
                    "enabled": True,
                    "kind": "stremio_nntp",
                    "displayName": "NNTP",
                    "accountId": "nntp-account",
                    "options": {},
                }
            ],
        }

        with (
            patch(
                "comet.api.endpoints.stream.settings.USENET_ENABLED",
                True,
            ),
            patch(
                "comet.api.endpoints.stream.settings.COMET_CAPABILITY_SECRET",
                ROOT,
            ),
            patch(
                "comet.api.endpoints.stream.NzbBroker.resolve_owned_artifacts",
                AsyncMock(return_value={}),
            ) as resolve_artifact,
        ):
            streams = await _render_stremio_nntp_options(
                result,
                config,
                "https://comet.example/config",
            )

        self.assertEqual(len(streams), 1)
        self.assertEqual(
            next(iter(streams.values()))["fileMustInclude"],
            r"/Movie\.2024\.1080p.*?\.(?:mkv|mp4|m4v|mov|webm|avi|ts|m2ts|mpg|mpeg|wmv|flv)"
            r"(?:[^A-Z0-9]|$)/i",
        )
        self.assertNotIn("fileIdx", next(iter(streams.values())))
        capability = (
            next(iter(streams.values()))["nzbUrl"].split("/")[-1].removesuffix(".nzb")
        )
        intent = CapabilityCodec(ROOT).decode_nzb_handoff_intent(
            capability,
            partition=CapabilityCodec(ROOT).configuration_partition_for_config(config),
        )
        self.assertEqual(intent.candidate_id, candidate_id)
        self.assertEqual(intent.provider_configuration_id, provider_id)
        self.assertEqual(intent.locator_ids, rendered_locator_ids[:3])
        resolve_artifact.assert_not_awaited()

    def test_renders_only_a_committed_valid_nzbdav_provider_option(self):
        candidate = StreamCandidateFactory._usenet_candidate(
            "candidate",
            "Example.2024.1080p",
            size=42,
        )
        option = StreamCandidateFactory._option(candidate, "nzbdav")
        result = MediaSearchResult(
            MediaSearchStatus.OK,
            candidates=(candidate,),
            provider_options=(option,),
            provider_capabilities={("candidate", "provider"): "pi2.payload.signature"},
        )
        config = {
            "resultFormat": ["all"],
            "playbackProviders": [
                {
                    "configurationId": "provider",
                    "displayName": "Bridge",
                    "options": {
                        "internalBaseUrl": "https://bridge.example",
                        "sabApiKey": "key",
                        "webdavUsername": "user",
                        "webdavPassword": "password",
                    },
                }
            ],
        }

        streams = _render_server_usenet_options(
            result, config, "https://comet.example/config"
        )

        self.assertEqual(len(streams), 1)
        self.assertEqual(next(iter(streams.values()))["name"], "[Bridge📰] Comet 1080p")
        self.assertEqual(
            next(iter(streams.values()))["url"],
            "https://comet.example/config/playback/v2/pi2.payload.signature",
        )

    def test_returns_no_stream_without_provider_options(self):
        result = MediaSearchResult(MediaSearchStatus.OK)

        self.assertEqual(
            _render_server_usenet_options(
                result,
                {"resultFormat": ["all"], "playbackProviders": []},
                "https://comet.example",
            ),
            {},
        )

    def test_falls_back_to_the_release_title_when_selected_fields_do_not_apply(self):
        candidate = StreamCandidateFactory._usenet_candidate(
            "candidate",
            "Movie.2026.1080p.WEB-DL-GROUP",
        )
        option = StreamCandidateFactory._option(candidate, "nzbdav")
        result = MediaSearchResult(
            MediaSearchStatus.OK,
            candidates=(candidate,),
            provider_options=(option,),
            provider_capabilities={("candidate", "provider"): "pi2.payload.signature"},
        )
        config = {
            "resultFormat": ["seeders"],
            "playbackProviders": [
                {
                    "configurationId": "provider",
                    "displayName": "NzbDAV",
                }
            ],
        }

        rendered = next(
            iter(
                _render_server_usenet_options(
                    result,
                    config,
                    "https://comet.example/config",
                ).values()
            )
        )

        self.assertEqual(
            rendered["description"],
            "📄 Movie.2026.1080p.WEB-DL-GROUP",
        )
        self.assertNotIn("Empty result format", rendered["description"])

    def test_renders_a_valid_easynews_provider_option(self):
        candidate = StreamCandidateFactory._usenet_candidate(
            "candidate",
            "Example",
            size=None,
        )
        option = StreamCandidateFactory._option(candidate, "easynews")
        result = MediaSearchResult(
            MediaSearchStatus.OK,
            candidates=(candidate,),
            provider_options=(option,),
            provider_capabilities={("candidate", "provider"): "pi2.payload.signature"},
        )
        config = {
            "resultFormat": ["all"],
            "playbackProviders": [
                {
                    "configurationId": "provider",
                    "displayName": "Easynews",
                    "options": {"username": "member", "password": "secret"},
                }
            ],
        }

        streams = _render_server_usenet_options(
            result, config, "https://comet.example/config"
        )

        self.assertEqual(len(streams), 1)
        self.assertEqual(next(iter(streams.values()))["name"], "[Easynews📰] Comet")

    def test_renders_native_and_stremthru_server_playback_options(self):
        candidate = StreamCandidateFactory._usenet_candidate(
            "candidate",
            "Example",
            size=42,
        )
        providers = tuple(
            StreamCandidateFactory._option(
                candidate,
                kind,
                configuration_id,
                position=position,
            )
            for position, (kind, configuration_id) in enumerate(
                (
                    ("comet_native_usenet", "native"),
                    ("stremthru_newz", "stremthru"),
                )
            )
        )
        result = MediaSearchResult(
            MediaSearchStatus.OK,
            candidates=(candidate,),
            provider_options=providers,
            provider_capabilities={
                ("candidate", "native"): "pi2.native.signature",
                ("candidate", "stremthru"): "pi2.stremthru.signature",
            },
        )
        config = {
            "resultFormat": ["all"],
            "playbackProviders": [
                {
                    "configurationId": "native",
                    "displayName": "Native",
                },
                {
                    "configurationId": "stremthru",
                    "displayName": "StremThru",
                },
            ],
        }

        streams = _render_server_usenet_options(
            result,
            config,
            "https://comet.example/config",
        )

        self.assertEqual(
            [stream["name"] for stream in streams.values()],
            ["[Native📰] Comet", "[StremThru📰] Comet"],
        )

    def test_renders_easynews_credentials_referenced_by_account(self):
        candidate = StreamCandidateFactory._usenet_candidate(
            "candidate",
            "Example",
            size=None,
        )
        option = StreamCandidateFactory._option(candidate, "easynews")
        result = MediaSearchResult(
            MediaSearchStatus.OK,
            candidates=(candidate,),
            provider_options=(option,),
            provider_capabilities={("candidate", "provider"): "pi2.payload.signature"},
        )
        config = {
            "accounts": {"account": {"username": "member", "password": "secret"}},
            "resultFormat": ["all"],
            "playbackProviders": [
                {
                    "configurationId": "provider",
                    "accountId": "account",
                    "displayName": "Easynews",
                    "options": {},
                }
            ],
        }

        self.assertEqual(
            len(
                _render_server_usenet_options(
                    result, config, "https://comet.example/config"
                )
            ),
            1,
        )

    def test_renders_torbox_usenet_only_with_an_explicit_playback_key(self):
        candidate = StreamCandidateFactory._usenet_candidate(
            "candidate",
            "Example",
            size=1,
        )
        option = StreamCandidateFactory._option(candidate, "torbox_usenet")
        result = MediaSearchResult(
            MediaSearchStatus.OK,
            candidates=(candidate,),
            provider_options=(option,),
            provider_capabilities={("candidate", "provider"): "pi2.payload.signature"},
        )
        config = {
            "accounts": {"torbox": {"apiKey": "key"}},
            "resultFormat": ["all"],
            "playbackProviders": [
                {
                    "configurationId": "provider",
                    "accountId": "torbox",
                    "displayName": "TorBox",
                    "options": {},
                }
            ],
        }

        streams = _render_server_usenet_options(
            result, config, "https://comet.example/config"
        )

        self.assertEqual(len(streams), 1)
        self.assertEqual(next(iter(streams.values()))["name"], "[TorBox📰] Comet")

    async def test_renders_an_owner_granted_artifact_as_a_client_nntp_handoff(self):
        unavailable = NzbArtifactRef(
            locator_id="unavailable-artifact",
            kind=LocatorKind.NZB_ARTIFACT,
            policy=LocatorPolicy(frozenset({"stremio_nntp"})),
            artifact_sha256="c" * 64,
            manifest_identity="nm1:" + "d" * 64,
        )
        artifact = NzbArtifactRef(
            locator_id="artifact",
            kind=LocatorKind.NZB_ARTIFACT,
            policy=LocatorPolicy(frozenset({"stremio_nntp"})),
            artifact_sha256="a" * 64,
            manifest_identity="nm1:" + "b" * 64,
        )
        candidate = StreamCandidateFactory._usenet_candidate(
            "candidate",
            "Example",
            locators=(unavailable, artifact),
            size=None,
        )
        option = StreamCandidateFactory._option(candidate, "stremio_nntp")
        result = MediaSearchResult(
            MediaSearchStatus.OK,
            candidates=(candidate,),
            provider_options=(option,),
        )
        config = {
            "schemaVersion": 2,
            "accounts": {},
            "resultFormat": ["all"],
            "playbackProviders": [
                {
                    "configurationId": "provider",
                    "enabled": True,
                    "kind": "stremio_nntp",
                    "displayName": "NNTP",
                    "options": {
                        "servers": [
                            {
                                "host": "news.example",
                                "port": 563,
                                "tls_mode": "implicit_tls",
                                "username": "user",
                                "password": "pass",
                                "connections": 1,
                            }
                        ],
                    },
                }
            ],
        }
        token = base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("=")
        resolved = type(
            "Artifact",
            (),
            {
                "grant_id": str(uuid.uuid4()),
                "manifest": [
                    {
                        "subject": '"Example.2024.1080p.mkv" yEnc',
                        "postings": [],
                    }
                ],
            },
        )()

        with (
            patch("comet.api.endpoints.stream.settings.USENET_ENABLED", True),
            patch("comet.api.endpoints.stream.settings.COMET_CAPABILITY_SECRET", token),
            patch(
                "comet.api.endpoints.stream.NzbBroker.resolve_owned_artifacts",
                AsyncMock(return_value={"a" * 64: resolved}),
            ),
        ):
            streams = await _render_stremio_nntp_options(
                result, config, "https://comet.example/config"
            )

        self.assertEqual(len(streams), 1)
        self.assertIn("nzbUrl", next(iter(streams.values())))
        self.assertIn("servers", next(iter(streams.values())))
        self.assertEqual(next(iter(streams.values()))["name"], "[NNTP📰] Comet")
        self.assertEqual(
            next(iter(streams.values()))["description"],
            "📄 Example\n🔎 Usenet",
        )
        self.assertEqual(
            next(iter(streams.values()))["behaviorHints"],
            {
                "bingeGroup": "comet|provider|candidate",
                "filename": "Example",
            },
        )
