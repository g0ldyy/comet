import asyncio
import base64
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, call, patch

from starlette.requests import Request
from starlette.responses import StreamingResponse

from comet.api.endpoints.usenet_playback import (
    NativeRangeError,
    _acquire_native_artifact_leases,
    _advance_remote_preparation,
    _broker_nzb_transform,
    _close_artifact_reader_leases,
    _log_playback_failure,
    _native_range,
    _native_session_body,
    _poll_nzbdav_interactively,
    _poll_stremthru_interactively,
    _poll_torbox_interactively,
    _preparation_state_response,
    _resolved_remote_download_url,
    _run_remote_preparation,
    _serve_torrent_debrid,
    _torrent_debrid_selection,
    playback_v2,
)
from comet.discovery.adapters.newznab import NewznabError
from comet.metadata.manager import MetadataFetchResult, MetadataFetchStatus
from comet.playback.manager import NzbSourceError
from comet.playback.providers.altmount import AltMountError
from comet.playback.providers.nzbdav import NzbDavError
from comet.playback.providers.stremthru_newz import StremThruNewzError
from comet.playback.providers.torbox_usenet import TorBoxUsenetError
from comet.playback.providers.torrent_debrid import TorrentDebridProvider
from comet.usenet.easynews import EasynewsNzbError
from comet.usenet.engine_client import EngineNntpError
from comet.usenet.engine_transport import EngineUnavailable

ROOT = base64.urlsafe_b64encode(b"a" * 32).decode().rstrip("=")
ACCOUNT_PARTITION = b"c" * 32
TEST_RELEASE = type(
    "Release",
    (),
    {"media_id": "tt1234567", "title": "Example"},
)()


def _metadata_result(
    title: str,
    aliases: dict | None = None,
) -> MetadataFetchResult:
    return MetadataFetchResult(
        MetadataFetchStatus.OK,
        {"title": title},
        {} if aliases is None else aliases,
    )


def _resolution(**fields):
    return type(
        "Resolution",
        (),
        {
            "account_partition": ACCOUNT_PARTITION,
            "release": TEST_RELEASE,
            **fields,
        },
    )()


def _pending_prepared():
    preparation = type(
        "Preparation",
        (),
        {
            "provider_kind": "easynews",
            "state": "pending",
        },
    )()
    return type(
        "Prepared",
        (),
        {
            "capability": "pa2.payload.signature",
            "preparation": preparation,
            "resolution": _resolution(),
        },
    )()


def _request(
    *,
    method: str = "GET",
    range_value: str | None = None,
    if_none_match: str | None = None,
    if_range: str | None = None,
) -> Request:
    headers = []
    if range_value is not None:
        headers.append((b"range", range_value.encode()))
    if if_none_match is not None:
        headers.append((b"if-none-match", if_none_match.encode()))
    if if_range is not None:
        headers.append((b"if-range", if_range.encode()))
    return Request(
        {
            "type": "http",
            "method": method,
            "scheme": "https",
            "server": ("comet.example", 443),
            "path": "/config/playback/v2/pi2.token",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 1234),
        }
    )


def _range_request(value: str | None) -> Request:
    headers = [] if value is None else [(b"range", value.encode())]
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "server": ("comet.example", 443),
            "path": "/",
            "headers": headers,
        }
    )


class UsenetPlaybackEndpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        monitor = patch("comet.api.endpoints.usenet_playback.usenet_operation_monitor")
        self.operation_monitor = monitor.start()
        self.operation_monitor.start = AsyncMock(return_value="operation")
        self.operation_monitor.finish = AsyncMock()
        self.operation_monitor.admin_cancelled.return_value = False

        async def run_cancellable(_operation_id, request):
            return await request

        self.operation_monitor.run_cancellable = AsyncMock(side_effect=run_cancellable)
        self.addCleanup(monitor.stop)

    def test_torrent_selection_ignores_unconsumed_locator_kinds(self):
        torrent = {
            "kind": "torrent",
            "payload": {
                "info_hash": "a" * 40,
                "season_norm": -1,
                "episode_norm": -1,
            },
        }
        prepared = SimpleNamespace(
            preparation=SimpleNamespace(selection_intent=(0,)),
            resolution=SimpleNamespace(
                release=SimpleNamespace(
                    locators=(
                        torrent,
                        {"kind": "nzb", "payload": {"opaque": True}},
                    )
                )
            ),
        )

        selected, season, episode = _torrent_debrid_selection(prepared)

        self.assertIs(selected, torrent)
        self.assertIsNone(season)
        self.assertIsNone(episode)

    async def test_artifact_reader_close_failures_are_observable(self):
        error = RuntimeError("lease close failed")
        reader = type("ArtifactReader", (), {"close": AsyncMock(side_effect=error)})()

        with patch("comet.api.endpoints.usenet_playback.log.warning") as warning:
            await _close_artifact_reader_leases((reader,))

        warning.assert_called_once_with(
            "usenet.artifact_reader.close_failed",
            "Usenet artifact reader close failed",
            exc=error,
        )

    async def test_sparse_session_composite_does_not_require_materialized_artifacts(
        self,
    ):
        prepared = type(
            "Prepared",
            (),
            {
                "preparation": type(
                    "Preparation",
                    (),
                    {"preparation_id": "22222222-2222-4222-8222-222222222222"},
                )()
            },
        )()
        target = type("Target", (), {"source_kind": "raw_composite"})()
        acquire = AsyncMock(return_value=())
        with patch(
            "comet.api.endpoints.usenet_playback.MaterializedArtifactRepository."
            "acquire_for_preparation",
            acquire,
        ):
            leases = await _acquire_native_artifact_leases(
                prepared,
                b"a" * 32,
                target,
            )

        self.assertEqual(leases, ())
        acquire.assert_awaited_once_with(
            prepared.preparation.preparation_id,
            owner_configuration_partition=b"a" * 32,
        )

    def test_failure_log_preserves_safe_stage_code_and_debug_exception(self):
        error = NewznabError("nzb_response_invalid")
        prepared = type(
            "Prepared",
            (),
            {
                "preparation": type(
                    "Preparation",
                    (),
                    {"provider_kind": "comet_native_usenet"},
                )(),
                "resolution": type(
                    "Resolution",
                    (),
                    {
                        "release": type(
                            "Release",
                            (),
                            {"media_id": "tt1234567"},
                        )()
                    },
                )(),
            },
        )()

        with patch("comet.api.endpoints.usenet_playback.log.warning") as warning:
            _log_playback_failure(
                prepared,
                error.code,
                retryable=False,
                operation="nzb_grab",
                exception=error,
            )

        warning.assert_called_once_with(
            "usenet.playback.failed",
            "Usenet playback failed",
            exc=error,
            provider_name="comet_native_usenet",
            content_id="tt1234567",
            retryable=False,
            operation="nzb_grab",
            failure_reason="nzb_response_invalid",
        )

    def test_terminal_preparation_does_not_advertise_a_retry(self):
        prepared = _pending_prepared()
        pending = _preparation_state_response("pending", prepared=prepared)
        failed = _preparation_state_response(
            "failed",
            prepared=type(
                "Prepared",
                (),
                {
                    "preparation": type(
                        "Preparation",
                        (),
                        {
                            "state": "failed",
                            "target_ref": {"failure_code": "nntp_article_missing"},
                        },
                    )()
                },
            )(),
        )

        self.assertEqual(pending.headers["retry-after"], "2")
        self.assertTrue(pending.path.endswith("MEDIA_NOT_CACHED_YET.mp4"))
        self.assertEqual(pending.comet_playback_mode, "status")
        self.assertNotIn("retry-after", failed.headers)
        self.assertTrue(failed.path.endswith("LINK_OFFLINE.mp4"))
        self.assertEqual(failed.comet_playback_mode, "status")
        with self.assertRaisesRegex(ValueError, "preparation result"):
            _preparation_state_response("corrupt", prepared=prepared)

    async def test_newznab_limit_returns_an_actionable_status_video(self):
        source_error = NzbSourceError(
            "11111111-1111-4111-8111-111111111111",
            "newznab",
            code="provider_limit_exhausted",
            operation="nzb_grab",
            retryable=True,
        )
        prepared = type(
            "Prepared",
            (),
            {
                "resolution": type(
                    "Resolution",
                    (),
                    {
                        "account_partition": ACCOUNT_PARTITION,
                        "release": type(
                            "Release",
                            (),
                            {"media_id": "tt15239678"},
                        )(),
                    },
                )(),
                "preparation": type(
                    "Preparation",
                    (),
                    {
                        "preparation_id": "22222222-2222-4222-8222-222222222222",
                        "provider_kind": "comet_native_usenet",
                        "provider_configuration_id": "33333333-3333-4333-8333-333333333333",
                        "state": "pending",
                    },
                )(),
            },
        )()
        with (
            patch(
                "comet.api.endpoints.usenet_playback.config_check",
                return_value={"schemaVersion": 2},
            ),
            patch(
                "comet.api.endpoints.usenet_playback.settings.USENET_ENABLED",
                True,
            ),
            patch(
                "comet.api.endpoints.usenet_playback.settings.COMET_CAPABILITY_SECRET",
                ROOT,
            ),
            patch(
                "comet.api.endpoints.usenet_playback.http_client_manager.get_session",
                AsyncMock(return_value=object()),
            ),
            patch(
                "comet.api.endpoints.usenet_playback.resolve_prepared_asset",
                AsyncMock(return_value=prepared),
            ),
            patch(
                "comet.playback.tokens.CapabilityCodec.configuration_partition_for_config",
                return_value=b"a" * 32,
            ),
            patch(
                "comet.api.endpoints.usenet_playback._advance_native_usenet",
                AsyncMock(side_effect=source_error),
            ),
        ):
            response = await playback_v2(
                _request(),
                "config",
                "pa2.payload.signature",
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(str(response.path).endswith("TOO_MANY_REQUESTS.mp4"))
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertNotIn("retry-after", response.headers)

    async def test_torrent_debrid_capability_resolves_without_a_numeric_index(self):
        provider = TorrentDebridProvider(
            object(),
            "realdebrid",
            "account-key",
            "127.0.0.1",
        )
        provider.generate_download_link = AsyncMock(
            return_value="https://download.example/video?signature=temporary"
        )
        preparation_id = str(uuid.uuid4())
        prepared = type(
            "Prepared",
            (),
            {
                "resolution": type(
                    "Resolution",
                    (),
                    {
                        "provider": provider,
                        "account_partition": ACCOUNT_PARTITION,
                        "release": type(
                            "Release",
                            (),
                            {
                                "media_id": "tt1234567",
                                "title": "Movie.2026.1080p",
                                "locators": (
                                    {
                                        "kind": "torrent",
                                        "payload": {
                                            "info_hash": "a" * 40,
                                            "file_index": 4,
                                            "season_norm": -1,
                                            "episode_norm": -1,
                                            "selection_title": ("Movie.2026.1080p"),
                                        },
                                    },
                                ),
                            },
                        )(),
                    },
                )(),
                "preparation": type(
                    "Preparation",
                    (),
                    {
                        "preparation_id": preparation_id,
                        "selection_intent": (0,),
                        "state": "pending",
                    },
                )(),
            },
        )()
        mark_ready = AsyncMock()

        with (
            patch(
                "comet.api.endpoints.usenet_playback.get_cached_download_link",
                AsyncMock(return_value=None),
            ),
            patch(
                "comet.api.endpoints.usenet_playback.MetadataScraper."
                "fetch_metadata_and_aliases",
                AsyncMock(return_value=_metadata_result("Movie", {"ez": ["Movie"]})),
            ),
            patch(
                "comet.api.endpoints.usenet_playback.cache_download_link_best_effort",
                AsyncMock(),
            ) as cache,
            patch(
                "comet.api.endpoints.usenet_playback."
                "PlaybackPreparationRepository.mark_ready",
                mark_ready,
            ),
            patch(
                "comet.api.endpoints.usenet_playback.settings.PROXY_DEBRID_STREAM",
                False,
            ),
        ):
            response = await _serve_torrent_debrid(
                _request(),
                prepared,
                {"debridStreamProxyPassword": ""},
                object(),
                owner_configuration_partition=b"a" * 32,
                client_ip="127.0.0.1",
            )

        self.assertEqual(response.status_code, 307)
        self.assertEqual(
            response.headers["location"],
            "https://download.example/video?signature=temporary",
        )
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        provider.generate_download_link.assert_awaited_once()
        cache.assert_awaited_once()
        target = mark_ready.await_args.kwargs["target_ref"]
        self.assertEqual(
            target,
            {
                "info_hash": "a" * 40,
                "file_index": 4,
                "season": None,
                "episode": None,
            },
        )
        self.assertNotIn("url", target)

    async def test_playback_rejections_are_private_and_do_not_leak_referrers(self):
        with patch(
            "comet.api.endpoints.usenet_playback.config_check",
            return_value=None,
        ):
            with self.assertRaisesRegex(Exception, "Invalid configuration") as invalid:
                await playback_v2(_request(), "config", "pi2.payload.signature")

        self.assertEqual(invalid.exception.status_code, 400)
        self.assertEqual(
            invalid.exception.headers,
            {
                "Cache-Control": "private, no-store",
                "Referrer-Policy": "no-referrer",
            },
        )

        with (
            patch(
                "comet.api.endpoints.usenet_playback.config_check",
                return_value={"schemaVersion": 2},
            ),
            patch(
                "comet.api.endpoints.usenet_playback.settings.COMET_CAPABILITY_SECRET",
                "",
            ),
        ):
            with self.assertRaisesRegex(
                Exception,
                "Signed playback is unavailable",
            ) as unavailable:
                await playback_v2(_request(), "config", "pi2.payload.signature")

        self.assertEqual(unavailable.exception.status_code, 503)
        self.assertEqual(
            unavailable.exception.headers,
            {
                "Cache-Control": "private, no-store",
                "Referrer-Policy": "no-referrer",
            },
        )

    async def test_torbox_interactive_polling_uses_bounded_exponential_cadence(self):
        now = [10.0]
        delays = []

        async def sleep(delay):
            delays.append(delay)
            now[0] += delay

        with patch(
            "comet.api.endpoints.usenet_playback.poll_torbox_usenet",
            AsyncMock(side_effect=["pending", "pending", "ready"]),
        ) as poll:
            state = await _poll_torbox_interactively(
                object(),
                object(),
                owner_configuration_partition=b"a" * 32,
                deadline_seconds=15,
                _clock=lambda: now[0],
                _sleep=sleep,
            )

        self.assertEqual(state, "ready")
        self.assertEqual(delays, [1.0, 2.0])
        self.assertEqual(poll.await_count, 3)

    async def test_remote_submission_is_polled_in_the_same_request(self):
        initial = SimpleNamespace(
            preparation=SimpleNamespace(
                preparation_id="22222222-2222-4222-8222-222222222222",
                target_ref=None,
            ),
            resolution=object(),
            capability="pa2.payload.signature",
        )
        persisted = SimpleNamespace(target_ref={"remote_id": 7})
        prepare = AsyncMock(return_value="pending")
        poll = AsyncMock(return_value="ready")

        with patch(
            "comet.api.endpoints.usenet_playback.PlaybackPreparationRepository.resolve",
            AsyncMock(return_value=persisted),
        ) as resolve:
            state = await _advance_remote_preparation(
                initial,
                b"a" * 32,
                prepare,
                poll,
            )

        self.assertEqual(state, "ready")
        prepare.assert_awaited_once()
        resolve.assert_awaited_once_with(
            initial.preparation.preparation_id,
            owner_configuration_partition=b"a" * 32,
        )
        current = poll.await_args.args[0]
        self.assertIs(current.preparation, persisted)
        self.assertIs(current.resolution, initial.resolution)
        self.assertEqual(current.capability, initial.capability)

    async def test_concurrent_remote_opens_share_work_after_one_client_disconnects(
        self,
    ):
        entered = asyncio.Event()
        release = asyncio.Event()
        prepared = SimpleNamespace(
            preparation=SimpleNamespace(
                preparation_id="22222222-2222-4222-8222-222222222222"
            )
        )

        async def locked(*_args, **_kwargs):
            entered.set()
            await release.wait()
            return "ready"

        with patch(
            "comet.api.endpoints.usenet_playback._with_preparation_lock",
            AsyncMock(side_effect=locked),
        ) as lock:
            disconnected = asyncio.create_task(
                _run_remote_preparation(
                    prepared,
                    b"a" * 32,
                    AsyncMock(),
                    AsyncMock(),
                )
            )
            await entered.wait()
            follower = asyncio.create_task(
                _run_remote_preparation(
                    prepared,
                    b"a" * 32,
                    AsyncMock(),
                    AsyncMock(),
                )
            )
            disconnected.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await disconnected
            release.set()

            self.assertEqual(await follower, "ready")

        lock.assert_awaited_once()

    async def test_remote_reopen_starts_a_new_cycle_after_pending_deadline(self):
        prepared = SimpleNamespace(
            preparation=SimpleNamespace(
                preparation_id="33333333-3333-4333-8333-333333333333"
            )
        )
        with patch(
            "comet.api.endpoints.usenet_playback._with_preparation_lock",
            AsyncMock(side_effect=["pending", "ready"]),
        ) as lock:
            first = await _run_remote_preparation(
                prepared,
                b"a" * 32,
                AsyncMock(),
                AsyncMock(),
            )
            await asyncio.sleep(0)
            second = await _run_remote_preparation(
                prepared,
                b"a" * 32,
                AsyncMock(),
                AsyncMock(),
            )

        self.assertEqual((first, second), ("pending", "ready"))
        self.assertEqual(lock.await_count, 2)

    async def test_remote_follower_reloads_ready_state_before_provider_polling(self):
        prepared = SimpleNamespace(
            preparation=SimpleNamespace(
                preparation_id="44444444-4444-4444-8444-444444444444"
            ),
            resolution=object(),
            capability="pa2.payload.signature",
        )
        persisted = SimpleNamespace(state="ready")
        lock = SimpleNamespace(
            acquire=AsyncMock(return_value=True),
            release=AsyncMock(),
        )

        async def run(operation):
            return await operation

        lock.run = AsyncMock(side_effect=run)
        prepare = AsyncMock()
        poll = AsyncMock()
        with (
            patch(
                "comet.api.endpoints.usenet_playback.DistributedLock",
                return_value=lock,
            ),
            patch(
                "comet.api.endpoints.usenet_playback."
                "PlaybackPreparationRepository.resolve",
                AsyncMock(return_value=persisted),
            ),
        ):
            state = await _run_remote_preparation(
                prepared,
                b"a" * 32,
                prepare,
                poll,
            )

        self.assertEqual(state, "ready")
        lock.acquire.assert_awaited_once_with(wait_timeout=110)
        prepare.assert_not_awaited()
        poll.assert_not_awaited()

    async def test_torbox_seek_reuses_the_exact_cached_download_url(self):
        prepared = SimpleNamespace(
            preparation=SimpleNamespace(
                preparation_id="22222222-2222-4222-8222-222222222222",
                provider_kind="torbox_usenet",
                target_ref={"file_id": 3},
            ),
            resolution=SimpleNamespace(account_partition=b"c" * 32),
        )
        cached_url = "https://cdn.example/video?token=cached"

        with (
            patch(
                "comet.api.endpoints.usenet_playback.get_cached_download_link",
                AsyncMock(return_value=cached_url),
            ) as cache,
            patch(
                "comet.api.endpoints.usenet_playback.remote_download_url",
                AsyncMock(),
            ) as resolve,
        ):
            result = await _resolved_remote_download_url(prepared)

        self.assertEqual(result, cached_url)
        resolve.assert_not_awaited()
        self.assertEqual(
            cache.await_args.kwargs["info_hash"],
            prepared.preparation.preparation_id,
        )
        self.assertEqual(cache.await_args.kwargs["client_ip"], "")

    async def test_torbox_interactive_polling_stops_at_its_deadline(self):
        now = [10.0]
        delays = []

        async def sleep(delay):
            delays.append(delay)
            now[0] += delay

        with patch(
            "comet.api.endpoints.usenet_playback.poll_torbox_usenet",
            AsyncMock(return_value="pending"),
        ) as poll:
            state = await _poll_torbox_interactively(
                object(),
                object(),
                owner_configuration_partition=b"a" * 32,
                deadline_seconds=3,
                _clock=lambda: now[0],
                _sleep=sleep,
            )

        self.assertEqual(state, "pending")
        self.assertEqual(delays, [1.0, 2.0])
        self.assertEqual(poll.await_count, 2)

    async def test_interactive_polling_cancels_work_at_the_hard_deadline(self):
        cancelled = asyncio.Event()

        async def blocked(*_args, **_kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with patch(
            "comet.api.endpoints.usenet_playback.poll_nzbdav",
            side_effect=blocked,
        ) as poll:
            state = await _poll_nzbdav_interactively(
                object(),
                object(),
                owner_configuration_partition=b"a" * 32,
                deadline_seconds=0.01,
            )

        self.assertEqual(state, "pending")
        poll.assert_called_once()
        self.assertTrue(cancelled.is_set())

    async def test_stremthru_polling_uses_the_same_bounded_cadence(self):
        now = [10.0]
        delays = []

        async def sleep(delay):
            delays.append(delay)
            now[0] += delay

        with patch(
            "comet.api.endpoints.usenet_playback.poll_stremthru_newz",
            AsyncMock(side_effect=["pending", "ready"]),
        ) as poll:
            state = await _poll_stremthru_interactively(
                object(),
                object(),
                owner_configuration_partition=b"a" * 32,
                deadline_seconds=15,
                _clock=lambda: now[0],
                _sleep=sleep,
            )

        self.assertEqual(state, "ready")
        self.assertEqual(delays, [1.0])
        self.assertEqual(poll.await_count, 2)

    async def test_stremthru_tombstone_stops_before_polling_stale_target_again(self):
        sleep = AsyncMock()
        with patch(
            "comet.api.endpoints.usenet_playback.poll_stremthru_newz",
            AsyncMock(return_value="reprepare"),
        ) as poll:
            state = await _poll_stremthru_interactively(
                object(),
                object(),
                owner_configuration_partition=b"a" * 32,
                deadline_seconds=15,
                _sleep=sleep,
            )

        self.assertEqual(state, "reprepare")
        poll.assert_awaited_once()
        sleep.assert_not_awaited()

    async def test_nzbdav_polling_uses_the_shared_interactive_deadline(self):
        now = [10.0]

        async def sleep(delay):
            now[0] += delay

        with patch(
            "comet.api.endpoints.usenet_playback.poll_nzbdav",
            AsyncMock(side_effect=["pending", "ready"]),
        ) as poll:
            state = await _poll_nzbdav_interactively(
                object(),
                object(),
                owner_configuration_partition=b"a" * 32,
                deadline_seconds=15,
                _clock=lambda: now[0],
                _sleep=sleep,
            )

        self.assertEqual(state, "ready")
        self.assertEqual(poll.await_count, 2)

    def test_native_ranges_are_bounded_and_canonical(self):
        self.assertEqual(_native_range(_range_request("bytes=2-4"), 6), (2, 4, True))
        self.assertEqual(
            _native_range(_range_request(None), 10 * 1024 * 1024),
            (0, 10 * 1024 * 1024 - 1, False),
        )
        self.assertEqual(_native_range(_range_request("bytes=-4"), 6), (2, 5, True))
        self.assertEqual(
            _native_range(_range_request("bytes=2-9000000"), 6), (2, 5, True)
        )
        for value in (
            "bytes=6-7",
            "bytes=0-1,4-5",
            "bytes=4",
            f"bytes={'9' * 5_000}-",
            f"bytes=-{'9' * 5_000}",
        ):
            with self.assertRaisesRegex(NativeRangeError, "Range"):
                _native_range(_range_request(value), 6)

    def test_native_ranges_accept_only_ascii_digits(self):
        """str.isdigit() also accepts non-ASCII digits, which RFC 7233 does not."""
        for value in (
            "bytes=\u0663-\u0665",
            "bytes=-\u0663",
            "bytes=\u00b2-4",
            "bytes=-\u00b2",
            "bytes=2-\u00b2",
        ):
            with self.assertRaisesRegex(NativeRangeError, "Range"):
                _native_range(_range_request(value), 6)

    async def test_generated_easynews_auth_rejection_invalidates_source_binding(self):
        source_id = "11111111-1111-4111-8111-111111111111"
        prepared = type(
            "Prepared",
            (),
            {
                "resolution": type(
                    "Resolution",
                    (),
                    {
                        "release": type(
                            "Release",
                            (),
                            {"locators": ({"kind": "easynews_http"},)},
                        )()
                    },
                )()
            },
        )()

        async def rejected(*_args, **_kwargs):
            raise NzbSourceError(
                source_id,
                "easynews",
                code="easynews_auth_failed",
                operation="nzb_generate",
                auth_failed=True,
            ) from EasynewsNzbError("easynews_auth_failed", auth_failed=True)

        with (
            patch(
                "comet.api.endpoints.usenet_playback.broker_nzb_sources",
                side_effect=rejected,
            ),
            patch(
                "comet.api.endpoints.usenet_playback.build_discovery_adapters",
                return_value={},
            ),
            patch(
                "comet.api.endpoints.usenet_playback.settings.COMET_CAPABILITY_SECRET",
                ROOT,
            ),
            patch(
                "comet.api.endpoints.usenet_playback.record_discovery_capability_failure",
                AsyncMock(),
            ) as record_failure,
        ):
            with self.assertRaises(NzbSourceError):
                await _broker_nzb_transform(
                    prepared,
                    b"a" * 32,
                    {"schemaVersion": 2},
                    object(),
                    object(),
                )

        record_failure.assert_awaited_once_with(
            {"schemaVersion": 2},
            ANY,
            ANY,
            source_id,
            state="auth_failed",
            error_code="credentials_rejected",
            retry_after=None,
        )

    async def test_native_response_streams_full_content_in_engine_sized_chunks(self):
        engine = type(
            "Engine",
            (),
            {
                "open_session_reader": AsyncMock(return_value="L" * 22),
                "read_session_range": AsyncMock(side_effect=[b"abcd", b"ef"]),
                "close_session_reader": AsyncMock(),
            },
        )()
        with patch("comet.api.endpoints.usenet_playback._NATIVE_RANGE_CHUNK_BYTES", 4):
            chunks = [
                chunk
                async for chunk in _native_session_body(
                    engine,
                    "A" * 22,
                    6,
                    0,
                    5,
                    source_kind="session",
                    client_ip="192.0.2.1",
                    content_id="tt1234567",
                    title="Example",
                    member_path="example.mkv",
                )
            ]

        self.assertEqual(chunks, [b"abcd", b"ef"])
        self.assertEqual(
            [call.args for call in engine.read_session_range.await_args_list],
            [
                ("A" * 22, "L" * 22, 6, 0, 3),
                ("A" * 22, "L" * 22, 6, 4, 5),
            ],
        )
        engine.close_session_reader.assert_awaited_once_with(
            "A" * 22,
            "L" * 22,
        )

    async def test_native_response_yields_a_small_initial_chunk_before_full_chunks(
        self,
    ):
        engine = type(
            "Engine",
            (),
            {
                "open_session_reader": AsyncMock(return_value="L" * 22),
                "read_session_range": AsyncMock(side_effect=[b"ab", b"cdef", b"gh"]),
                "close_session_reader": AsyncMock(),
            },
        )()
        with (
            patch(
                "comet.api.endpoints.usenet_playback._NATIVE_INITIAL_RANGE_CHUNK_BYTES",
                2,
            ),
            patch(
                "comet.api.endpoints.usenet_playback._NATIVE_RANGE_CHUNK_BYTES",
                4,
            ),
        ):
            chunks = [
                chunk
                async for chunk in _native_session_body(
                    engine,
                    "A" * 22,
                    8,
                    0,
                    7,
                    source_kind="session",
                    client_ip="192.0.2.1",
                    content_id="tt1234567",
                    title="Example",
                    member_path="example.mkv",
                )
            ]

        self.assertEqual(chunks, [b"ab", b"cdef", b"gh"])
        self.assertEqual(
            [call.args[-2:] for call in engine.read_session_range.await_args_list],
            [(0, 1), (2, 5), (6, 7)],
        )

    async def test_native_response_releases_reader_when_stream_is_closed_early(self):
        engine = type(
            "Engine",
            (),
            {
                "open_session_reader": AsyncMock(return_value="L" * 22),
                "read_session_range": AsyncMock(return_value=b"abcd"),
                "close_session_reader": AsyncMock(),
            },
        )()
        artifact_readers = [
            type("ArtifactReader", (), {"close": AsyncMock()})() for _ in range(2)
        ]
        with patch("comet.api.endpoints.usenet_playback._NATIVE_RANGE_CHUNK_BYTES", 4):
            body = _native_session_body(
                engine,
                "A" * 22,
                8,
                0,
                7,
                source_kind="session",
                client_ip="192.0.2.1",
                content_id="tt1234567",
                title="Example",
                member_path="example.mkv",
                artifact_reader_leases=tuple(artifact_readers),
            )
            self.assertEqual(await anext(body), b"abcd")
            await body.aclose()

        engine.close_session_reader.assert_awaited_once_with(
            "A" * 22,
            "L" * 22,
        )
        for reader in artifact_readers:
            reader.close.assert_awaited_once()

    async def test_native_client_disconnect_is_not_an_admin_cancellation(self):
        reading = asyncio.Event()

        async def read_range(*_args):
            reading.set()
            await asyncio.Event().wait()

        engine = type(
            "Engine",
            (),
            {
                "open_session_reader": AsyncMock(return_value="L" * 22),
                "read_session_range": AsyncMock(side_effect=read_range),
                "close_session_reader": AsyncMock(),
            },
        )()
        body = _native_session_body(
            engine,
            "A" * 22,
            8,
            0,
            7,
            source_kind="session",
            client_ip="192.0.2.1",
            content_id="tt1234567",
            title="Example",
            member_path="example.mkv",
        )
        stream = asyncio.create_task(anext(body))
        await reading.wait()
        stream.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await stream

        engine.close_session_reader.assert_awaited_once_with(
            "A" * 22,
            "L" * 22,
        )
        self.operation_monitor.finish.assert_awaited_once_with(
            "operation",
            outcome="completed",
            error_code=None,
        )

    async def test_native_admin_cancellation_completes_the_asgi_stream(self):
        async def read_range(*_args):
            await asyncio.Event().wait()

        engine = type(
            "Engine",
            (),
            {
                "open_session_reader": AsyncMock(return_value="L" * 22),
                "read_session_range": AsyncMock(side_effect=read_range),
                "close_session_reader": AsyncMock(),
            },
        )()
        self.operation_monitor.admin_cancelled.return_value = True

        async def cancel_request(_operation_id, request):
            task = asyncio.create_task(request)
            task.cancel()
            return await task

        self.operation_monitor.run_cancellable.side_effect = cancel_request
        response = StreamingResponse(
            _native_session_body(
                engine,
                "A" * 22,
                8,
                0,
                7,
                source_kind="session",
                client_ip="192.0.2.1",
                content_id="tt1234567",
                title="Example",
                member_path="example.mkv",
            )
        )
        messages = []

        async def send(message):
            messages.append(message)

        await response(
            {"type": "http", "asgi": {"spec_version": "2.4"}},
            AsyncMock(),
            send,
        )

        self.assertEqual(
            messages[-1],
            {"type": "http.response.body", "body": b"", "more_body": False},
        )
        engine.close_session_reader.assert_awaited_once_with(
            "A" * 22,
            "L" * 22,
        )
        self.operation_monitor.finish.assert_awaited_once_with(
            "operation",
            outcome="cancelled",
            error_code=None,
        )

    async def test_native_response_retries_only_the_failed_engine_range(self):
        engine = type(
            "Engine",
            (),
            {
                "open_session_reader": AsyncMock(return_value="L" * 22),
                "read_session_range": AsyncMock(
                    side_effect=[
                        EngineNntpError(
                            "nntp_availability_unknown",
                            retryable=True,
                        ),
                        b"abcd",
                        b"ef",
                    ]
                ),
                "close_session_reader": AsyncMock(),
            },
        )()
        with (
            patch(
                "comet.api.endpoints.usenet_playback._NATIVE_RANGE_CHUNK_BYTES",
                4,
            ),
            patch(
                "comet.api.endpoints.usenet_playback._NATIVE_RANGE_RETRY_DELAYS",
                (0,),
            ),
        ):
            chunks = [
                chunk
                async for chunk in _native_session_body(
                    engine,
                    "A" * 22,
                    6,
                    0,
                    5,
                    source_kind="session",
                    client_ip="192.0.2.1",
                    content_id="tt1234567",
                    title="Example",
                    member_path="example.mkv",
                )
            ]

        self.assertEqual(chunks, [b"abcd", b"ef"])
        self.assertEqual(engine.read_session_range.await_count, 3)
        engine.close_session_reader.assert_awaited_once_with(
            "A" * 22,
            "L" * 22,
        )

    async def test_native_response_swallows_best_effort_reader_close_failure(self):
        engine = type(
            "Engine",
            (),
            {
                "open_session_reader": AsyncMock(return_value="L" * 22),
                "read_session_range": AsyncMock(return_value=b"abcd"),
                "close_session_reader": AsyncMock(
                    side_effect=EngineUnavailable("engine restarted")
                ),
            },
        )()

        with patch("comet.api.endpoints.usenet_playback.log.warning") as warning:
            chunks = [
                chunk
                async for chunk in _native_session_body(
                    engine,
                    "A" * 22,
                    4,
                    0,
                    3,
                    source_kind="session",
                    client_ip="192.0.2.1",
                    content_id="tt1234567",
                    title="Example",
                    member_path="example.mkv",
                )
            ]

        self.assertEqual(chunks, [b"abcd"])
        engine.close_session_reader.assert_awaited_once()
        warning.assert_called_once()

    async def test_native_restart_after_headers_terminates_without_failover(self):
        engine = type(
            "Engine",
            (),
            {
                "open_session_reader": AsyncMock(return_value="L" * 22),
                "read_session_range": AsyncMock(
                    side_effect=[
                        b"abcd",
                        EngineUnavailable("engine restarted"),
                    ]
                ),
                "close_session_reader": AsyncMock(),
            },
        )()
        with patch(
            "comet.api.endpoints.usenet_playback._NATIVE_RANGE_CHUNK_BYTES",
            4,
        ):
            body = _native_session_body(
                engine,
                "A" * 22,
                8,
                0,
                7,
                source_kind="session",
                client_ip="192.0.2.1",
                content_id="tt1234567",
                title="Example",
                member_path="example.mkv",
            )
            self.assertEqual(await anext(body), b"abcd")
            with self.assertRaisesRegex(EngineUnavailable, "restarted"):
                await anext(body)

        engine.open_session_reader.assert_awaited_once_with("A" * 22)
        self.assertEqual(engine.read_session_range.await_count, 2)
        engine.close_session_reader.assert_awaited_once_with(
            "A" * 22,
            "L" * 22,
        )

    async def test_native_response_streams_fingerprint_pinned_materialization_ranges(
        self,
    ):
        engine = type(
            "Engine",
            (),
            {
                "open_raw_composite_reader": AsyncMock(return_value="L" * 22),
                "read_raw_composite_range": AsyncMock(return_value=b"cdef"),
                "close_raw_composite_reader": AsyncMock(),
            },
        )()

        chunks = [
            chunk
            async for chunk in _native_session_body(
                engine,
                "a" * 64,
                6,
                2,
                5,
                source_kind="raw_composite",
                client_ip="192.0.2.1",
                content_id="tt1234567",
                title="Example",
                member_path="example.mkv",
            )
        ]

        self.assertEqual(chunks, [b"cdef"])
        engine.read_raw_composite_range.assert_awaited_once_with(
            "a" * 64,
            "L" * 22,
            6,
            2,
            5,
        )
        engine.close_raw_composite_reader.assert_awaited_once_with(
            "a" * 64,
            "L" * 22,
        )

    async def test_ready_native_playback_preserves_full_and_partial_http_semantics(
        self,
    ):
        preparation_id = "22222222-2222-4222-8222-222222222222"
        weak_etag = f'W/"pa2-{preparation_id}-{"b" * 64}"'
        preparation = type(
            "Preparation",
            (),
            {
                "preparation_id": preparation_id,
                "provider_kind": "comet_native_usenet",
                "state": "ready",
                "target_ref": {
                    "source_kind": "session",
                    "session_id": "B" * 22,
                    "byte_size": 6,
                    "session_revision": "b" * 64,
                    "relative_path": "Café [2026].mkv",
                },
            },
        )()
        prepared = type(
            "Prepared",
            (),
            {
                "preparation": preparation,
                "resolution": _resolution(),
            },
        )()
        engine = type(
            "Engine",
            (),
            {
                "open_session_reader": AsyncMock(return_value="L" * 22),
                "read_session_range": AsyncMock(
                    side_effect=[
                        b"abcdef",
                        b"cdef",
                        b"abcdef",
                        b"abcdef",
                    ]
                ),
                "close_session_reader": AsyncMock(),
            },
        )()
        common = [
            patch(
                "comet.api.endpoints.usenet_playback.config_check",
                return_value={"schemaVersion": 2},
            ),
            patch("comet.api.endpoints.usenet_playback.settings.USENET_ENABLED", True),
            patch(
                "comet.api.endpoints.usenet_playback.settings.COMET_CAPABILITY_SECRET",
                ROOT,
            ),
            patch(
                "comet.api.endpoints.usenet_playback.http_client_manager.get_session",
                AsyncMock(return_value=object()),
            ),
            patch(
                "comet.api.endpoints.usenet_playback.resolve_prepared_asset",
                AsyncMock(return_value=prepared),
            ),
            patch(
                "comet.api.endpoints.usenet_playback.EngineClient", return_value=engine
            ),
            patch(
                "comet.playback.tokens.CapabilityCodec.configuration_partition_for_config",
                return_value=b"a" * 32,
            ),
            patch(
                "comet.api.endpoints.usenet_playback._advance_native_usenet",
                AsyncMock(return_value="ready"),
            ),
        ]
        with (
            common[0],
            common[1],
            common[2],
            common[3],
            common[4],
            common[5],
            common[6],
            common[7] as advance_native,
        ):
            head_response = await playback_v2(
                _request(method="HEAD", range_value="bytes=1-2"),
                "config",
                "pa2.payload.signature",
            )
            head_body = b"".join([chunk async for chunk in head_response.body_iterator])
            full_response = await playback_v2(
                _request(), "config", "pa2.payload.signature"
            )
            full_body = b"".join([chunk async for chunk in full_response.body_iterator])
            partial_response = await playback_v2(
                _request(range_value="bytes=2-99"), "config", "pa2.payload.signature"
            )
            partial_body = b"".join(
                [chunk async for chunk in partial_response.body_iterator]
            )
            if_range_miss = await playback_v2(
                _request(range_value="bytes=2-99", if_range='"rs1-stale"'),
                "config",
                "pa2.payload.signature",
            )
            if_range_miss_body = b"".join(
                [chunk async for chunk in if_range_miss.body_iterator]
            )
            weak_if_range = await playback_v2(
                _request(
                    range_value="bytes=2-99",
                    if_range=weak_etag,
                ),
                "config",
                "pa2.payload.signature",
            )
            weak_if_range_body = b"".join(
                [chunk async for chunk in weak_if_range.body_iterator]
            )
            not_modified = await playback_v2(
                _request(if_none_match=weak_etag),
                "config",
                "pa2.payload.signature",
            )
            strong_etag = f'"ar1-{"d" * 64}"'
            preparation.target_ref["strong_asset_revision"] = "d" * 64
            engine.read_session_range.side_effect = [b"cdef", b"abcdef"]
            strong_partial = await playback_v2(
                _request(
                    range_value="bytes=2-99",
                    if_range=strong_etag,
                ),
                "config",
                "pa2.payload.signature",
            )
            strong_partial_body = b"".join(
                [chunk async for chunk in strong_partial.body_iterator]
            )
            stale_strong = await playback_v2(
                _request(
                    range_value="bytes=2-99",
                    if_range=f'"ar1-{"e" * 64}"',
                ),
                "config",
                "pa2.payload.signature",
            )
            stale_strong_body = b"".join(
                [chunk async for chunk in stale_strong.body_iterator]
            )
            strong_not_modified = await playback_v2(
                _request(if_none_match=strong_etag),
                "config",
                "pa2.payload.signature",
            )

        self.assertEqual(head_response.status_code, 206)
        self.assertEqual(head_response.headers["content-range"], "bytes 1-2/6")
        self.assertEqual(head_response.headers["content-length"], "2")
        self.assertEqual(head_response.headers["cache-control"], "private, no-store")
        self.assertEqual(head_response.headers["referrer-policy"], "no-referrer")
        self.assertEqual(
            head_response.headers["content-type"], "application/octet-stream"
        )
        self.assertEqual(
            head_response.headers["content-disposition"],
            "inline; filename*=UTF-8''Caf%C3%A9%20%5B2026%5D.mkv",
        )
        self.assertEqual(head_body, b"")
        self.assertEqual(
            (full_response.status_code, full_response.headers["content-length"]),
            (200, "6"),
        )
        self.assertEqual(full_response.headers["etag"], weak_etag)
        self.assertEqual(full_response.headers["cache-control"], "private, no-store")
        self.assertEqual(full_response.headers["referrer-policy"], "no-referrer")
        self.assertEqual(
            full_response.headers["content-type"], "application/octet-stream"
        )
        self.assertNotIn("content-range", full_response.headers)
        self.assertEqual(full_body, b"abcdef")
        self.assertEqual(
            (partial_response.status_code, partial_response.headers["content-range"]),
            (206, "bytes 2-5/6"),
        )
        self.assertEqual(partial_response.headers["cache-control"], "private, no-store")
        self.assertEqual(partial_response.headers["referrer-policy"], "no-referrer")
        self.assertEqual(partial_body, b"cdef")
        self.assertEqual(if_range_miss.status_code, 200)
        self.assertEqual(if_range_miss.headers["cache-control"], "private, no-store")
        self.assertEqual(if_range_miss.headers["referrer-policy"], "no-referrer")
        self.assertNotIn("content-range", if_range_miss.headers)
        self.assertEqual(if_range_miss_body, b"abcdef")
        self.assertEqual(weak_if_range.status_code, 200)
        self.assertNotIn("content-range", weak_if_range.headers)
        self.assertEqual(weak_if_range_body, b"abcdef")
        self.assertEqual(
            (not_modified.status_code, not_modified.headers["etag"]),
            (304, weak_etag),
        )
        self.assertEqual(not_modified.headers["cache-control"], "private, no-store")
        self.assertEqual(not_modified.headers["referrer-policy"], "no-referrer")
        self.assertEqual(
            not_modified.headers["content-disposition"],
            "inline; filename*=UTF-8''Caf%C3%A9%20%5B2026%5D.mkv",
        )
        self.assertEqual(strong_partial.status_code, 206)
        self.assertEqual(
            strong_partial.headers["content-range"],
            "bytes 2-5/6",
        )
        self.assertEqual(strong_partial.headers["etag"], strong_etag)
        self.assertEqual(strong_partial_body, b"cdef")
        self.assertEqual(stale_strong.status_code, 200)
        self.assertNotIn("content-range", stale_strong.headers)
        self.assertEqual(stale_strong.headers["etag"], strong_etag)
        self.assertEqual(stale_strong_body, b"abcdef")
        self.assertEqual(strong_not_modified.status_code, 304)
        self.assertEqual(strong_not_modified.headers["etag"], strong_etag)
        advance_native.assert_not_awaited()
        self.assertEqual(engine.open_session_reader.await_count, 6)
        self.assertEqual(engine.close_session_reader.await_count, 6)

    async def test_native_unsatisfiable_range_is_an_http_416(self):
        preparation_id = "22222222-2222-4222-8222-222222222222"
        preparation = type(
            "Preparation",
            (),
            {
                "preparation_id": preparation_id,
                "provider_kind": "comet_native_usenet",
                "state": "ready",
                "target_ref": {
                    "source_kind": "session",
                    "session_id": "B" * 22,
                    "byte_size": 6,
                    "session_revision": "b" * 64,
                    "relative_path": "movie.mkv",
                },
            },
        )()
        prepared = type(
            "Prepared",
            (),
            {
                "preparation": preparation,
                "resolution": _resolution(),
            },
        )()
        with (
            patch(
                "comet.api.endpoints.usenet_playback.config_check",
                return_value={"schemaVersion": 2},
            ),
            patch("comet.api.endpoints.usenet_playback.settings.USENET_ENABLED", True),
            patch(
                "comet.api.endpoints.usenet_playback.settings.COMET_CAPABILITY_SECRET",
                ROOT,
            ),
            patch(
                "comet.api.endpoints.usenet_playback.http_client_manager.get_session",
                AsyncMock(return_value=object()),
            ),
            patch(
                "comet.api.endpoints.usenet_playback.resolve_prepared_asset",
                AsyncMock(return_value=prepared),
            ),
            patch(
                "comet.playback.tokens.CapabilityCodec.configuration_partition_for_config",
                return_value=b"a" * 32,
            ),
            patch(
                "comet.api.endpoints.usenet_playback._advance_native_usenet",
                AsyncMock(return_value="ready"),
            ),
        ):
            with self.assertRaisesRegex(Exception, "Requested range") as caught:
                await playback_v2(
                    _request(range_value="bytes=6-7"), "config", "pa2.payload.signature"
                )

        self.assertEqual(caught.exception.status_code, 416)
        self.assertEqual(caught.exception.headers["Content-Range"], "bytes */6")
        self.assertEqual(
            caught.exception.headers["Cache-Control"],
            "private, no-store",
        )
        self.assertEqual(
            caught.exception.headers["Referrer-Policy"],
            "no-referrer",
        )

    async def test_ready_native_recreates_only_an_expired_replica_local_session(self):
        preparation_id = "22222222-2222-4222-8222-222222222222"

        def prepared(identity):
            return type(
                "Prepared",
                (),
                {
                    "resolution": _resolution(),
                    "preparation": type(
                        "Preparation",
                        (),
                        {
                            "preparation_id": preparation_id,
                            "provider_kind": "comet_native_usenet",
                            "state": "ready",
                            "target_ref": {
                                "source_kind": "session",
                                "session_id": identity,
                                "byte_size": 6,
                                "session_revision": "b" * 64,
                                "relative_path": "movie.mkv",
                            },
                        },
                    )(),
                },
            )()

        expired = prepared("B" * 22)
        recreated = prepared("C" * 22)
        engine = type(
            "Engine",
            (),
            {
                "open_session_reader": AsyncMock(
                    side_effect=[
                        EngineNntpError("session_unavailable", retryable=True),
                        "L" * 22,
                    ]
                ),
                "read_session_range": AsyncMock(return_value=b"abcdef"),
                "close_session_reader": AsyncMock(),
            },
        )()
        with (
            patch(
                "comet.api.endpoints.usenet_playback.config_check",
                return_value={"schemaVersion": 2},
            ),
            patch(
                "comet.api.endpoints.usenet_playback.settings.COMET_CAPABILITY_SECRET",
                ROOT,
            ),
            patch(
                "comet.api.endpoints.usenet_playback.http_client_manager.get_session",
                AsyncMock(return_value=object()),
            ),
            patch(
                "comet.api.endpoints.usenet_playback.resolve_prepared_asset",
                AsyncMock(side_effect=[expired, recreated]),
            ),
            patch(
                "comet.playback.tokens.CapabilityCodec."
                "configuration_partition_for_config",
                return_value=b"a" * 32,
            ),
            patch(
                "comet.api.endpoints.usenet_playback.EngineClient",
                return_value=engine,
            ),
            patch(
                "comet.api.endpoints.usenet_playback._advance_native_usenet",
                AsyncMock(return_value="ready"),
            ) as advance,
        ):
            response = await playback_v2(
                _request(),
                "config",
                "pa2.payload.signature",
            )
            body = b"".join([chunk async for chunk in response.body_iterator])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(body, b"abcdef")
        advance.assert_awaited_once()
        self.assertEqual(
            [call.args for call in engine.open_session_reader.await_args_list],
            [("B" * 22,), ("C" * 22,)],
        )
        engine.read_session_range.assert_awaited_once_with(
            "C" * 22,
            "L" * 22,
            6,
            0,
            5,
        )
        engine.close_session_reader.assert_awaited_once_with(
            "C" * 22,
            "L" * 22,
        )

    async def test_native_capacity_rejection_stays_retryable_without_disabling_binding(
        self,
    ):
        provider_id = "11111111-1111-4111-8111-111111111111"
        preparation_id = "22222222-2222-4222-8222-222222222222"
        prepared = type(
            "Prepared",
            (),
            {
                "resolution": _resolution(),
                "preparation": type(
                    "Preparation",
                    (),
                    {
                        "preparation_id": preparation_id,
                        "provider_kind": "comet_native_usenet",
                        "provider_configuration_id": provider_id,
                        "state": "pending",
                    },
                )(),
            },
        )()
        with (
            patch(
                "comet.api.endpoints.usenet_playback.config_check",
                return_value={"schemaVersion": 2},
            ),
            patch(
                "comet.api.endpoints.usenet_playback.settings.USENET_ENABLED",
                True,
            ),
            patch(
                "comet.api.endpoints.usenet_playback.settings.COMET_CAPABILITY_SECRET",
                ROOT,
            ),
            patch(
                "comet.api.endpoints.usenet_playback.http_client_manager.get_session",
                AsyncMock(return_value=object()),
            ),
            patch(
                "comet.api.endpoints.usenet_playback.resolve_prepared_asset",
                AsyncMock(return_value=prepared),
            ),
            patch(
                "comet.playback.tokens.CapabilityCodec."
                "configuration_partition_for_config",
                return_value=b"a" * 32,
            ),
            patch(
                "comet.api.endpoints.usenet_playback._advance_native_usenet",
                AsyncMock(
                    side_effect=EngineNntpError(
                        "native_busy",
                        retryable=True,
                    )
                ),
            ),
            patch(
                "comet.api.endpoints.usenet_playback."
                "record_playback_capability_failure",
                AsyncMock(),
            ) as record_failure,
            patch(
                "comet.api.endpoints.usenet_playback."
                "PlaybackPreparationRepository.mark_failed",
                AsyncMock(),
            ) as marked,
        ):
            response = await playback_v2(
                _request(),
                "config",
                "pa2.payload.signature",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["retry-after"], "2")
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        record_failure.assert_not_awaited()
        marked.assert_not_awaited()

    async def test_native_engine_outage_degrades_the_exact_instance_binding(self):
        provider_id = "11111111-1111-4111-8111-111111111111"
        prepared = type(
            "Prepared",
            (),
            {
                "resolution": _resolution(),
                "preparation": type(
                    "Preparation",
                    (),
                    {
                        "preparation_id": ("22222222-2222-4222-8222-222222222222"),
                        "provider_kind": "comet_native_usenet",
                        "provider_configuration_id": provider_id,
                        "state": "pending",
                    },
                )(),
            },
        )()
        config = {"schemaVersion": 2}
        with (
            patch(
                "comet.api.endpoints.usenet_playback.config_check",
                return_value=config,
            ),
            patch(
                "comet.api.endpoints.usenet_playback.settings.USENET_ENABLED",
                True,
            ),
            patch(
                "comet.api.endpoints.usenet_playback.settings.COMET_CAPABILITY_SECRET",
                ROOT,
            ),
            patch(
                "comet.api.endpoints.usenet_playback.settings."
                "USENET_NATIVE_ACCESS_TOKEN",
                "native-token",
            ),
            patch(
                "comet.api.endpoints.usenet_playback.settings.USENET_NATIVE_SERVERS",
                [{"host": "news.example.test"}],
            ),
            patch(
                "comet.api.endpoints.usenet_playback.http_client_manager.get_session",
                AsyncMock(return_value=object()),
            ),
            patch(
                "comet.api.endpoints.usenet_playback.resolve_prepared_asset",
                AsyncMock(return_value=prepared),
            ),
            patch(
                "comet.playback.tokens.CapabilityCodec."
                "configuration_partition_for_config",
                return_value=b"a" * 32,
            ),
            patch(
                "comet.api.endpoints.usenet_playback._advance_native_usenet",
                AsyncMock(side_effect=EngineUnavailable("engine restarted")),
            ),
            patch(
                "comet.api.endpoints.usenet_playback."
                "record_playback_capability_failure",
                AsyncMock(),
            ) as record_failure,
        ):
            response = await playback_v2(
                _request(),
                "config",
                "pa2.payload.signature",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["retry-after"], "30")
        record_failure.assert_awaited_once_with(
            config,
            ANY,
            ANY,
            provider_id,
            state="transiently_unreachable",
            error_code="native_engine_unavailable",
            retry_after=30,
            instance_credential_material={
                "comet_native_usenet": ANY,
            },
        )

    async def test_native_auth_rejection_invalidates_the_exact_instance_binding(
        self,
    ):
        provider_id = "11111111-1111-4111-8111-111111111111"
        preparation_id = "22222222-2222-4222-8222-222222222222"
        prepared = type(
            "Prepared",
            (),
            {
                "resolution": _resolution(),
                "preparation": type(
                    "Preparation",
                    (),
                    {
                        "preparation_id": preparation_id,
                        "provider_kind": "comet_native_usenet",
                        "provider_configuration_id": provider_id,
                        "state": "pending",
                    },
                )(),
            },
        )()
        config = {"schemaVersion": 2}
        with (
            patch(
                "comet.api.endpoints.usenet_playback.config_check",
                return_value=config,
            ),
            patch(
                "comet.api.endpoints.usenet_playback.settings.USENET_ENABLED",
                True,
            ),
            patch(
                "comet.api.endpoints.usenet_playback.settings.COMET_CAPABILITY_SECRET",
                ROOT,
            ),
            patch(
                "comet.api.endpoints.usenet_playback.settings."
                "USENET_NATIVE_ACCESS_TOKEN",
                "native-token",
            ),
            patch(
                "comet.api.endpoints.usenet_playback.settings.USENET_NATIVE_SERVERS",
                [{"host": "news.example.test"}],
            ),
            patch(
                "comet.api.endpoints.usenet_playback.http_client_manager.get_session",
                AsyncMock(return_value=object()),
            ),
            patch(
                "comet.api.endpoints.usenet_playback.resolve_prepared_asset",
                AsyncMock(return_value=prepared),
            ),
            patch(
                "comet.playback.tokens.CapabilityCodec."
                "configuration_partition_for_config",
                return_value=b"a" * 32,
            ),
            patch(
                "comet.api.endpoints.usenet_playback._advance_native_usenet",
                AsyncMock(side_effect=EngineNntpError("nntp_auth_failed")),
            ),
            patch(
                "comet.api.endpoints.usenet_playback."
                "record_playback_capability_failure",
                AsyncMock(),
            ) as record_failure,
            patch(
                "comet.api.endpoints.usenet_playback."
                "PlaybackPreparationRepository.mark_failed",
                AsyncMock(),
            ) as marked,
        ):
            response = await playback_v2(
                _request(),
                "config",
                "pa2.payload.signature",
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(str(response.path).endswith("INVALID_ACCOUNT_OR_PASSWORD.mp4"))
        marked.assert_awaited_once_with(
            preparation_id,
            owner_configuration_partition=b"a" * 32,
            provider_account_partition=ACCOUNT_PARTITION,
            code="credentials_rejected",
        )
        record_failure.assert_awaited_once_with(
            config,
            ANY,
            ANY,
            provider_id,
            state="auth_failed",
            error_code="credentials_rejected",
            retry_after=None,
            instance_credential_material={
                "comet_native_usenet": ANY,
            },
        )

    async def test_ready_archive_materialization_uses_the_native_range_contract(self):
        preparation_id = "22222222-2222-4222-8222-222222222222"
        strong_etag = f'"ar1-{"b" * 64}"'
        preparation = type(
            "Preparation",
            (),
            {
                "preparation_id": preparation_id,
                "provider_kind": "comet_native_usenet",
                "state": "ready",
                "target_ref": {
                    "source_kind": "raw_composite",
                    "raw_composite_id": "a" * 64,
                    "byte_size": 6,
                    "asset_revision": "a" * 64,
                    "strong_asset_revision": "b" * 64,
                    "relative_path": "movie.mkv",
                },
            },
        )()
        prepared = type(
            "Prepared",
            (),
            {
                "preparation": preparation,
                "resolution": _resolution(),
            },
        )()
        engine = type(
            "Engine",
            (),
            {
                "open_raw_composite_reader": AsyncMock(return_value="L" * 22),
                "read_raw_composite_range": AsyncMock(
                    side_effect=[b"cdef", b"cdef", b"abcdef"]
                ),
                "close_raw_composite_reader": AsyncMock(),
            },
        )()
        artifact_readers = [
            type("ArtifactReader", (), {"close": AsyncMock()})() for _ in range(3)
        ]
        with (
            patch(
                "comet.api.endpoints.usenet_playback.config_check",
                return_value={"schemaVersion": 2},
            ),
            patch("comet.api.endpoints.usenet_playback.settings.USENET_ENABLED", True),
            patch(
                "comet.api.endpoints.usenet_playback.settings.COMET_CAPABILITY_SECRET",
                ROOT,
            ),
            patch(
                "comet.api.endpoints.usenet_playback.http_client_manager.get_session",
                AsyncMock(return_value=object()),
            ),
            patch(
                "comet.api.endpoints.usenet_playback.resolve_prepared_asset",
                AsyncMock(return_value=prepared),
            ),
            patch(
                "comet.api.endpoints.usenet_playback.EngineClient",
                return_value=engine,
            ),
            patch(
                "comet.playback.tokens.CapabilityCodec.configuration_partition_for_config",
                return_value=b"a" * 32,
            ),
            patch(
                "comet.api.endpoints.usenet_playback._advance_native_usenet",
                AsyncMock(return_value="ready"),
            ),
            patch(
                "comet.api.endpoints.usenet_playback.MaterializedArtifactRepository."
                "acquire_for_preparation",
                AsyncMock(side_effect=[(reader,) for reader in artifact_readers]),
            ),
        ):
            response = await playback_v2(
                _request(range_value="bytes=2-5"),
                "config",
                "pa2.payload.signature",
            )
            body = b"".join([chunk async for chunk in response.body_iterator])
            matching = await playback_v2(
                _request(range_value="bytes=2-5", if_range=strong_etag),
                "config",
                "pa2.payload.signature",
            )
            matching_body = b"".join([chunk async for chunk in matching.body_iterator])
            stale = await playback_v2(
                _request(
                    range_value="bytes=2-5",
                    if_range=f'"ar1-{"c" * 64}"',
                ),
                "config",
                "pa2.payload.signature",
            )
            stale_body = b"".join([chunk async for chunk in stale.body_iterator])
            not_modified = await playback_v2(
                _request(if_none_match=strong_etag),
                "config",
                "pa2.payload.signature",
            )

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["content-range"], "bytes 2-5/6")
        self.assertEqual(response.headers["etag"], strong_etag)
        self.assertEqual(body, b"cdef")
        self.assertEqual(matching.status_code, 206)
        self.assertEqual(matching.headers["content-range"], "bytes 2-5/6")
        self.assertEqual(matching_body, b"cdef")
        self.assertEqual(stale.status_code, 200)
        self.assertNotIn("content-range", stale.headers)
        self.assertEqual(stale_body, b"abcdef")
        self.assertEqual(not_modified.status_code, 304)
        self.assertEqual(not_modified.headers["etag"], strong_etag)
        self.assertEqual(engine.open_raw_composite_reader.await_count, 3)
        self.assertEqual(engine.close_raw_composite_reader.await_count, 3)
        for reader in artifact_readers:
            reader.close.assert_awaited_once()
        engine.open_raw_composite_reader.assert_has_awaits(
            [call("a" * 64), call("a" * 64), call("a" * 64)]
        )

    async def test_pi2_redirects_to_a_relative_owner_bound_pa2_without_trusting_host(
        self,
    ):
        prepared = _pending_prepared()
        with (
            patch(
                "comet.api.endpoints.usenet_playback.config_check",
                return_value={"schemaVersion": 2},
            ),
            patch("comet.api.endpoints.usenet_playback.settings.USENET_ENABLED", True),
            patch(
                "comet.api.endpoints.usenet_playback.settings.COMET_CAPABILITY_SECRET",
                "a" * 43,
            ),
            patch(
                "comet.api.endpoints.usenet_playback.http_client_manager.get_session",
                AsyncMock(return_value=object()),
            ),
            patch(
                "comet.api.endpoints.usenet_playback.create_playback_preparation",
                AsyncMock(return_value=prepared),
            ),
            patch(
                "comet.api.endpoints.usenet_playback.settings.PUBLIC_BASE_URL",
                None,
            ),
        ):
            response = await playback_v2(_request(), "config", "pi2.payload.signature")

        self.assertEqual(response.status_code, 307)
        self.assertEqual(
            response.headers["location"],
            "/config/playback/v2/pa2.payload.signature",
        )
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")

    async def test_pi2_redirect_honors_the_explicit_public_base_url(self):
        prepared = _pending_prepared()
        with (
            patch(
                "comet.api.endpoints.usenet_playback.config_check",
                return_value={"schemaVersion": 2},
            ),
            patch(
                "comet.api.endpoints.usenet_playback.settings.COMET_CAPABILITY_SECRET",
                "a" * 43,
            ),
            patch(
                "comet.api.endpoints.usenet_playback.http_client_manager.get_session",
                AsyncMock(return_value=object()),
            ),
            patch(
                "comet.api.endpoints.usenet_playback.create_playback_preparation",
                AsyncMock(return_value=prepared),
            ),
            patch(
                "comet.api.endpoints.usenet_playback.settings.PUBLIC_BASE_URL",
                "https://public.example",
            ),
        ):
            response = await playback_v2(_request(), "config", "pi2.payload.signature")

        self.assertEqual(
            response.headers["location"],
            "https://public.example/config/playback/v2/pa2.payload.signature",
        )

    async def test_bridge_runtime_failures_update_the_exact_capability(self):
        provider_id = "11111111-1111-4111-8111-111111111111"
        preparation_id = "22222222-2222-4222-8222-222222222222"
        config = {"schemaVersion": 2}
        cases = (
            (
                "nzbdav",
                "remote_download_url",
                NzbDavError(
                    "nzbdav_credentials_rejected",
                    auth_failed=True,
                    terminal=True,
                ),
                "auth",
            ),
            (
                "nzbdav",
                "remote_download_url",
                NzbDavError("nzbdav_invalid_response", retryable=True),
                "transient",
            ),
            (
                "nzbdav",
                "remote_download_url",
                NzbDavError("nzbdav_submission_failed", terminal=True),
                "terminal",
            ),
            (
                "altmount",
                "remote_download_url",
                AltMountError(
                    "altmount_credentials_rejected",
                    auth_failed=True,
                    terminal_status="failed",
                ),
                "auth",
            ),
            (
                "altmount",
                "remote_download_url",
                AltMountError("altmount_unavailable", retryable=True),
                "transient",
            ),
        )
        for provider_kind, target_name, error, disposition in cases:
            error_code = error.code
            with self.subTest(
                provider_kind=provider_kind,
                error_code=error_code,
            ):
                prepared = type(
                    "Prepared",
                    (),
                    {
                        "resolution": _resolution(),
                        "preparation": type(
                            "Preparation",
                            (),
                            {
                                "preparation_id": preparation_id,
                                "provider_kind": provider_kind,
                                "provider_configuration_id": provider_id,
                                "state": "ready",
                            },
                        )(),
                    },
                )()
                with (
                    patch(
                        "comet.api.endpoints.usenet_playback.config_check",
                        return_value=config,
                    ),
                    patch(
                        "comet.api.endpoints.usenet_playback.settings.USENET_ENABLED",
                        True,
                    ),
                    patch(
                        "comet.api.endpoints.usenet_playback.settings."
                        "COMET_CAPABILITY_SECRET",
                        ROOT,
                    ),
                    patch(
                        "comet.api.endpoints.usenet_playback."
                        "http_client_manager.get_session",
                        AsyncMock(return_value=object()),
                    ),
                    patch(
                        "comet.api.endpoints.usenet_playback.resolve_prepared_asset",
                        AsyncMock(return_value=prepared),
                    ),
                    patch(
                        "comet.playback.tokens.CapabilityCodec."
                        "configuration_partition_for_config",
                        return_value=b"a" * 32,
                    ),
                    patch(
                        f"comet.api.endpoints.usenet_playback.{target_name}",
                        AsyncMock(side_effect=error),
                    ),
                    patch(
                        "comet.api.endpoints.usenet_playback."
                        "record_playback_capability_failure",
                        AsyncMock(),
                    ) as record_failure,
                    patch(
                        "comet.api.endpoints.usenet_playback."
                        "PlaybackPreparationRepository.mark_failed",
                        AsyncMock(),
                    ) as marked,
                ):
                    if disposition in {"auth", "terminal"}:
                        with self.assertRaisesRegex(
                            Exception,
                            "Playback capability is unavailable",
                        ) as raised:
                            await playback_v2(
                                _request(),
                                "config",
                                "pa2.payload.signature",
                            )
                        self.assertEqual(raised.exception.status_code, 404)
                        marked.assert_awaited_once_with(
                            preparation_id,
                            owner_configuration_partition=b"a" * 32,
                            provider_account_partition=ACCOUNT_PARTITION,
                            code=(
                                "credentials_rejected"
                                if disposition == "auth"
                                else error_code
                            ),
                        )
                    else:
                        response = await playback_v2(
                            _request(),
                            "config",
                            "pa2.payload.signature",
                        )
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.headers["retry-after"], "30")
                        marked.assert_not_awaited()

                if disposition == "terminal":
                    record_failure.assert_not_awaited()
                else:
                    record_failure.assert_awaited_once_with(
                        config,
                        ANY,
                        ANY,
                        provider_id,
                        state=(
                            "auth_failed"
                            if disposition == "auth"
                            else "transiently_unreachable"
                        ),
                        error_code=(
                            "credentials_rejected"
                            if disposition == "auth"
                            else error_code
                        ),
                        retry_after=(None if disposition == "auth" else 30),
                    )

    async def test_stremthru_runtime_capacity_failure_is_persisted(self):
        provider_id = "11111111-1111-4111-8111-111111111111"
        prepared = type(
            "Prepared",
            (),
            {
                "resolution": _resolution(),
                "preparation": type(
                    "Preparation",
                    (),
                    {
                        "preparation_id": ("22222222-2222-4222-8222-222222222222"),
                        "provider_kind": "stremthru_newz",
                        "provider_configuration_id": provider_id,
                        "state": "ready",
                    },
                )(),
            },
        )()
        config = {"schemaVersion": 2}
        with (
            patch(
                "comet.api.endpoints.usenet_playback.config_check",
                return_value=config,
            ),
            patch(
                "comet.api.endpoints.usenet_playback.settings.USENET_ENABLED",
                True,
            ),
            patch(
                "comet.api.endpoints.usenet_playback.settings.COMET_CAPABILITY_SECRET",
                ROOT,
            ),
            patch(
                "comet.api.endpoints.usenet_playback.http_client_manager.get_session",
                AsyncMock(return_value=object()),
            ),
            patch(
                "comet.api.endpoints.usenet_playback.resolve_prepared_asset",
                AsyncMock(return_value=prepared),
            ),
            patch(
                "comet.playback.tokens.CapabilityCodec."
                "configuration_partition_for_config",
                return_value=b"a" * 32,
            ),
            patch(
                "comet.api.endpoints.usenet_playback.remote_download_url",
                AsyncMock(
                    side_effect=StremThruNewzError(
                        "stremthru_rate_limited",
                        retryable=True,
                        retry_after=12,
                    )
                ),
            ),
            patch(
                "comet.api.endpoints.usenet_playback.get_cached_download_link",
                AsyncMock(return_value=None),
            ),
            patch(
                "comet.api.endpoints.usenet_playback."
                "record_playback_capability_failure",
                AsyncMock(),
            ) as record_failure,
        ):
            response = await playback_v2(
                _request(),
                "config",
                "pa2.payload.signature",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        self.assertEqual(response.headers["retry-after"], "12")
        record_failure.assert_awaited_once_with(
            config,
            ANY,
            ANY,
            provider_id,
            state="transiently_unreachable",
            error_code="stremthru_rate_limited",
            retry_after=12,
        )

    async def test_torbox_runtime_auth_failure_invalidates_exact_binding(self):
        provider_id = "11111111-1111-4111-8111-111111111111"
        preparation_id = "22222222-2222-4222-8222-222222222222"
        prepared = type(
            "Prepared",
            (),
            {
                "resolution": _resolution(),
                "preparation": type(
                    "Preparation",
                    (),
                    {
                        "preparation_id": preparation_id,
                        "provider_kind": "torbox_usenet",
                        "provider_configuration_id": provider_id,
                        "state": "ready",
                        "target_ref": {"file_id": 3},
                    },
                )(),
            },
        )()
        config = {"schemaVersion": 2}
        with (
            patch(
                "comet.api.endpoints.usenet_playback.config_check",
                return_value=config,
            ),
            patch(
                "comet.api.endpoints.usenet_playback.settings.USENET_ENABLED",
                True,
            ),
            patch(
                "comet.api.endpoints.usenet_playback.settings.COMET_CAPABILITY_SECRET",
                ROOT,
            ),
            patch(
                "comet.api.endpoints.usenet_playback.http_client_manager.get_session",
                AsyncMock(return_value=object()),
            ),
            patch(
                "comet.api.endpoints.usenet_playback.resolve_prepared_asset",
                AsyncMock(return_value=prepared),
            ),
            patch(
                "comet.playback.tokens.CapabilityCodec."
                "configuration_partition_for_config",
                return_value=b"a" * 32,
            ),
            patch(
                "comet.api.endpoints.usenet_playback.remote_download_url",
                AsyncMock(
                    side_effect=TorBoxUsenetError(
                        "torbox_auth_failed",
                        auth_failed=True,
                    )
                ),
            ),
            patch(
                "comet.api.endpoints.usenet_playback.get_cached_download_link",
                AsyncMock(return_value=None),
            ),
            patch(
                "comet.api.endpoints.usenet_playback."
                "record_playback_capability_failure",
                AsyncMock(),
            ) as record_failure,
            patch(
                "comet.api.endpoints.usenet_playback."
                "PlaybackPreparationRepository.mark_failed",
                AsyncMock(),
            ) as marked,
        ):
            with self.assertRaisesRegex(
                Exception,
                "Playback capability is unavailable",
            ) as raised:
                await playback_v2(
                    _request(),
                    "config",
                    "pa2.payload.signature",
                )

        self.assertEqual(raised.exception.status_code, 404)
        marked.assert_awaited_once_with(
            preparation_id,
            owner_configuration_partition=b"a" * 32,
            provider_account_partition=ACCOUNT_PARTITION,
            code="credentials_rejected",
        )
        record_failure.assert_awaited_once_with(
            config,
            ANY,
            ANY,
            provider_id,
            state="auth_failed",
            error_code="credentials_rejected",
            retry_after=None,
        )
