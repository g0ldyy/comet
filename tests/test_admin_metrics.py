import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import orjson

from comet.api.endpoints import admin
from comet.api.endpoints.admin import _decode_cached_metrics


class AdminMetricsTests(unittest.TestCase):
    def test_cached_metrics_require_current_object_schema(self):
        self.assertEqual(
            _decode_cached_metrics('{"torrents":{"total":1}}'),
            {"torrents": {"total": 1}},
        )
        self.assertIsNone(_decode_cached_metrics("not-json"))
        self.assertIsNone(_decode_cached_metrics("[]"))


class AdminMetricsRefreshTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        admin._metrics_refresh_task = None

    async def test_stale_snapshot_is_served_while_one_refresh_runs(self):
        release = asyncio.Event()
        refresh = AsyncMock(side_effect=release.wait)
        stale = {"payload_json": '{"torrents":{"total":1}}', "refreshed_at": 0}
        with (
            patch.object(admin.settings, "PUBLIC_METRICS_API", True),
            patch.object(
                admin.database, "fetch_one", new=AsyncMock(return_value=stale)
            ),
            patch.object(admin, "_refresh_metrics", new=refresh),
        ):
            first = await admin.admin_api_metrics()
            second = await admin.admin_api_metrics()
            await asyncio.sleep(0)

            self.assertEqual(orjson.loads(first.body), {"torrents": {"total": 1}})
            self.assertEqual(orjson.loads(second.body), {"torrents": {"total": 1}})
            self.assertEqual(refresh.call_count, 1)
            release.set()
            await admin._metrics_refresh_task

    async def test_cold_start_requests_share_one_refresh(self):
        refresh = AsyncMock(return_value={"torrents": {"total": 2}})
        with (
            patch.object(admin.settings, "PUBLIC_METRICS_API", True),
            patch.object(admin.database, "fetch_one", new=AsyncMock(return_value=None)),
            patch.object(admin, "_refresh_metrics", new=refresh),
        ):
            responses = await asyncio.gather(
                admin.admin_api_metrics(), admin.admin_api_metrics()
            )

        self.assertEqual(refresh.await_count, 1)
        for response in responses:
            self.assertEqual(orjson.loads(response.body), {"torrents": {"total": 2}})


class AdminBackgroundScraperTests(unittest.IsolatedAsyncioTestCase):
    async def test_drain_endpoint_reports_scheduled_stop(self):
        with (
            patch.object(admin, "require_admin_auth"),
            patch.object(admin.background_scraper, "is_running", True),
            patch.object(
                admin.background_scraper,
                "drain",
                new=AsyncMock(return_value=True),
            ) as drain,
        ):
            response = await admin.admin_background_scraper_drain()

        drain.assert_awaited_once_with()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(orjson.loads(response.body)["state"], "scheduled")

    async def test_drain_endpoint_stops_when_no_run_is_active(self):
        with (
            patch.object(admin, "require_admin_auth"),
            patch.object(admin.background_scraper, "is_running", True),
            patch.object(
                admin.background_scraper,
                "drain",
                new=AsyncMock(return_value=False),
            ) as drain,
        ):
            response = await admin.admin_background_scraper_drain()

        drain.assert_awaited_once_with()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(orjson.loads(response.body)["state"], "stopped")

    async def test_drain_endpoint_is_idempotent_when_already_stopped(self):
        with (
            patch.object(admin, "require_admin_auth"),
            patch.object(admin.background_scraper, "is_running", False),
            patch.object(
                admin.background_scraper,
                "drain",
                new=AsyncMock(),
            ) as drain,
        ):
            response = await admin.admin_background_scraper_drain()

        drain.assert_not_awaited()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(orjson.loads(response.body)["state"], "stopped")

    async def test_cancel_drain_endpoint_is_idempotent(self):
        with (
            patch.object(admin, "require_admin_auth"),
            patch.object(
                admin.background_scraper,
                "cancel_drain",
                return_value=False,
            ) as cancel_drain,
        ):
            response = await admin.admin_background_scraper_cancel_drain()

        cancel_drain.assert_called_once_with()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(orjson.loads(response.body)["state"], "unchanged")
