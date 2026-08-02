import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from mediaflow_proxy.utils.http_utils import EnhancedStreamingResponse

from comet.services.streaming.manager import (
    add_active_connection,
    admit_active_connection,
    check_ip_connections,
    combined_background_tasks,
    custom_handle_stream_request,
    on_stream_end,
)
from comet.services.streaming.wrapper import monitored_handle_stream_request


class StreamingLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_connection_limit_check_and_insert_are_serialized(self):
        admission_lock = asyncio.Lock()
        active_connections = 0

        class Lock:
            def __init__(self, *args, **kwargs):
                pass

            async def acquire(self, wait_timeout=None):
                await admission_lock.acquire()
                return True

            async def release(self):
                admission_lock.release()

        async def check(ip):
            observed = active_connections
            await asyncio.sleep(0.01)
            return observed < 1

        async def add(media_id, ip, service):
            nonlocal active_connections
            active_connections += 1
            return f"connection-{active_connections}"

        with (
            patch(
                "comet.services.streaming.manager.settings.PROXY_DEBRID_STREAM_MAX_CONNECTIONS",
                1,
            ),
            patch(
                "comet.services.streaming.manager.DistributedLock",
                new=Lock,
            ),
            patch(
                "comet.services.streaming.manager.check_ip_connections",
                new=check,
            ),
            patch(
                "comet.services.streaming.manager.add_active_connection",
                new=add,
            ),
        ):
            results = await asyncio.gather(
                admit_active_connection("first", "127.0.0.1", "realdebrid"),
                admit_active_connection("second", "127.0.0.1", "realdebrid"),
            )

        self.assertEqual(results, ["connection-1", None])
        self.assertEqual(active_connections, 1)

    async def test_end_tracking_failure_does_not_skip_database_cleanup(self):
        execute = AsyncMock()
        secret = "signed-provider-url-secret"
        with (
            patch(
                "comet.services.streaming.manager.bandwidth_monitor.end_connection",
                new=AsyncMock(side_effect=RuntimeError(secret)),
            ),
            patch(
                "comet.services.streaming.manager.database.execute",
                new=execute,
            ),
            patch("comet.services.streaming.manager.log.warning") as warning,
        ):
            await on_stream_end(
                "private-connection-id",
                "192.0.2.10",
                outcome="completed",
            )

        execute.assert_awaited_once()
        warning.assert_called_once_with(
            "stream.cleanup.failed",
            "Stream cleanup failed",
            operation="bandwidth_cleanup",
            failure_reason="bandwidth_cleanup",
            details="RuntimeError",
            error_code="dependency_warning",
        )
        self.assertNotIn(secret, str(warning.call_args))
        self.assertNotIn("private-connection-id", str(warning.call_args))
        self.assertNotIn("192.0.2.10", str(warning.call_args))

    async def test_end_tracking_cancellation_cleans_database_then_propagates(self):
        execute = AsyncMock()
        with (
            patch(
                "comet.services.streaming.manager.bandwidth_monitor.end_connection",
                new=AsyncMock(side_effect=asyncio.CancelledError),
            ),
            patch(
                "comet.services.streaming.manager.database.execute",
                new=execute,
            ),
            self.assertRaises(asyncio.CancelledError),
        ):
            await on_stream_end(
                "connection",
                "127.0.0.1",
                outcome="completed",
            )

        execute.assert_awaited_once()

    async def test_connection_limit_database_failure_is_not_a_false_quota_hit(self):
        with (
            patch(
                "comet.services.streaming.manager.settings.PROXY_DEBRID_STREAM_MAX_CONNECTIONS",
                1,
            ),
            patch(
                "comet.services.streaming.manager.database.fetch_val",
                new=AsyncMock(side_effect=RuntimeError("database failed")),
            ),
            self.assertRaisesRegex(RuntimeError, "database failed"),
        ):
            await check_ip_connections("127.0.0.1")

    async def test_start_tracking_failure_removes_database_connection(self):
        execute = AsyncMock()
        with (
            patch(
                "comet.services.streaming.manager.database.execute",
                new=execute,
            ),
            patch(
                "comet.services.streaming.manager.bandwidth_monitor.start_connection",
                new=AsyncMock(side_effect=RuntimeError("tracking failed")),
            ),
            self.assertRaisesRegex(RuntimeError, "tracking failed"),
        ):
            await add_active_connection("tt123", "127.0.0.1", "realdebrid")

        self.assertEqual(execute.await_count, 2)
        self.assertIn(
            "INSERT INTO active_connections", execute.await_args_list[0].args[0]
        )
        self.assertIn(
            "DELETE FROM active_connections", execute.await_args_list[1].args[0]
        )

    async def test_connection_identity_does_not_embed_ip_or_media(self):
        media_id = "private-media-id"
        ip = "192.0.2.25"
        with (
            patch(
                "comet.services.streaming.manager.database.execute",
                new=AsyncMock(),
            ),
            patch(
                "comet.services.streaming.manager.bandwidth_monitor.start_connection",
                new=AsyncMock(),
            ),
        ):
            connection_id = await add_active_connection(media_id, ip, "realdebrid")
        self.assertNotIn(media_id, connection_id)
        self.assertNotIn(ip, connection_id)

    async def test_cleanup_runs_without_upstream_background_task(self):
        cleanup = AsyncMock()

        with patch(
            "comet.services.streaming.manager.on_stream_end",
            new=cleanup,
        ):
            await combined_background_tasks("connection", "127.0.0.1", None)

        cleanup.assert_awaited_once_with(
            "connection",
            "127.0.0.1",
            outcome="completed",
            error_code=None,
        )

    async def test_cleanup_runs_when_upstream_background_fails(self):
        upstream = AsyncMock(side_effect=RuntimeError("close failed"))
        cleanup = AsyncMock()

        with (
            patch(
                "comet.services.streaming.manager.on_stream_end",
                new=cleanup,
            ),
            self.assertRaisesRegex(RuntimeError, "close failed"),
        ):
            await combined_background_tasks("connection", "127.0.0.1", upstream)

        cleanup.assert_awaited_once_with(
            "connection",
            "127.0.0.1",
            outcome="failed",
            error_code="upstream_close_failed",
        )

    async def test_response_creation_failure_cleans_registered_connection(self):
        cleanup = AsyncMock()
        with (
            patch(
                "comet.services.streaming.manager.admit_active_connection",
                new=AsyncMock(return_value="connection"),
            ),
            patch(
                "comet.services.streaming.manager.monitored_handle_stream_request",
                new=AsyncMock(side_effect=RuntimeError("proxy failed")),
            ),
            patch(
                "comet.services.streaming.manager.on_stream_end",
                new=cleanup,
            ),
            self.assertRaisesRegex(RuntimeError, "proxy failed"),
        ):
            await custom_handle_stream_request(
                "GET",
                "https://video.test",
                Mock(),
                "tt123",
                "127.0.0.1",
                "realdebrid",
            )

        cleanup.assert_awaited_once_with(
            "connection",
            "127.0.0.1",
            outcome="failed",
            error_code="upstream_request_failed",
        )

    async def test_monitor_delegates_background_and_counts_body_once(self):
        upstream_background = AsyncMock()

        async def body():
            yield b"abc"
            yield "é"

        upstream = EnhancedStreamingResponse(
            body(),
            background=upstream_background,
        )

        with (
            patch(
                "comet.services.streaming.wrapper.mediaflow_proxy.handlers.handle_stream_request",
                new=AsyncMock(return_value=upstream),
            ),
            patch(
                "comet.services.streaming.wrapper.bandwidth_monitor.update_connection"
            ) as update,
        ):
            response = await monitored_handle_stream_request(
                "GET", "https://video.test", Mock(), "connection"
            )
            chunks = [chunk async for chunk in response.body_iterator]

        self.assertEqual(chunks, [b"abc", "é"])
        self.assertIs(response.background, upstream_background)
        upstream_background.assert_not_awaited()
        self.assertEqual(update.call_args_list[0].args, ("connection", 3))
        self.assertEqual(update.call_args_list[1].args, ("connection", 2))

    async def test_client_disconnect_closes_upstream_and_releases_admission(self):
        body_started = asyncio.Event()
        body_closed = asyncio.Event()
        upstream_background = AsyncMock()
        cleanup = AsyncMock()

        async def body():
            try:
                body_started.set()
                await asyncio.Event().wait()
                yield b"unreachable"
            finally:
                body_closed.set()

        upstream = EnhancedStreamingResponse(
            body(),
            background=upstream_background,
        )
        with (
            patch(
                "comet.services.streaming.manager.admit_active_connection",
                new=AsyncMock(return_value="connection"),
            ),
            patch(
                "comet.services.streaming.manager.monitored_handle_stream_request",
                new=AsyncMock(return_value=upstream),
            ),
            patch(
                "comet.services.streaming.manager.on_stream_end",
                new=cleanup,
            ),
        ):
            response = await custom_handle_stream_request(
                "GET",
                "https://video.test",
                Mock(),
                "tt123",
                "127.0.0.1",
                "realdebrid",
            )

            async def receive():
                await body_started.wait()
                return {"type": "http.disconnect"}

            await response(
                {
                    "type": "http",
                    "method": "GET",
                    "asgi": {"spec_version": "2.3"},
                },
                receive,
                AsyncMock(),
            )

        self.assertTrue(body_closed.is_set())
        upstream_background.assert_awaited_once_with()
        cleanup.assert_awaited_once_with(
            "connection",
            "127.0.0.1",
            outcome="completed",
            error_code=None,
        )
