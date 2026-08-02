import base64
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from databases import Database
from starlette.requests import Request

from comet.api.endpoints.usenet_playback import playback_v2
from comet.core.capabilities import EligibleProvider
from comet.core.capability_bindings import build_playback_capability_bindings
from comet.core.capability_states import CapabilityStateRepository
from comet.core.db_router import ReplicaAwareDatabase
from comet.core.schema_migrations import (
    MigrationContext,
    _ensure_usenet_schema,
    _migration_foundation,
)
from comet.core.sources import (
    LocatorKind,
    LocatorPolicy,
    ReleaseCandidate,
    ReleaseScope,
    TorrentLocator,
    TransportKind,
)
from comet.metadata.manager import MetadataFetchResult, MetadataFetchStatus
from comet.playback.presentation import (
    ProviderOption,
    issue_provider_option_capability,
)
from comet.playback.repository import RenderedReleaseRepository
from comet.playback.tokens import CapabilityCodec

ROOT = base64.urlsafe_b64encode(b"d" * 32).decode().rstrip("=")
PROVIDER_ID = "11111111-1111-4111-8111-111111111111"
ACCOUNT_KEY = "k" * 3_000


def _metadata_result() -> MetadataFetchResult:
    return MetadataFetchResult(
        MetadataFetchStatus.OK,
        {"title": "Movie"},
        {},
    )


def _request(capability: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "server": ("comet.example", 443),
            "path": f"/config/playback/v2/{capability}",
            "query_string": b"",
            "headers": (),
            "client": ("127.0.0.1", 12345),
        }
    )


class TorrentDebridV2Tests(unittest.IsolatedAsyncioTestCase):
    async def test_pi2_to_pa2_uses_the_exact_debrid_binding_and_no_numeric_index(
        self,
    ):
        config = {
            "schemaVersion": 2,
            "enabledTransports": ["bittorrent"],
            "accounts": None,
            "playbackProviders": [
                {
                    "configurationId": PROVIDER_ID,
                    "displayName": "Living room",
                    "kind": "realdebrid",
                    "enabled": True,
                }
            ],
            "_debridEntries": [
                {
                    "configurationId": PROVIDER_ID,
                    "service": "realdebrid",
                    "apiKey": ACCOUNT_KEY,
                }
            ],
            "debridStreamProxyPassword": "",
        }
        codec = CapabilityCodec(ROOT)
        partition = codec.configuration_partition_for_config(config)
        locator = TorrentLocator(
            locator_id="runtime-torrent",
            kind=LocatorKind.TORRENT,
            policy=LocatorPolicy(frozenset({"realdebrid"})),
            info_hash="a" * 40,
            file_index=7,
            selection_title="Movie.2026.1080p.mkv",
            selection_size=42,
            selection_parsed_json="{}",
        )
        candidate = ReleaseCandidate(
            candidate_id="btih:" + "a" * 40,
            media_id="tt1234567",
            scope=ReleaseScope.MOVIE,
            transport=TransportKind.BITTORRENT,
            title="Movie.2026.1080p.mkv",
            locators=(locator,),
            size=42,
        )

        with TemporaryDirectory() as temporary:
            database = ReplicaAwareDatabase(
                Database(f"sqlite+aiosqlite:///{temporary}/torrent-v2.db")
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
                persisted = (
                    await RenderedReleaseRepository(database).persist(
                        (candidate,),
                        owner_configuration_partition=partition,
                    )
                )[candidate.candidate_id]
                option = ProviderOption(
                    candidate.candidate_id,
                    EligibleProvider(PROVIDER_ID, "realdebrid", 0),
                    candidate.locators,
                    cached=True,
                )
                intent = issue_provider_option_capability(
                    codec,
                    partition=partition,
                    option=option,
                    persisted=persisted,
                    selection_intent=[0],
                    client="stremio",
                )
                binding = build_playback_capability_bindings(
                    config,
                    codec,
                )[0].binding
                await CapabilityStateRepository(database).record_success(
                    binding,
                )

                generated = AsyncMock(
                    return_value=(
                        "https://download.example/video?signature=request-local"
                    )
                )
                with (
                    patch(
                        "comet.api.endpoints.usenet_playback.database",
                        database,
                    ),
                    patch(
                        "comet.api.endpoints.usenet_playback.config_check",
                        return_value=config,
                    ),
                    patch(
                        "comet.api.endpoints.usenet_playback."
                        "http_client_manager.get_session",
                        AsyncMock(return_value=object()),
                    ),
                    patch(
                        "comet.api.endpoints.usenet_playback.settings."
                        "COMET_CAPABILITY_SECRET",
                        ROOT,
                    ),
                    patch(
                        "comet.api.endpoints.usenet_playback.settings.USENET_ENABLED",
                        False,
                    ),
                    patch(
                        "comet.api.endpoints.usenet_playback.settings."
                        "PROXY_DEBRID_STREAM",
                        False,
                    ),
                    patch(
                        "comet.playback.providers.torrent_debrid."
                        "TorrentDebridProvider.generate_download_link",
                        generated,
                    ),
                    patch(
                        "comet.api.endpoints.usenet_playback.get_cached_download_link",
                        AsyncMock(return_value=None),
                    ),
                    patch(
                        "comet.api.endpoints.usenet_playback."
                        "cache_download_link_best_effort",
                        AsyncMock(),
                    ),
                    patch(
                        "comet.api.endpoints.usenet_playback.MetadataScraper."
                        "fetch_metadata_and_aliases",
                        AsyncMock(return_value=_metadata_result()),
                    ),
                ):
                    first = await playback_v2(
                        _request(intent),
                        "config",
                        intent,
                    )
                    prepared_token = first.headers["location"].rsplit("/", 1)[-1]
                    second = await playback_v2(
                        _request(prepared_token),
                        "config",
                        prepared_token,
                    )

                self.assertEqual(first.status_code, 307)
                self.assertTrue(prepared_token.startswith("pa2."))
                self.assertEqual(second.status_code, 307)
                self.assertEqual(
                    second.headers["location"],
                    "https://download.example/video?signature=request-local",
                )
                generated.assert_awaited_once()
                self.assertEqual(
                    generated.await_args.kwargs["info_hash"],
                    "a" * 40,
                )
                self.assertEqual(
                    generated.await_args.kwargs["file_index"],
                    7,
                )
                row = await database.fetch_one(
                    """
                    SELECT state,
                           reconstruction_blueprint_json AS target_ref_json
                    FROM asset_preparations
                    """
                )
                self.assertEqual(row["state"], "ready")
                self.assertNotIn("http", row["target_ref_json"])
                self.assertNotIn("signature", row["target_ref_json"])
                self.assertNotIn("/playback/" + "a" * 40, first.headers["location"])
            finally:
                await database.disconnect()
