import base64
import hashlib
import unittest
from dataclasses import dataclass, replace
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import urlsplit

import orjson
from databases import Database
from httpx import ASGITransport, AsyncClient

from comet.core.capabilities import CapabilityPlan, CapabilityPlanner
from comet.core.capability_bindings import build_playback_capability_bindings
from comet.core.capability_states import (
    CapabilityStateRepository,
    EffectiveCapabilityState,
)
from comet.core.config_validation import config_check
from comet.core.db_router import ReplicaAwareDatabase
from comet.core.schema_migrations import (
    MigrationContext,
    _ensure_operations_schema,
    _ensure_usenet_schema,
    _migration_foundation,
    _upgrade_download_link_cache,
)
from comet.discovery.manager import DiscoveryResult
from comet.discovery.models import DiscoveryBatch, DiscoveryContext, MediaQuery
from comet.discovery.registry import build_discovery_adapters
from comet.discovery.repository import ReleaseDiscoveryRepository
from comet.metadata.manager import MetadataFetchResult, MetadataFetchStatus
from comet.playback.base import Actionability, ProviderStatus, Readiness
from comet.playback.presentation import build_provider_options
from comet.playback.providers.altmount import (
    AltMountRemoteFile,
    AltMountSelectedFile,
    AltMountStream,
)
from comet.playback.providers.nzbdav import (
    NzbDavError,
    NzbDavJob,
    NzbDavWebDavEntry,
)
from comet.playback.providers.stremthru_newz import (
    StremThruGeneratedLink,
    StremThruNewzError,
    StremThruNewzFile,
    StremThruNewzRemoteItem,
    StremThruNewzSubmission,
)
from comet.playback.repository import RenderedReleaseRepository
from comet.playback.tokens import CapabilityCodec
from comet.usenet.access import NativeAccessAuthorizer
from comet.usenet.nzb_broker import NzbBroker

_SECRET = "YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE"
_PROVIDER_ID = "11111111-1111-4111-8111-111111111111"
_SOURCE_ID = "22222222-2222-4222-8222-222222222222"
_TORBOX_PROVIDER_ID = "33333333-3333-4333-8333-333333333333"
_NZBDAV_PROVIDER_ID = "55555555-5555-4555-8555-555555555555"
_NEWZNAB_SOURCE_ID = "66666666-6666-4666-8666-666666666666"
_NATIVE_PROVIDER_ID = "88888888-8888-4888-8888-888888888888"
_STREMTHRU_PROVIDER_ID = "99999999-9999-4999-8999-999999999999"
_ALTMOUNT_PROVIDER_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_STREMIO_NNTP_PROVIDER_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _metadata_result() -> MetadataFetchResult:
    return MetadataFetchResult(
        MetadataFetchStatus.OK,
        {
            "title": "Example",
            "year": 2026,
            "year_end": None,
            "season": None,
            "episode": None,
        },
        {},
    )


async def _persist_canonical_candidate(
    database,
    candidate,
    *,
    partition: bytes,
    discovery_configuration_id: str,
):
    context = MigrationContext(
        database,
        is_sqlite=True,
        is_postgres=False,
    )
    await _ensure_usenet_schema(context)
    query = MediaQuery(
        candidate.media_id,
        "movie",
        title_aliases=(candidate.title,),
    )
    branch_fingerprint = hashlib.sha256(discovery_configuration_id.encode()).hexdigest()
    stored = (
        await ReleaseDiscoveryRepository(database).persist_success(
            query,
            branch_fingerprint,
            (candidate,),
            discovery_configuration_id=discovery_configuration_id,
            owner_configuration_partition=partition,
            next_refresh_at=120,
            now=10,
        )
    )[candidate.candidate_id]
    return replace(
        candidate,
        candidate_id=stored.candidate_id,
        locators=tuple(
            replace(
                locator,
                locator_id=stored.locator_ids[locator.locator_id],
            )
            for locator in candidate.locators
        ),
    )


class _DiscoveryResponse:
    def __init__(self, payload):
        self.status = 200
        self._body = orjson.dumps(payload)
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

    async def read(self, _maximum=None):
        if self._read:
            return b""
        self._read = True
        return self._body


class _DiscoverySession:
    def __init__(self, payload):
        self._payload = payload

    def get(self, *_args, **_kwargs):
        return _DiscoveryResponse(self._payload)


_NEWZNAB_CAPS = b"""<?xml version="1.0"?>
<caps>
  <searching>
    <search available="yes" supportedParams="q"/>
    <movie-search available="yes" supportedParams="q,imdbid,year"/>
  </searching>
  <categories><category id="2000" name="Movies"/></categories>
</caps>"""
_NEWZNAB_RESULTS = b"""<?xml version="1.0"?>
<rss xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">
  <channel>
    <newznab:response offset="0" total="1"/>
    <item>
      <title>Example.2026.1080p</title>
      <guid>https://indexer.example/get?id=opaque-guid&amp;apikey=secret</guid>
      <enclosure url="https://indexer.example/get?id=opaque-guid&amp;apikey=secret"
        length="99" type="application/x-nzb"/>
      <newznab:attr name="size" value="99"/>
    </item>
  </channel>
</rss>"""
_NEWZNAB_GRAB = b'<?xml version="1.0"?><nzb></nzb>'


class _XmlResponse:
    status = 200

    def __init__(self, body: bytes):
        self._body = body
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def iter_chunked(self, size):
        for offset in range(0, len(self._body), size):
            yield self._body[offset : offset + size]


class _NewznabSession:
    def __init__(self):
        self.operations = []

    def get(self, _url, **kwargs):
        operation = kwargs["params"]["t"]
        self.operations.append(operation)
        return _XmlResponse(
            _NEWZNAB_CAPS
            if operation == "caps"
            else _NEWZNAB_GRAB
            if operation == "get"
            else _NEWZNAB_RESULTS
        )


