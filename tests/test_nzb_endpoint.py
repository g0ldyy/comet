import base64
import gzip
import unittest
import uuid
from pathlib import Path
from typing import ClassVar
from unittest.mock import AsyncMock, patch

import orjson
import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from starlette.datastructures import URL
from starlette.requests import Request

from comet.api.endpoints.nzb import (
    _artifact_grant_from_token,
    _asset_catalog,
    _decode_nzb_document,
    _enforce_nzb_handoff_rate,
    _enforce_nzb_import_rate,
    _manual_provider_options,
    _manual_selection_intent,
    _read_import_json,
    _read_multipart_nzb,
    _read_uploaded_nzb,
    _same_origin,
    artifact_intent,
    import_upload,
    issue_artifact_capability,
    select_imported_asset,
)
from comet.core.capabilities import CapabilityPlan, EligibleProvider
from comet.core.sources import (
    LocatorKind,
    LocatorPolicy,
    NzbArtifactRef,
    ReleaseCandidate,
    ReleaseScope,
    TransportKind,
)
from comet.playback.manager import (
    PlaybackIntentResolution,
)
from comet.playback.providers.stremio_nntp import StremioNntpProvider
from comet.playback.repository import (
    RenderedCandidateIds,
    ResolvedPlaybackIntent,
)
from comet.playback.tokens import CapabilityCodec, PlaybackIntent
from comet.usenet.file_selection import FileSelectionError, UsenetAsset
from comet.usenet.nzb_broker import NzbArtifact

ROOT = base64.urlsafe_b64encode(b"a" * 32).decode().rstrip("=")


def _manual_candidate(
    partition: bytes,
    provider_kinds: set[str],
) -> ReleaseCandidate:
    return ReleaseCandidate(
        candidate_id="manual:test",
        media_id="manual:test",
        scope=ReleaseScope.MOVIE,
        transport=TransportKind.USENET,
        title="Movie.mkv",
        locators=(
            NzbArtifactRef(
                locator_id="manual-nzb:" + "c" * 64,
                kind=LocatorKind.NZB_ARTIFACT,
                policy=LocatorPolicy(
                    frozenset(provider_kinds),
                    owner_configuration_partition=partition,
                ),
                artifact_sha256="c" * 64,
                manifest_identity="nm1:" + "d" * 64,
            ),
        ),
    )


def _rendered_ids(candidate: ReleaseCandidate) -> RenderedCandidateIds:
    return RenderedCandidateIds(
        str(uuid.uuid4()),
        {candidate.locators[0].locator_id: str(uuid.uuid4())},
    )


def _provider_config(
    provider_id: str,
    kind: str,
    display_name: str,
    *,
    options: dict | None = None,
) -> dict:
    return {
        "accounts": {},
        "playbackProviders": [
            {
                "configurationId": provider_id,
                "displayName": display_name,
                "kind": kind,
                "enabled": True,
                "accountId": None,
                "options": {} if options is None else options,
            }
        ],
    }


def test_nzb_artifact_route_accepts_only_a_bound_na1_grant():
    codec = CapabilityCodec(ROOT)
    partition = codec.configuration_partition(b"normalized")
    grant = uuid.uuid4()
    token = codec.encode(
        "na1", partition=partition, suffix=[grant.bytes], ttl=60, now=100
    )

    assert _artifact_grant_from_token(token, partition, codec, now=120) == str(grant)


def test_nzb_artifact_capability_binds_the_normalized_configuration():
    codec = CapabilityCodec(ROOT)
    config = {"schemaVersion": 2, "playbackProviders": []}
    grant = str(uuid.uuid4())

    token = issue_artifact_capability(codec, config, grant)

    assert (
        _artifact_grant_from_token(
            token, codec.configuration_partition_for_config(config), codec
        )
        == grant
    )


class ProviderExportPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_response_is_private_and_non_leaking(self):
        from comet.api.app import app

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="https://comet.example",
        ) as client:
            response = await client.get("/nzb/export/v1/not-a-token.nzb")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.headers["cache-control"],
            "private, no-store",
        )
        self.assertEqual(
            response.headers["referrer-policy"],
            "no-referrer",
        )


class FakeUpload:
    def __init__(self, chunks):
        self._chunks = iter(chunks)

    async def read(self, _size):
        return next(self._chunks, b"")


class UploadReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_reader_accepts_bounded_plain_nzb(self):
        self.assertEqual(
            await _read_uploaded_nzb(FakeUpload([b"<nzb>", b"</nzb>"])),
            b"<nzb></nzb>",
        )

    async def test_multipart_reader_bounds_body_before_spooling(self):
        class Request:
            headers: ClassVar[dict[str, str]] = {
                "Content-Type": "multipart/form-data; boundary=boundary"
            }

            async def stream(self):
                yield b"x" * 65

        with patch("comet.api.endpoints.nzb._MAX_MULTIPART_BYTES", 64):
            with self.assertRaises(HTTPException) as error:
                await _read_multipart_nzb(Request())
        self.assertEqual(error.exception.status_code, 413)

    async def test_multipart_reader_accepts_exactly_one_file(self):
        body = (
            b"--boundary\r\n"
            b'Content-Disposition: form-data; name="file"; filename="test.nzb"\r\n'
            b"Content-Type: application/x-nzb\r\n"
            b"\r\n"
            b"<nzb></nzb>\r\n"
            b"--boundary--\r\n"
        )

        class Request:
            headers: ClassVar[dict[str, str]] = {
                "Content-Type": "multipart/form-data; boundary=boundary"
            }

            async def stream(self):
                yield body

        self.assertEqual(
            await _read_multipart_nzb(Request()),
            b"<nzb></nzb>",
        )

    def test_origin_check_normalizes_only_equivalent_http_origins(self):
        class Request:
            def __init__(self, origin):
                self.headers = {} if origin is None else {"origin": origin}
                self.url = URL("https://comet.example/import")

        self.assertTrue(_same_origin(Request(None)))
        self.assertTrue(_same_origin(Request("https://COMET.EXAMPLE:443")))
        self.assertFalse(_same_origin(Request("http://comet.example")))
        self.assertFalse(_same_origin(Request("https://other.example")))
        self.assertFalse(_same_origin(Request("null")))
        self.assertFalse(_same_origin(Request(" https://comet.example")))
        self.assertFalse(_same_origin(Request("https://comet.example:0")))

    async def test_url_import_json_uses_observed_bytes_not_declared_length(self):
        class Request:
            headers: ClassVar[dict[str, str]] = {
                "content-length": str(16 * 1024 * 1024 + 1)
            }

            async def stream(self):
                yield b'{"url":"https://example.test/file.nzb"}'

        self.assertEqual(
            (await _read_import_json(Request()))["url"],
            "https://example.test/file.nzb",
        )

    async def test_url_import_returns_only_the_broker_verified_asset_catalog(self):
        class ImportRequest:
            headers: ClassVar[dict[str, str]] = {
                "content-type": "application/json",
                "origin": "https://comet.example",
            }
            url = URL("https://comet.example/config/nzb/v1/import")

            async def stream(self):
                yield b'{"url":"https://indexer.example/release.nzb"}'

        config = {"schemaVersion": 2, "playbackProviders": []}
        codec = CapabilityCodec(ROOT)
        artifact = NzbArtifact(
            "b" * 64,
            str(uuid.uuid4()),
            "nh1:" + "c" * 40,
            "nm1:" + "d" * 64,
            [{"subject": '"Movie.mkv" yEnc', "postings": []}],
            42,
            2_000_000_000,
        )
        asset = UsenetAsset(
            bytes.fromhex("e" * 64),
            0,
            "Movie.mkv",
            1_234,
        )
        broker = type(
            "Broker",
            (),
            {
                "ingest_bytes": AsyncMock(return_value=artifact),
                "catalog_artifact": AsyncMock(return_value=(asset,)),
            },
        )()

        class Repository:
            async def persist(self, candidates, **_kwargs):
                candidate = candidates[0]
                locator = candidate.locators[0]
                return {
                    candidate.candidate_id: RenderedCandidateIds(
                        str(uuid.uuid4()),
                        {locator.locator_id: str(uuid.uuid4())},
                    )
                }

        final_repository = type(
            "FinalRepository",
            (),
            {"persist_manual": AsyncMock()},
        )()
        with (
            patch("comet.api.endpoints.nzb.config_check", return_value=config),
            patch("comet.api.endpoints.nzb._codec", return_value=codec),
            patch("comet.api.endpoints.nzb._broker", return_value=broker),
            patch(
                "comet.api.endpoints.nzb.fetch_public_nzb",
                AsyncMock(return_value=b"<nzb/>"),
            ),
            patch(
                "comet.api.endpoints.nzb._enforce_nzb_import_rate",
                AsyncMock(),
            ),
            patch(
                "comet.api.endpoints.nzb._manual_capability_plan",
                AsyncMock(return_value=object()),
            ),
            patch(
                "comet.api.endpoints.nzb.RenderedReleaseRepository",
                return_value=Repository(),
            ),
            patch(
                "comet.api.endpoints.nzb.ReleaseDiscoveryRepository",
                return_value=final_repository,
            ),
            patch(
                "comet.api.endpoints.nzb._manual_provider_options",
                return_value=[],
            ),
            patch(
                "comet.api.endpoints.nzb.settings.CONFIGURE_PAGE_PASSWORD",
                False,
            ),
            patch(
                "comet.api.endpoints.nzb.settings.PUBLIC_BASE_URL",
                "https://comet.example",
            ),
        ):
            response = await import_upload(ImportRequest(), "config")

        body = orjson.loads(response.body)
        self.assertEqual(
            body["assets"],
            [
                {
                    "asset_id": "e" * 64,
                    "file_index": 0,
                    "name": "Movie.mkv",
                    "byte_size": 1_234,
                    "kind": "video",
                }
            ],
        )
        broker.ingest_bytes.assert_awaited_once()
        broker.catalog_artifact.assert_awaited_once_with(artifact)
        final_repository.persist_manual.assert_awaited_once()
        self.assertEqual(
            final_repository.persist_manual.await_args.kwargs["origin_kind"],
            "manual_url",
        )

    async def test_catalog_selection_reuses_the_grant_without_reimporting(self):
        class SelectionRequest:
            headers: ClassVar[dict[str, str]] = {
                "content-type": "application/json",
                "origin": "https://comet.example",
            }
            url = URL("https://comet.example/config/nzb/v1/token/select")

            def __init__(self, candidate_id, asset_id):
                self._body = orjson.dumps(
                    {
                        "candidate_id": candidate_id,
                        "selection_intent": {"assetId": asset_id},
                    }
                )

            async def stream(self):
                yield self._body

        config = {"schemaVersion": 2, "playbackProviders": []}
        codec = CapabilityCodec(ROOT)
        partition = codec.configuration_partition_for_config(config)
        grant_id = str(uuid.uuid4())
        candidate_id = f"manual:{uuid.uuid4()}"
        artifact = NzbArtifact(
            "b" * 64,
            grant_id,
            "nh1:" + "c" * 40,
            "nm1:" + "d" * 64,
            [{"subject": '"Movie.mkv" yEnc', "postings": []}],
            42,
            2_000_000_000,
        )
        asset = UsenetAsset(
            bytes.fromhex("e" * 64),
            0,
            "Movie.mkv",
            1_234,
        )
        broker = type(
            "Broker",
            (),
            {
                "resolve_granted_artifact": AsyncMock(return_value=artifact),
                "catalog_artifact": AsyncMock(return_value=(asset,)),
                "ingest_bytes": AsyncMock(),
            },
        )()
        final_repository = type(
            "FinalRepository",
            (),
            {
                "manual_artifact_origin": AsyncMock(return_value="manual_upload"),
                "persist_manual": AsyncMock(),
            },
        )()

        class RenderedRepository:
            async def persist(self, candidates, **_kwargs):
                candidate = candidates[0]
                locator = candidate.locators[0]
                return {
                    candidate.candidate_id: RenderedCandidateIds(
                        str(uuid.uuid4()),
                        {locator.locator_id: str(uuid.uuid4())},
                    )
                }

        capability = issue_artifact_capability(codec, config, grant_id)
        with (
            patch("comet.api.endpoints.nzb.config_check", return_value=config),
            patch("comet.api.endpoints.nzb._codec", return_value=codec),
            patch("comet.api.endpoints.nzb._broker", return_value=broker),
            patch(
                "comet.api.endpoints.nzb._enforce_nzb_handoff_rate",
                AsyncMock(),
            ),
            patch(
                "comet.api.endpoints.nzb._manual_capability_plan",
                AsyncMock(return_value=object()),
            ),
            patch(
                "comet.api.endpoints.nzb.ReleaseDiscoveryRepository",
                return_value=final_repository,
            ),
            patch(
                "comet.api.endpoints.nzb.RenderedReleaseRepository",
                return_value=RenderedRepository(),
            ),
            patch(
                "comet.api.endpoints.nzb._manual_provider_options",
                return_value=[],
            ),
            patch(
                "comet.api.endpoints.nzb.settings.CONFIGURE_PAGE_PASSWORD",
                False,
            ),
            patch(
                "comet.api.endpoints.nzb.settings.PUBLIC_BASE_URL",
                "https://comet.example",
            ),
        ):
            response = await select_imported_asset(
                SelectionRequest(candidate_id, asset.asset_id.hex()),
                "config",
                capability,
            )

        body = orjson.loads(response.body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["candidate_id"], candidate_id)
        self.assertFalse(body["selection_required"])
        broker.resolve_granted_artifact.assert_awaited_once_with(
            grant_id,
            owner_configuration_partition=partition,
        )
        broker.catalog_artifact.assert_awaited_once_with(artifact)
        broker.ingest_bytes.assert_not_awaited()
        final_repository.manual_artifact_origin.assert_awaited_once_with(
            candidate_id,
            artifact.artifact_sha256,
            owner_configuration_partition=partition,
        )
        final_repository.persist_manual.assert_not_awaited()

    async def test_catalog_selection_requires_one_explicit_asset_id(self):
        class SelectionRequest:
            headers: ClassVar[dict[str, str]] = {
                "content-type": "application/json",
                "origin": "https://comet.example",
            }
            url = URL("https://comet.example/config/nzb/v1/token/select")

            async def stream(self):
                yield orjson.dumps(
                    {
                        "candidate_id": f"manual:{uuid.uuid4()}",
                        "selection_intent": {"season": 1, "episode": 2},
                    }
                )

        config = {"schemaVersion": 2, "playbackProviders": []}
        with (
            patch("comet.api.endpoints.nzb.config_check", return_value=config),
            patch(
                "comet.api.endpoints.nzb._codec",
                return_value=CapabilityCodec(ROOT),
            ),
            patch(
                "comet.api.endpoints.nzb.settings.CONFIGURE_PAGE_PASSWORD",
                False,
            ),
            pytest.raises(HTTPException) as raised,
        ):
            await select_imported_asset(
                SelectionRequest(),
                "config",
                "unread",
            )

        self.assertEqual(raised.value.status_code, 400)
        self.assertEqual(
            raised.value.detail,
            "Expected one catalog asset selection",
        )


class NzbIntentRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_handoff_rate_limit_is_database_coordinated(self):
        with patch(
            "comet.api.endpoints.nzb.ProviderGovernor.acquire_window",
            AsyncMock(return_value=None),
        ) as acquire:
            with self.assertRaises(Exception) as error:
                await _enforce_nzb_handoff_rate(
                    b"a" * 32,
                    "nzb_intent",
                    (str(uuid.uuid4()),),
                )

        self.assertEqual(getattr(error.exception, "status_code", None), 429)
        acquire.assert_awaited_once()

    async def test_import_rate_limit_is_database_coordinated(self):
        with patch(
            "comet.api.endpoints.nzb.ProviderGovernor.acquire_window",
            AsyncMock(return_value=None),
        ) as acquire:
            with self.assertRaises(Exception) as error:
                await _enforce_nzb_import_rate(b"a" * 32)

        self.assertEqual(getattr(error.exception, "status_code", None), 429)
        self.assertEqual(
            getattr(error.exception, "headers", None), {"Retry-After": "60"}
        )
        acquire.assert_awaited_once_with(
            b"a" * 32,
            "nzb_manual_import",
            limit=10,
            window_seconds=60,
        )

    async def test_lazy_handoff_brokers_and_serves_only_the_transformed_artifact(
        self,
    ):
        candidate_id, provider_id, source_locator_id, artifact_locator_id = (
            str(uuid.uuid4()) for _ in range(4)
        )
        config = {
            "schemaVersion": 2,
            "playbackProviders": [
                {
                    "configurationId": provider_id,
                    "kind": "stremio_nntp",
                    "enabled": True,
                    "options": {},
                }
            ],
        }
        codec = CapabilityCodec(ROOT)
        partition = codec.configuration_partition_for_config(config)
        capability = codec.encode(
            "ni2",
            partition=partition,
            suffix=[
                uuid.UUID(candidate_id).bytes,
                uuid.UUID(provider_id).bytes,
                [uuid.UUID(source_locator_id).bytes],
                [0],
                "stremio",
            ],
            ttl=60,
        )
        intent = PlaybackIntent(
            candidate_id,
            provider_id,
            (source_locator_id,),
            (0,),
            "stremio",
        )
        provider = type(
            "Provider",
            (),
            {
                "descriptor": type(
                    "Descriptor",
                    (),
                    {"kind": "stremio_nntp"},
                )()
            },
        )()
        source_release = ResolvedPlaybackIntent(
            candidate_id,
            "usenet",
            "Movie.2024.1080p",
            42,
            (
                {
                    "locator_id": source_locator_id,
                    "kind": "real_nzb",
                    "payload": {
                        "adapter_configuration_id": "origin",
                        "remote_guid": "opaque-guid",
                    },
                    "policy": {
                        "allowed_provider_kinds": ["stremio_nntp"],
                        "exact_provider_configuration_id": None,
                        "expires_at": None,
                        "owner_configuration_partition": partition.hex(),
                    },
                },
            ),
            "tt123",
        )
        resolution = PlaybackIntentResolution(
            intent,
            provider,
            {},
            source_release,
            b"c" * 32,
            "d" * 64,
        )
        artifact_release = ResolvedPlaybackIntent(
            candidate_id,
            "usenet",
            source_release.title,
            42,
            (
                {
                    "locator_id": artifact_locator_id,
                    "kind": "nzb_artifact",
                    "payload": {
                        "artifact_sha256": "a" * 64,
                        "manifest_identity": "nm1:" + "b" * 64,
                    },
                    "policy": source_release.locators[0]["policy"],
                },
            ),
            "tt123",
        )
        artifact = type(
            "Artifact",
            (),
            {
                "manifest": [
                    {
                        "subject": '"Movie.2024.1080p.mkv" yEnc',
                        "postings": [],
                    }
                ]
            },
        )()
        reader = type(
            "Reader",
            (),
            {
                "path": Path("/tmp/artifact.nzb"),
                "byte_size": 42,
                "close": AsyncMock(),
            },
        )()
        broker = type(
            "Broker",
            (),
            {
                "resolve_owned_artifact": AsyncMock(return_value=artifact),
                "acquire_owned_artifact": AsyncMock(return_value=reader),
            },
        )()
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "scheme": "https",
                "server": ("comet.example", 443),
                "path": "/config/nzb/intent/v2/token.nzb",
                "headers": [],
                "client": ("127.0.0.1", 1234),
            }
        )
        adapters = {"origin": object()}

        with (
            patch(
                "comet.api.endpoints.nzb.config_check",
                return_value=config,
            ),
            patch(
                "comet.api.endpoints.nzb.settings.USENET_ENABLED",
                True,
            ),
            patch(
                "comet.api.endpoints.nzb.settings.COMET_CAPABILITY_SECRET",
                ROOT,
            ),
            patch(
                "comet.api.endpoints.nzb.http_client_manager.get_session",
                AsyncMock(return_value=object()),
            ),
            patch(
                "comet.api.endpoints.nzb.resolve_nzb_handoff_intent",
                AsyncMock(return_value=resolution),
            ),
            patch(
                "comet.api.endpoints.nzb._enforce_nzb_handoff_rate",
                AsyncMock(),
            ) as rate_limit,
            patch(
                "comet.api.endpoints.nzb.build_discovery_adapters",
                return_value=adapters,
            ),
            patch(
                "comet.api.endpoints.nzb.broker_nzb_release",
                AsyncMock(return_value=artifact_release),
            ) as transform,
            patch(
                "comet.api.endpoints.nzb._broker",
                return_value=broker,
            ),
        ):
            response = await artifact_intent(
                request,
                "config",
                capability,
            )

        self.assertEqual(response.headers["content-length"], "42")
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        transform.assert_awaited_once()
        rate_limit.assert_awaited_once_with(
            partition,
            "nzb_intent",
            (candidate_id, provider_id, source_locator_id),
        )
        self.assertIs(
            transform.await_args.args[3]["origin"],
            adapters["origin"],
        )
        broker.resolve_owned_artifact.assert_awaited_once_with(
            "a" * 64,
            owner_configuration_partition=partition,
        )
        broker.acquire_owned_artifact.assert_awaited_once_with(
            "a" * 64,
            owner_configuration_partition=partition,
        )
        await response.background()
        reader.close.assert_awaited_once_with()


def test_nzb_upload_rejects_a_gzip_expansion_bomb():
    with pytest.raises(Exception) as error:
        _decode_nzb_document(gzip.compress(b"x" * (17 * 1024 * 1024)))

    assert getattr(error.value, "status_code", None) == 413


def test_stremio_provider_rejects_ambiguous_artifact_selection():
    provider = StremioNntpProvider()
    with pytest.raises(ValueError, match="unambiguous"):
        provider.render_client_delegated(
            {
                "servers": [
                    {
                        "host": "news.example",
                        "port": 563,
                        "tls_mode": "implicit_tls",
                        "username": "user",
                        "password": "password",
                        "connections": 4,
                    }
                ],
            },
            "https://comet.example/nzb",
            [{}, {}],
        )


def test_asset_catalog_is_deterministic_and_omits_postings():
    asset = UsenetAsset(
        bytes.fromhex("a" * 64),
        7,
        "Season 01/Example.S01E02.mkv",
        1_234,
    )
    catalog = _asset_catalog((asset,))

    assert catalog == [
        {
            "asset_id": "a" * 64,
            "file_index": 7,
            "name": "Season 01/Example.S01E02.mkv",
            "byte_size": 1_234,
            "kind": "video",
        }
    ]


class ProviderOptionTests(unittest.IsolatedAsyncioTestCase):
    def test_manual_selection_requires_one_target_or_an_exact_catalog_id(self):
        first = UsenetAsset(bytes.fromhex("a" * 64), 0, "First.mkv", 100)
        second = UsenetAsset(bytes.fromhex("b" * 64), 1, "Second.mkv", 200)

        self.assertEqual(
            _manual_selection_intent(None, (first,)),
            ((2, first.asset_id), first),
        )
        self.assertEqual(
            _manual_selection_intent(None, (first, second)),
            (None, None),
        )
        self.assertEqual(
            _manual_selection_intent(
                {"assetId": second.asset_id.hex(), "extension": True},
                (first, second),
            ),
            ((2, second.asset_id), second),
        )
        malformed_archive = UsenetAsset(
            bytes.fromhex("c" * 64),
            2,
            "not-an-archive.bin",
            300,
            kind="archive",
        )
        with self.assertRaises(FileSelectionError):
            _manual_selection_intent(None, (first, malformed_archive))

    def test_manual_episode_hint_must_resolve_one_catalog_target(self):
        episode = UsenetAsset(
            bytes.fromhex("a" * 64),
            0,
            "Show.S01E02.mkv",
            100,
        )
        other = UsenetAsset(
            bytes.fromhex("b" * 64),
            1,
            "Show.S01E03.mkv",
            100,
        )
        intent = {"season": 1, "episode": 2}

        self.assertEqual(
            _manual_selection_intent(intent, (episode, other)),
            ((1, 1, 2), episode),
        )
        self.assertEqual(
            _manual_selection_intent(intent, (episode, episode)),
            (None, None),
        )

    def test_manual_server_option_is_a_signed_exact_asset_intent(self):
        provider_id = str(uuid.uuid4())
        candidate_row = str(uuid.uuid4())
        locator_row = str(uuid.uuid4())
        partition = b"a" * 32
        asset_id = bytes.fromhex("b" * 64)
        selected_asset = UsenetAsset(asset_id, 0, "Movie.mkv", 100)
        candidate = ReleaseCandidate(
            candidate_id="manual:test",
            media_id="manual:test",
            scope=ReleaseScope.MOVIE,
            transport=TransportKind.USENET,
            title="Movie.mkv",
            locators=(
                NzbArtifactRef(
                    locator_id="manual-nzb:" + "c" * 64,
                    kind=LocatorKind.NZB_ARTIFACT,
                    policy=LocatorPolicy(
                        frozenset({"torbox_usenet"}),
                        owner_configuration_partition=partition,
                    ),
                    artifact_sha256="c" * 64,
                    manifest_identity="nm1:" + "d" * 64,
                ),
            ),
        )
        plan = CapabilityPlan(
            frozenset({TransportKind.USENET}),
            (),
            (EligibleProvider(provider_id, "torbox_usenet", 0),),
            (),
        )
        persisted = RenderedCandidateIds(
            candidate_row,
            {candidate.locators[0].locator_id: locator_row},
        )
        codec = CapabilityCodec(ROOT)

        records = _manual_provider_options(
            _provider_config(provider_id, "torbox_usenet", "Cloud"),
            candidate,
            plan,
            persisted,
            codec,
            partition,
            (2, asset_id),
            selected_asset,
            "https://comet.example/config",
            "https://comet.example/config/nzb",
        )

        self.assertEqual(records[0]["delivery"], "server_resolved")
        token = records[0]["url"].rsplit("/", 1)[1]
        intent = codec.decode_playback_intent(token, partition=partition)
        self.assertEqual(intent.candidate_id, candidate_row)
        self.assertEqual(intent.provider_configuration_id, provider_id)
        self.assertEqual(intent.locator_ids, (locator_row,))
        self.assertEqual(intent.selection_intent, (2, asset_id))

    def test_manual_server_option_requires_selection_before_issuing_pi2(self):
        provider_id = str(uuid.uuid4())
        partition = b"a" * 32
        candidate = _manual_candidate(partition, {"torbox_usenet"})
        plan = CapabilityPlan(
            frozenset({TransportKind.USENET}),
            (),
            (EligibleProvider(provider_id, "torbox_usenet", 0),),
            (),
        )
        records = _manual_provider_options(
            _provider_config(provider_id, "torbox_usenet", "Cloud"),
            candidate,
            plan,
            _rendered_ids(candidate),
            CapabilityCodec(ROOT),
            partition,
            None,
            None,
            "https://comet.example/config",
            "https://comet.example/config/nzb",
        )

        self.assertTrue(records[0]["selection_required"])
        self.assertNotIn("url", records[0])

    def test_manual_stremio_option_pins_the_selected_direct_file(self):
        provider_id = str(uuid.uuid4())
        partition = b"a" * 32
        candidate = _manual_candidate(partition, {"stremio_nntp"})
        plan = CapabilityPlan(
            frozenset({TransportKind.USENET}),
            (),
            (EligibleProvider(provider_id, "stremio_nntp", 0),),
            (),
        )
        selected = UsenetAsset(bytes.fromhex("e" * 64), 7, "Movie.mkv", 100)
        records = _manual_provider_options(
            _provider_config(
                provider_id,
                "stremio_nntp",
                "Client",
                options={
                    "servers": [
                        {
                            "host": "news.example",
                            "port": 563,
                            "tls_mode": "implicit_tls",
                            "username": "user",
                            "password": "password",
                            "connections": 4,
                        }
                    ],
                },
            ),
            candidate,
            plan,
            _rendered_ids(candidate),
            CapabilityCodec(ROOT),
            partition,
            (2, selected.asset_id),
            selected,
            "https://comet.example/config",
            "https://comet.example/config/nzb",
        )

        self.assertEqual(
            records[0]["stream"],
            {
                "nzbUrl": "https://comet.example/config/nzb",
                "servers": ["nntps://user:password@news.example:563/4"],
                "fileIdx": 7,
                "name": "[Client] Comet",
                "description": "Movie.mkv",
            },
        )

    def test_manual_stremio_option_never_uses_an_archive_volume_file_index(self):
        provider_id = str(uuid.uuid4())
        partition = b"a" * 32
        candidate = _manual_candidate(partition, {"stremio_nntp"})
        plan = CapabilityPlan(
            frozenset({TransportKind.USENET}),
            (),
            (EligibleProvider(provider_id, "stremio_nntp", 0),),
            (),
        )
        selected = UsenetAsset(
            bytes.fromhex("e" * 64),
            7,
            "Movie.part01.rar",
            100,
            kind="archive",
        )
        records = _manual_provider_options(
            _provider_config(provider_id, "stremio_nntp", "Client"),
            candidate,
            plan,
            _rendered_ids(candidate),
            CapabilityCodec(ROOT),
            partition,
            (2, selected.asset_id),
            selected,
            "https://comet.example/config",
            "https://comet.example/config/nzb",
        )

        self.assertTrue(records[0]["client_handoff_unavailable"])
        self.assertNotIn("stream", records[0])


@pytest.mark.parametrize("prefix", ["pi2", "pa2", "ni2"])
def test_nzb_artifact_route_rejects_other_capability_audiences(prefix):
    codec = CapabilityCodec(ROOT)
    partition = codec.configuration_partition(b"normalized")
    suffix = [uuid.uuid4().bytes]
    if prefix in {"pi2", "ni2"}:
        suffix = [
            uuid.uuid4().bytes,
            uuid.uuid4().bytes,
            [uuid.uuid4().bytes],
            [0],
            "stremio",
        ]
    token = codec.encode(prefix, partition=partition, suffix=suffix, ttl=60, now=100)

    with pytest.raises(ValueError):
        _artifact_grant_from_token(token, partition, codec)