def _artifact_parse_result() -> dict:
    return {
        "version": 2,
        "files": 1,
        "segments": 1,
        "nh1": "nh1:" + "a" * 40,
        "nm1": "nm1:" + "b" * 64,
        "metadata": {},
        "manifest": [
            {
                "subject": '"Example.2026.1080p.mkv" yEnc',
                "groups": ["alt.binaries.example"],
                "postings": [
                    {
                        "number": 1,
                        "bytes": 99,
                        "message_id": "example@usenet.invalid",
                    }
                ],
            }
        ],
    }


class _ArtifactEngine:
    async def parse_nzb(self, _artifact_sha256, _document):
        return _artifact_parse_result()


def _native_asset(artifact_sha256: str) -> dict:
    path = "Example.2026.1080p.mkv"
    digest = hashlib.sha256()
    digest.update(b"comet-nzb-asset-v1\0")
    digest.update(bytes.fromhex(artifact_sha256))
    digest.update((0).to_bytes(4, "big"))
    encoded_path = path.encode()
    digest.update(len(encoded_path).to_bytes(4, "big"))
    digest.update(encoded_path)
    return {
        "asset_id": digest.hexdigest(),
        "file_index": 0,
        "relative_path": path,
        "declared_bytes": 99,
        "kind": "video",
    }


def _principal_config(
    provider_id: str,
    display_name: str,
    kind: str,
    options: dict,
    *,
    native_access_token: str | None = None,
) -> dict:
    config = {
        "schemaVersion": 2,
        "enabledTransports": ["usenet"],
        "playbackProviders": [
            {
                "configurationId": provider_id,
                "displayName": display_name,
                "kind": kind,
                "enabled": True,
                "options": options,
            }
        ],
        "discoverySources": [
            {
                "configurationId": _NEWZNAB_SOURCE_ID,
                "kind": "newznab",
                "enabled": True,
                "options": {
                    "endpoint": "https://indexer.example/api",
                    "apiKey": "indexer-key",
                    "userAgentMode": "custom",
                    "queryUserAgent": "Comet",
                    "grabUserAgent": "Comet",
                },
            }
        ],
    }
    if native_access_token is not None:
        config["nativeAccessToken"] = native_access_token
    return config


@dataclass(frozen=True, slots=True)
class _PrincipalDiscovery:
    b64config: str
    config: dict
    codec: CapabilityCodec
    partition: bytes
    session: _NewznabSession
    discovered: DiscoveryBatch
    plan: CapabilityPlan


async def _discover_principal(
    config: dict,
    provider_id: str,
    *,
    native_authorizer: NativeAccessAuthorizer | None = None,
    native_engine_enabled: bool = False,
    native_instance_pool_available: bool = False,
) -> _PrincipalDiscovery:
    b64config = base64.urlsafe_b64encode(orjson.dumps(config)).decode().rstrip("=")
    normalized = config_check(b64config)
    if normalized is None:
        raise AssertionError("principal-path configuration was rejected")
    if native_authorizer is None:
        native_authorizer = NativeAccessAuthorizer(None)
    codec = CapabilityCodec(_SECRET)
    partition = codec.configuration_partition_for_config(normalized)
    session = _NewznabSession()
    adapters = build_discovery_adapters(
        normalized,
        session,
        account_partition=partition,
    )
    discovered = await adapters[_NEWZNAB_SOURCE_ID].search(
        MediaQuery(
            "tt1234567",
            "movie",
            title_aliases=("Example 2026",),
        ),
        DiscoveryContext(
            frozenset({"usenet"}),
            partition,
            _NEWZNAB_SOURCE_ID,
        ),
    )
    plan = CapabilityPlanner(
        usenet_offered=True,
        native_authorizer=native_authorizer,
        native_engine_enabled=native_engine_enabled,
        native_instance_pool_available=native_instance_pool_available,
    ).build(normalized)
    options = build_provider_options(discovered.candidates, plan)
    if len(options) != 1 or options[0].provider.configuration_id != provider_id:
        raise AssertionError("principal-path provider is not reachable")
    return _PrincipalDiscovery(
        b64config,
        normalized,
        codec,
        partition,
        session,
        discovered,
        plan,
    )


class UsenetPrincipalPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_nzb_candidate_reaches_nzbdav_direct_redirect(self):
        fixture = await _discover_principal(
            _principal_config(
                _NZBDAV_PROVIDER_ID,
                "NzbDAV",
                "nzbdav",
                {
                    "internalBaseUrl": "https://nzbdav.example",
                    "sabApiKey": "sab-key",
                    "webdavUsername": "webdav-user",
                    "webdavPassword": "webdav-pass",
                    "movieCategory": "movies",
                    "seriesCategory": "tv",
                },
            ),
            _NZBDAV_PROVIDER_ID,
        )
        b64config = fixture.b64config
        config = fixture.config
        codec = fixture.codec
        partition = fixture.partition
        candidate = fixture.discovered.candidates[0]
        plan = fixture.plan
        self.assertEqual(
            candidate.locators[0].adapter_configuration_id,
            _NEWZNAB_SOURCE_ID,
        )
        job_id = "77777777-7777-4777-8777-777777777777"

        with TemporaryDirectory() as temporary:
            artifact_directory = f"{temporary}/artifacts"
            database = ReplicaAwareDatabase(
                Database(f"sqlite+aiosqlite:///{temporary}/nzbdav-path.db")
            )
            await database.connect()
            try:
                context = MigrationContext(
                    database,
                    is_sqlite=True,
                    is_postgres=False,
                )
                await _migration_foundation(context)
                await _ensure_usenet_schema(context)
                candidate = await _persist_canonical_candidate(
                    database,
                    candidate,
                    partition=partition,
                    discovery_configuration_id=_NEWZNAB_SOURCE_ID,
                )
                binding = build_playback_capability_bindings(
                    config,
                    codec,
                    provider_configuration_ids=frozenset({_NZBDAV_PROVIDER_ID}),
                )[0].binding
                await CapabilityStateRepository(database).record_success(
                    binding,
                )
                broker = NzbBroker(
                    artifact_directory,
                    database,
                    _ArtifactEngine(),
                )
                artifact = await broker.ingest_bytes(
                    b'<?xml version="1.0"?><nzb></nzb>',
                    owner_configuration_partition=partition,
                )
                repository = RenderedReleaseRepository(database)
                persisted = (
                    await repository.persist(
                        (candidate,),
                        owner_configuration_partition=partition,
                    )
                )[candidate.candidate_id]
                await repository.attach_brokered_artifact(
                    persisted.candidate_id,
                    persisted.locator_ids[candidate.locators[0].locator_id],
                    artifact.artifact_sha256,
                    artifact.nm1,
                    owner_configuration_partition=partition,
                )
                completed = NzbDavJob(
                    job_id,
                    ProviderStatus(
                        Readiness.READY,
                        Actionability.SERVER_ON_DEMAND,
                    ),
                    verified_name=(f"comet-{artifact.artifact_sha256}"),
                )
                selected = NzbDavWebDavEntry(
                    "Example.2026.1080p.mkv",
                    99,
                    False,
                )
                download_target = (
                    "https://webdav-user:webdav-pass@nzbdav.example/"
                    "content/movies/"
                    f"comet-{artifact.artifact_sha256}/"
                    "Example.2026.1080p.mkv"
                )
                from comet.api.app import app

                with (
                    patch(
                        "comet.api.endpoints.usenet_playback.database",
                        database,
                    ),
                    patch(
                        "comet.services.media_search.database",
                        database,
                    ),
                    patch(
                        "comet.api.endpoints.usenet_playback.settings.USENET_ENABLED",
                        True,
                    ),
                    patch(
                        "comet.api.endpoints.usenet_playback.settings."
                        "COMET_CAPABILITY_SECRET",
                        _SECRET,
                    ),
                    patch(
                        "comet.api.endpoints.usenet_playback.settings."
                        "USENET_ARTIFACT_DIR",
                        artifact_directory,
                    ),
                    patch(
                        "comet.api.endpoints.usenet_playback.http_client_manager."
                        "get_session",
                        AsyncMock(return_value=_NewznabSession()),
                    ),
                    patch(
                        "comet.services.media_search.MetadataScraper."
                        "fetch_metadata_and_aliases",
                        AsyncMock(return_value=_metadata_result()),
                    ),
                    patch(
                        "comet.services.media_search._search_configured_sources",
                        AsyncMock(
                            return_value=DiscoveryResult(
                                (candidate,),
                                (),
                                plan,
                            )
                        ),
                    ),
                    patch(
                        "comet.playback.providers.nzbdav."
                        "NzbDavProvider.submit_artifact",
                        AsyncMock(return_value=job_id),
                    ) as submit,
                    patch(
                        "comet.playback.providers.nzbdav.NzbDavProvider.poll_artifact",
                        AsyncMock(
                            side_effect=[
                                NzbDavError(
                                    "nzbdav_unavailable",
                                    retryable=True,
                                ),
                                completed,
                            ]
                        ),
                    ) as poll,
                    patch(
                        "comet.playback.providers.nzbdav.NzbDavProvider.completed_file",
                        AsyncMock(return_value=selected),
                    ) as select,
                    patch(
                        "comet.playback.providers.nzbdav."
                        "NzbDavProvider.direct_download_url",
                        Mock(return_value=download_target),
                    ),
                ):
                    async with AsyncClient(
                        transport=ASGITransport(app=app),
                        base_url="https://comet.example",
                    ) as client:
                        stream_response = await client.get(
                            f"/{b64config}/stream/movie/tt1234567.json",
                        )
                        streams = stream_response.json()["streams"]
                        playback_url = streams[0]["url"]
                        pi2 = playback_url.rsplit("/", 1)[1]
                        intent_redirect = await client.get(
                            urlsplit(playback_url).path,
                            follow_redirects=False,
                        )
                        pa2 = urlsplit(intent_redirect.headers["location"]).path.rsplit(
                            "/", 1
                        )[1]
                        preparing = await client.get(
                            f"/{b64config}/playback/v2/{pa2}",
                        )
                        retrying = await client.get(
                            f"/{b64config}/playback/v2/{pa2}",
                        )
                        response = await client.get(
                            f"/{b64config}/playback/v2/{pa2}",
                            headers={"Range": "bytes=10-19"},
                        )

                self.assertEqual(stream_response.status_code, 200)
                self.assertEqual(len(streams), 1)
                self.assertEqual(streams[0]["name"], "[NzbDAV📰] Comet 1080p")
                self.assertTrue(pi2.startswith("pi2."))
                self.assertEqual(intent_redirect.status_code, 307)
                self.assertEqual(preparing.status_code, 200)
                self.assertEqual(retrying.status_code, 307)
                self.assertEqual(
                    retrying.headers["cache-control"],
                    "private, no-store",
                )
                self.assertEqual(
                    retrying.headers["referrer-policy"],
                    "no-referrer",
                )
                self.assertEqual(retrying.headers["location"], download_target)
                self.assertEqual(response.status_code, 307)
                self.assertEqual(
                    response.headers["location"],
                    download_target,
                )
                submit.assert_awaited_once()
                self.assertEqual(poll.await_count, 2)
                select.assert_awaited_once()

                capability_row = await database.fetch_one(
                    """
                    SELECT state, last_success_at, error_code, retry_after
                    FROM capability_validation_states
                    WHERE binding_fingerprint = :binding_fingerprint
                    """,
                    {
                        "binding_fingerprint": binding.binding_fingerprint,
                    },
                    force_primary=True,
                )
                self.assertEqual(
                    capability_row["state"],
                    "transiently_unreachable",
                )
                self.assertIsNotNone(capability_row["last_success_at"])
                self.assertEqual(
                    capability_row["error_code"],
                    "nzbdav_unavailable",
                )
                self.assertEqual(capability_row["retry_after"], 30)
            finally:
                await database.disconnect()

    async def test_real_nzb_candidate_reaches_stremthru_after_transient_retry(self):
        fixture = await _discover_principal(
            _principal_config(
                _STREMTHRU_PROVIDER_ID,
                "StremThru Newz",
                "stremthru_newz",
                {
                    "baseUrl": "https://bridge.example",
                    "authToken": "user:pass",
                    "allowedMediaOrigins": ["https://cdn.example"],
                },
            ),
            _STREMTHRU_PROVIDER_ID,
        )
        b64config = fixture.b64config
        config = fixture.config
        codec = fixture.codec
        partition = fixture.partition
        candidate = fixture.discovered.candidates[0]
        plan = fixture.plan

        with TemporaryDirectory() as temporary:
            artifact_directory = f"{temporary}/artifacts"
            database = ReplicaAwareDatabase(
                Database(f"sqlite+aiosqlite:///{temporary}/stremthru-path.db")
            )
            await database.connect()
            try:
                context = MigrationContext(
                    database,
                    is_sqlite=True,
                    is_postgres=False,
                )
                await _migration_foundation(context)
                await _upgrade_download_link_cache(context)
                await _ensure_usenet_schema(context)
                candidate = await _persist_canonical_candidate(
                    database,
                    candidate,
                    partition=partition,
                    discovery_configuration_id=_NEWZNAB_SOURCE_ID,
                )
                broker = NzbBroker(
                    artifact_directory,
                    database,
                    _ArtifactEngine(),
                )
                artifact = await broker.ingest_bytes(
                    b'<?xml version="1.0"?><nzb></nzb>',
                    owner_configuration_partition=partition,
                )
                repository = RenderedReleaseRepository(database)
                persisted = (
                    await repository.persist(
                        (candidate,),
                        owner_configuration_partition=partition,
                    )
                )[candidate.candidate_id]
                await repository.attach_brokered_artifact(
                    persisted.candidate_id,
                    persisted.locator_ids[candidate.locators[0].locator_id],
                    artifact.artifact_sha256,
                    artifact.nm1,
                    owner_configuration_partition=partition,
                )
                binding = build_playback_capability_bindings(
                    config,
                    codec,
                    provider_configuration_ids=frozenset({_STREMTHRU_PROVIDER_ID}),
                )[0].binding
                await CapabilityStateRepository(database).record_success(
                    binding,
                )
                submission = StremThruNewzSubmission(
                    "remote-1",
                    "hash-1",
                    "processing",
                )
                selected = StremThruNewzFile(
                    3,
                    "Example.2026.1080p.mkv",
                    99,
                    "locked-ref",
                )
                completed = StremThruNewzRemoteItem(
                    "remote-1",
                    "hash-1",
                    "downloaded",
                    (selected,),
                    False,
                )
                generated = StremThruGeneratedLink(
                    "https://cdn.example/video?signature=signed",
                )
                from comet.api.app import app

                with (
                    patch(
                        "comet.api.endpoints.usenet_playback.database",
                        database,
                    ),
                    patch(
                        "comet.services.media_search.database",
                        database,
                    ),
                    patch(
                        "comet.api.endpoints.usenet_playback.settings.USENET_ENABLED",
                        True,
                    ),
                    patch(
                        "comet.api.endpoints.usenet_playback.settings."
                        "COMET_CAPABILITY_SECRET",
                        _SECRET,
                    ),
                    patch(
                        "comet.api.endpoints.usenet_playback.settings."
                        "USENET_ARTIFACT_DIR",
                        artifact_directory,
                    ),
                    patch(
                        "comet.api.endpoints.usenet_playback.settings.PUBLIC_BASE_URL",
                        "https://comet.example",
                    ),
                    patch(
                        "comet.api.endpoints.usenet_playback."
                        "http_client_manager.get_session",
                        AsyncMock(return_value=_NewznabSession()),
                    ),
                    patch(
                        "comet.services.media_search.MetadataScraper."
                        "fetch_metadata_and_aliases",
                        AsyncMock(return_value=_metadata_result()),
                    ),
                    patch(
                        "comet.services.media_search._search_configured_sources",
                        AsyncMock(
                            return_value=DiscoveryResult(
                                (candidate,),
                                (),
                                plan,
                            )
                        ),
                    ),
                    patch(
                        "comet.playback.providers.stremthru_newz."
                        "StremThruNewzProvider.submit_export",
                        AsyncMock(
                            side_effect=[
                                StremThruNewzError(
                                    "stremthru_busy",
                                    retryable=True,
                                    retry_after=1,
                                    mutation_rejected=True,
                                ),
                                submission,
                            ]
                        ),
                    ) as submit,
                    patch(
                        "comet.playback.providers.stremthru_newz."
                        "StremThruNewzProvider.get_item",
                        AsyncMock(
                            side_effect=[
                                StremThruNewzError(
                                    "stremthru_rate_limited",
                                    retryable=True,
                                    retry_after=9,
                                ),
                                completed,
                            ]
                        ),
                    ) as get_item,
                    patch(
                        "comet.playback.providers.stremthru_newz."
                        "StremThruNewzProvider.generate_link",
                        AsyncMock(return_value=generated),
                    ) as generate_link,
                ):
                    async with AsyncClient(
                        transport=ASGITransport(app=app),
                        base_url="https://comet.example",
                    ) as client:
                        stream_response = await client.get(
                            f"/{b64config}/stream/movie/tt1234567.json",
                        )
                        streams = stream_response.json()["streams"]
                        playback_url = streams[0]["url"]
                        pi2 = playback_url.rsplit("/", 1)[1]
                        intent_redirect = await client.get(
                            urlsplit(playback_url).path,
                            follow_redirects=False,
                        )
                        pa2 = urlsplit(intent_redirect.headers["location"]).path.rsplit(
                            "/", 1
                        )[1]
                        preparing = await client.get(
                            f"/{b64config}/playback/v2/{pa2}",
                        )
                        submitted = await client.get(
                            f"/{b64config}/playback/v2/{pa2}",
                        )
                        retrying = await client.get(
                            f"/{b64config}/playback/v2/{pa2}",
                        )
                        response = await client.get(
                            f"/{b64config}/playback/v2/{pa2}",
                            headers={"Range": "bytes=10-19"},
                        )

                self.assertEqual(stream_response.status_code, 200)
                self.assertEqual(len(streams), 1)
                self.assertEqual(
                    streams[0]["name"],
                    "[StremThru Newz📰] Comet 1080p",
                )
                self.assertTrue(pi2.startswith("pi2."))
                self.assertEqual(intent_redirect.status_code, 307)
                self.assertEqual(preparing.status_code, 200)
                self.assertEqual(submitted.status_code, 200)
                self.assertEqual(retrying.status_code, 307)
                self.assertEqual(retrying.headers["location"], generated.url)
                self.assertEqual(response.status_code, 307)
                self.assertEqual(
                    response.headers["location"],
                    generated.url,
                )
                self.assertEqual(submit.await_count, 2)
                export_url = submit.await_args_list[-1].args[0]
                self.assertRegex(
                    export_url,
                    r"^https://comet\.example/nzb/export/v1/"
                    r"nx1\.[0-9a-f]{32}\.nzb$",
                )
                self.assertEqual(get_item.await_count, 2)
                generate_link.assert_awaited_once_with("locked-ref")

                capability_row = await database.fetch_one(
                    """
                    SELECT state, last_success_at, error_code, retry_after
                    FROM capability_validation_states
                    WHERE binding_fingerprint = :binding_fingerprint
                    """,
                    {
                        "binding_fingerprint": binding.binding_fingerprint,
                    },
                    force_primary=True,
                )
                self.assertEqual(
                    capability_row["state"],
                    "transiently_unreachable",
                )
                self.assertIsNotNone(capability_row["last_success_at"])
                self.assertEqual(
                    capability_row["error_code"],
                    "stremthru_rate_limited",
                )
                self.assertEqual(capability_row["retry_after"], 9)
            finally:
                await database.disconnect()

    async def test_real_nzb_candidate_reaches_altmount_direct_redirect(self):
        fixture = await _discover_principal(
            _principal_config(
                _ALTMOUNT_PROVIDER_ID,
                "AltMount",
                "altmount",
                {
                    "internalBaseUrl": "https://altmount.example",
                    "streamBaseUrl": "https://stream.altmount.example",
                    "apiKey": "altmount-key",
                    "category": "stremio",
                },
            ),
            _ALTMOUNT_PROVIDER_ID,
        )
        b64config = fixture.b64config
        config = fixture.config
        codec = fixture.codec
        partition = fixture.partition
        candidate = fixture.discovered.candidates[0]
        plan = fixture.plan

        with TemporaryDirectory() as temporary:
            artifact_directory = f"{temporary}/artifacts"
            database = ReplicaAwareDatabase(
                Database(f"sqlite+aiosqlite:///{temporary}/altmount-path.db")
            )
            await database.connect()
            try:
                context = MigrationContext(
                    database,
                    is_sqlite=True,
                    is_postgres=False,
                )
                await _migration_foundation(context)
                await _ensure_usenet_schema(context)
                candidate = await _persist_canonical_candidate(
                    database,
                    candidate,
                    partition=partition,
                    discovery_configuration_id=_NEWZNAB_SOURCE_ID,
                )
                broker = NzbBroker(
                    artifact_directory,
                    database,
                    _ArtifactEngine(),
                )
                artifact = await broker.ingest_bytes(
                    b'<?xml version="1.0"?><nzb></nzb>',
                    owner_configuration_partition=partition,
                )
                repository = RenderedReleaseRepository(database)
                persisted = (
                    await repository.persist(
                        (candidate,),
                        owner_configuration_partition=partition,
                    )
                )[candidate.candidate_id]
                await repository.attach_brokered_artifact(
                    persisted.candidate_id,
                    persisted.locator_ids[candidate.locators[0].locator_id],
                    artifact.artifact_sha256,
                    artifact.nm1,
                    owner_configuration_partition=partition,
                )
                binding = build_playback_capability_bindings(
                    config,
                    codec,
                    provider_configuration_ids=frozenset({_ALTMOUNT_PROVIDER_ID}),
                )[0].binding
                await CapabilityStateRepository(database).record_success(
                    binding,
                )
                submission = AltMountStream(
                    (AltMountRemoteFile("Example.2026.1080p.mkv"),)
                )
                selected = AltMountSelectedFile("Example.2026.1080p.mkv")
                download_target = (
                    "https://stream.altmount.example/api/files/stream"
                    "?path=Example.2026.1080p.mkv&download_key=request-local"
                )
                from comet.api.app import app

                with (
                    patch(
                        "comet.api.endpoints.usenet_playback.database",
                        database,
                    ),
                    patch(
                        "comet.services.media_search.database",
                        database,
                    ),
                    patch(
                        "comet.api.endpoints.usenet_playback.settings.USENET_ENABLED",
                        True,
                    ),
                    patch(
                        "comet.api.endpoints.usenet_playback.settings."
                        "COMET_CAPABILITY_SECRET",
                        _SECRET,
                    ),
                    patch(
                        "comet.api.endpoints.usenet_playback.settings."
                        "USENET_ARTIFACT_DIR",
                        artifact_directory,
                    ),
                    patch(
                        "comet.api.endpoints.usenet_playback."
                        "http_client_manager.get_session",
                        AsyncMock(return_value=_NewznabSession()),
                    ),
                    patch(
                        "comet.services.media_search.MetadataScraper."
                        "fetch_metadata_and_aliases",
                        AsyncMock(return_value=_metadata_result()),
                    ),
                    patch(
                        "comet.services.media_search._search_configured_sources",
                        AsyncMock(
                            return_value=DiscoveryResult(
                                (candidate,),
                                (),
                                plan,
                            )
                        ),
                    ),
                    patch(
                        "comet.playback.providers.altmount."
                        "AltMountProvider.submit_artifact",
                        AsyncMock(return_value=submission),
                    ) as submit,
                    patch(
                        "comet.playback.providers.altmount."
                        "AltMountProvider.select_file",
                        Mock(return_value=selected),
                    ) as resolve_file,
                    patch(
                        "comet.playback.providers.altmount.AltMountProvider.stream_url",
                        Mock(return_value=download_target),
                    ) as stream_target,
                ):
                    async with AsyncClient(
                        transport=ASGITransport(app=app),
                        base_url="https://comet.example",
                    ) as client:
                        stream_response = await client.get(
                            f"/{b64config}/stream/movie/tt1234567.json",
                        )
                        streams = stream_response.json()["streams"]
                        playback_url = streams[0]["url"]
                        pi2 = playback_url.rsplit("/", 1)[1]
                        intent_redirect = await client.get(
                            urlsplit(playback_url).path,
                            follow_redirects=False,
                        )
                        pa2 = urlsplit(intent_redirect.headers["location"]).path.rsplit(
                            "/", 1
                        )[1]
                        retrying = await client.get(
                            f"/{b64config}/playback/v2/{pa2}",
                        )
                        response = await client.get(
                            f"/{b64config}/playback/v2/{pa2}",
                            headers={"Range": "bytes=10-19"},
                        )

                self.assertEqual(stream_response.status_code, 200)
                self.assertEqual(len(streams), 1)
                self.assertEqual(streams[0]["name"], "[AltMount📰] Comet 1080p")
                self.assertTrue(pi2.startswith("pi2."))
                self.assertEqual(intent_redirect.status_code, 307)
                self.assertEqual(retrying.status_code, 307)
                self.assertEqual(retrying.headers["location"], download_target)
                self.assertEqual(response.status_code, 307)
                self.assertEqual(
                    response.headers["location"],
                    download_target,
                )
                submit.assert_awaited_once()
                resolve_file.assert_called_once()
                self.assertEqual(stream_target.call_count, 2)

            finally:
                await database.disconnect()

    async def test_real_nzb_candidate_reaches_reusable_stremio_nntp_handoff(self):
        fixture = await _discover_principal(
            _principal_config(
                _STREMIO_NNTP_PROVIDER_ID,
                "Stremio NNTP",
                "stremio_nntp",
                {
                    "servers": [
                        {
                            "host": "news.example.test",
                            "port": 563,
                            "tls_mode": "implicit_tls",
                            "username": "member",
                            "password": "secret",
                            "connections": 2,
                        }
                    ],
                },
            ),
            _STREMIO_NNTP_PROVIDER_ID,
        )
        b64config = fixture.b64config
        config = fixture.config
        codec = fixture.codec
        partition = fixture.partition
        session = fixture.session
        discovered = fixture.discovered
        plan = fixture.plan

        with TemporaryDirectory() as temporary:
            artifact_directory = f"{temporary}/artifacts"
            database = ReplicaAwareDatabase(
                Database(f"sqlite+aiosqlite:///{temporary}/handoff-path.db")
            )
            await database.connect()
            try:
                context = MigrationContext(
                    database,
                    is_sqlite=True,
                    is_postgres=False,
                )
                await _migration_foundation(context)
                await _ensure_usenet_schema(context)
                candidate = await _persist_canonical_candidate(
                    database,
                    discovered.candidates[0],
                    partition=partition,
                    discovery_configuration_id=_NEWZNAB_SOURCE_ID,
                )
                binding = build_playback_capability_bindings(
                    config,
                    codec,
                    provider_configuration_ids=frozenset({_STREMIO_NNTP_PROVIDER_ID}),
                )[0].binding
                await CapabilityStateRepository(database).record_success(
                    binding,
                )
                engine = type(
                    "Engine",
                    (),
                    {
                        "parse_nzb": AsyncMock(return_value=_artifact_parse_result()),
                    },
                )()
                from comet.api.app import app

                with (
                    patch(
                        "comet.api.endpoints.nzb.database",
                        database,
                    ),
                    patch(
                        "comet.api.endpoints.stream.database",
                        database,
                    ),
                    patch(
                        "comet.services.media_search.database",
                        database,
                    ),
                    patch(
                        "comet.api.endpoints.nzb.settings.USENET_ENABLED",
                        True,
                    ),
                    patch(
                        "comet.api.endpoints.nzb.settings.COMET_CAPABILITY_SECRET",
                        _SECRET,
                    ),
                    patch(
                        "comet.api.endpoints.nzb.settings.USENET_ARTIFACT_DIR",
                        artifact_directory,
                    ),
                    patch(
                        "comet.api.endpoints.nzb.http_client_manager.get_session",
                        AsyncMock(return_value=session),
                    ),
                    patch(
                        "comet.api.endpoints.nzb.EngineClient",
                        return_value=engine,
                    ),
                    patch(
                        "comet.services.media_search.MetadataScraper."
                        "fetch_metadata_and_aliases",
                        AsyncMock(return_value=_metadata_result()),
                    ),
                    patch(
                        "comet.services.media_search._search_configured_sources",
                        AsyncMock(
                            return_value=DiscoveryResult(
                                (candidate,),
                                (),
                                plan,
                            )
                        ),
                    ),
                ):
                    async with AsyncClient(
                        transport=ASGITransport(app=app),
                        base_url="https://comet.example",
                    ) as client:
                        stream_response = await client.get(
                            f"/{b64config}/stream/movie/tt1234567.json",
                        )
                        streams = stream_response.json()["streams"]
                        handoff_url = streams[0]["nzbUrl"]
                        capability = handoff_url.rsplit("/", 1)[1].removesuffix(".nzb")
                        response = await client.get(
                            urlsplit(handoff_url).path,
                        )
                        repeated = await client.head(
                            urlsplit(handoff_url).path,
                        )

                self.assertEqual(stream_response.status_code, 200)
                self.assertEqual(
                    stream_response.headers["cache-control"],
                    "private, no-store",
                )
                self.assertEqual(len(streams), 1)
                self.assertEqual(
                    streams[0]["name"],
                    "[Stremio NNTP📰] Comet 1080p",
                )
                self.assertTrue(capability.startswith("ni2."))
                self.assertEqual(
                    streams[0]["servers"],
                    [
                        "nntps://member:secret@news.example.test:563/2",
                    ],
                )
                self.assertNotIn("fileIdx", streams[0])
                self.assertIn("fileMustInclude", streams[0])
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, _NEWZNAB_GRAB)
                self.assertEqual(
                    response.headers["content-type"],
                    "application/x-nzb",
                )
                self.assertEqual(
                    response.headers["cache-control"],
                    "private, no-store",
                )
                self.assertEqual(
                    response.headers["referrer-policy"],
                    "no-referrer",
                )
                self.assertEqual(repeated.status_code, 200)
                self.assertEqual(repeated.content, b"")
                self.assertEqual(session.operations.count("get"), 1)
                engine.parse_nzb.assert_awaited_once()
            finally:
                await database.disconnect()

    async def test_real_nzb_candidate_reaches_native_sparse_range_reader(self):
        native_servers = [
            {
                "name": "primary",
                "host": "news.example.test",
                "port": 563,
                "tls_mode": "implicit",
                "username": "member",
                "password": "secret",
                "connections": 2,
                "priority": 0,
                "backup": False,
                "pipeline": 2,
            }
        ]
        fixture = await _discover_principal(
            _principal_config(
                _NATIVE_PROVIDER_ID,
                "Comet native",
                "comet_native_usenet",
                {"source": "instance_pool"},
                native_access_token="native-access",
            ),
            _NATIVE_PROVIDER_ID,
            native_authorizer=NativeAccessAuthorizer("native-access"),
            native_engine_enabled=True,
            native_instance_pool_available=True,
        )
        b64config = fixture.b64config
        codec = fixture.codec
        partition = fixture.partition
        discovered = fixture.discovered
        plan = fixture.plan

        with TemporaryDirectory() as temporary:
            artifact_directory = f"{temporary}/artifacts"
            database = ReplicaAwareDatabase(
                Database(f"sqlite+aiosqlite:///{temporary}/native-path.db")
            )
            await database.connect()
            try:
                context = MigrationContext(
                    database,
                    is_sqlite=True,
                    is_postgres=False,
                )
                await _migration_foundation(context)
                await _ensure_usenet_schema(context)
                await _ensure_operations_schema(context)
                query = MediaQuery(
                    "tt1234567",
                    "movie",
                    title_aliases=("Example 2026",),
                )
                branch_fingerprint = "b" * 64
                release_repository = ReleaseDiscoveryRepository(database)
                await release_repository.persist_success(
                    query,
                    branch_fingerprint,
                    discovered.candidates,
                    discovery_configuration_id=_NEWZNAB_SOURCE_ID,
                    owner_configuration_partition=partition,
                    next_refresh_at=120,
                    now=10,
                )
                canonical_candidates = await release_repository.load_active(
                    query,
                    branch_fingerprint,
                    owner_configuration_partition=partition,
                    now=11,
                )
                artifact_sha256 = hashlib.sha256(_NEWZNAB_GRAB).hexdigest()
                inspection = {
                    "version": 1,
                    "artifact_sha256": artifact_sha256,
                    "inspection_state": "provisionally_streamable",
                    "container": "matroska",
                    "duration_millis": 90_000,
                    "inspected_head_bytes": 99,
                    "inspected_tail_bytes": 0,
                }
                engine = type(
                    "Engine",
                    (),
                    {
                        "parse_nzb": AsyncMock(return_value=_artifact_parse_result()),
                        "catalog_nntp_artifact": AsyncMock(
                            return_value=[_native_asset(artifact_sha256)]
                        ),
                        "inspect_nntp_postings": AsyncMock(return_value=inspection),
                        "open_nntp_session": AsyncMock(
                            return_value=(
                                "S" * 22,
                                99,
                                "c" * 64,
                                None,
                            )
                        ),
                        "open_session_reader": AsyncMock(
                            side_effect=["L" * 22, "M" * 22]
                        ),
                        "read_session_range": AsyncMock(
                            side_effect=[b"x" * 10, b"y" * 10]
                        ),
                        "close_session_reader": AsyncMock(),
                    },
                )()
                from comet.api.app import app

                with (
                    patch(
                        "comet.api.endpoints.usenet_playback.database",
                        database,
                    ),
                    patch(
                        "comet.services.media_search.database",
                        database,
                    ),
                    patch(
                        "comet.api.endpoints.usenet_playback.settings.USENET_ENABLED",
                        True,
                    ),
                    patch(
                        "comet.api.endpoints.usenet_playback.settings."
                        "COMET_CAPABILITY_SECRET",
                        _SECRET,
                    ),
                    patch(
                        "comet.api.endpoints.usenet_playback.settings."
                        "USENET_ARTIFACT_DIR",
                        artifact_directory,
                    ),
                    patch(
                        "comet.services.usenet_operations.database",
                        database,
                    ),
                    patch(
                        "comet.api.endpoints.usenet_playback.settings."
                        "USENET_NATIVE_ACCESS_TOKEN",
                        "native-access",
                    ),
                    patch(
                        "comet.api.endpoints.usenet_playback.settings."
                        "USENET_ENGINE_ENABLED",
                        True,
                    ),
                    patch(
                        "comet.api.endpoints.usenet_playback.settings."
                        "USENET_NATIVE_SERVERS",
                        native_servers,
                    ),
                    patch(
                        "comet.api.endpoints.usenet_playback.http_client_manager."
                        "get_session",
                        AsyncMock(return_value=_NewznabSession()),
                    ),
                    patch(
                        "comet.playback.manager.ensure_playback_capability_states",
                        AsyncMock(
                            return_value={
                                _NATIVE_PROVIDER_ID: EffectiveCapabilityState(
                                    "valid",
                                    True,
                                    False,
                                    False,
                                )
                            }
                        ),
                    ),
                    patch(
                        "comet.services.media_search.MetadataScraper."
                        "fetch_metadata_and_aliases",
                        AsyncMock(return_value=_metadata_result()),
                    ),
                    patch(
                        "comet.services.media_search._search_configured_sources",
                        AsyncMock(
                            return_value=DiscoveryResult(
                                canonical_candidates,
                                (),
                                plan,
                            )
                        ),
                    ),
                    patch(
                        "comet.api.endpoints.usenet_playback.EngineClient",
                        return_value=engine,
                    ),
                ):
                    async with AsyncClient(
                        transport=ASGITransport(app=app),
                        base_url="https://comet.example",
                    ) as client:
                        stream_response = await client.get(
                            f"/{b64config}/stream/movie/tt1234567.json",
                        )
                        streams = stream_response.json()["streams"]
                        playback_url = streams[0]["url"]
                        pi2 = playback_url.rsplit("/", 1)[1]
                        intent_redirect = await client.get(
                            urlsplit(playback_url).path,
                            follow_redirects=False,
                        )
                        pa2 = urlsplit(intent_redirect.headers["location"]).path.rsplit(
                            "/", 1
                        )[1]
                        response_preparation_id = codec.decode_prepared_asset(
                            pa2,
                            partition=partition,
                        ).preparation_id
                        response = await client.get(
                            f"/{b64config}/playback/v2/{pa2}",
                            headers={"Range": "bytes=10-19"},
                        )
                        resumed = await client.get(
                            f"/{b64config}/playback/v2/{pa2}",
                            headers={"Range": "bytes=20-29"},
                        )

                self.assertEqual(stream_response.status_code, 200)
                self.assertEqual(len(streams), 1)
                self.assertEqual(
                    streams[0]["name"],
                    "[Comet native📰] Comet 1080p",
                )
                self.assertTrue(pi2.startswith("pi2."))
                self.assertEqual(intent_redirect.status_code, 307)
                self.assertEqual(response.status_code, 206)
                self.assertEqual(
                    response.headers["content-range"],
                    "bytes 10-19/99",
                )
                self.assertEqual(
                    response.headers["etag"],
                    f'W/"pa2-{response_preparation_id}-{"c" * 64}"',
                )
                self.assertEqual(
                    response.headers["content-type"],
                    "application/octet-stream",
                )
                self.assertEqual(
                    response.headers["content-disposition"],
                    "inline; filename*=UTF-8''Example.2026.1080p.mkv",
                )
                self.assertEqual(response.content, b"x" * 10)
                self.assertEqual(resumed.status_code, 206)
                self.assertEqual(
                    resumed.headers["content-range"],
                    "bytes 20-29/99",
                )
                self.assertEqual(
                    resumed.headers["etag"],
                    f'W/"pa2-{response_preparation_id}-{"c" * 64}"',
                )
                self.assertEqual(resumed.content, b"y" * 10)
                engine.parse_nzb.assert_awaited_once_with(
                    artifact_sha256,
                    _NEWZNAB_GRAB,
                )
                self.assertEqual(
                    engine.open_nntp_session.await_count,
                    1,
                )
                engine.catalog_nntp_artifact.assert_awaited_once()
                engine.inspect_nntp_postings.assert_awaited_once()
                self.assertEqual(
                    [call.args for call in engine.open_session_reader.await_args_list],
                    [("S" * 22,), ("S" * 22,)],
                )
                self.assertEqual(
                    [call.args for call in engine.read_session_range.await_args_list],
                    [
                        ("S" * 22, "L" * 22, 99, 10, 19),
                        ("S" * 22, "M" * 22, 99, 20, 29),
                    ],
                )
                self.assertEqual(
                    [call.args for call in engine.close_session_reader.await_args_list],
                    [
                        ("S" * 22, "L" * 22),
                        ("S" * 22, "M" * 22),
                    ],
                )
            finally:
                await database.disconnect()
