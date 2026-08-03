import asyncio
import json
import unittest
import warnings
from datetime import UTC, datetime
from decimal import Decimal
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock, patch

from databases import Database
from httpx import ASGITransport, AsyncClient

import comet.api.v1.cometnet as api_cometnet
import comet.api.v1.logs as api_logs
import comet.api.v1.metrics as api_metrics
import comet.api.v1.proxy as api_proxy
import comet.api.v1.router as api_v1
import comet.api.v1.scraping as api_scraping
import comet.api.v1.security as api_security
import comet.api.v1.system as api_system
import comet.api.v1.usenet as api_usenet
from comet.api import app as app_module
from comet.api.endpoints import base as base_endpoint
from comet.core.db_router import ReplicaAwareDatabase
from comet.core.event_store import EventFilters, EventStore, EventWrite
from comet.core.operator_store import OperatorSettingsStore
from comet.core.schema_migrations import run_schema_migrations
from comet.observability.database_metrics import (
    DatabaseMetricsSnapshot,
    DebridInventoryMetrics,
    DistributionMetric,
    ScraperInventoryMetrics,
    SearchInventoryMetrics,
    TorrentInventoryMetrics,
    TorrentInventorySummary,
    _collect_database_metrics,
)
from comet.observability.metric_snapshot import MetricSample
from comet.usenet.engine_stats import (
    ENGINE_STAT_BOOLEAN_FIELDS,
    ENGINE_STAT_INTEGER_FIELDS,
)
from comet.utils.signed_session import derive_session_secret
from comet.utils.update import UpdateStatus, VersionInfo
from scripts.generate_frontend_contracts import OUTPUT, generate


class ApiV1Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = TemporaryDirectory()
        self.database = ReplicaAwareDatabase(
            Database(f"sqlite+aiosqlite:///{self.temp_dir.name}/api-v1.db")
        )
        await self.database.connect()
        await run_schema_migrations(
            self.database,
            is_sqlite=True,
            is_postgres=False,
        )
        self.database_patch = patch.object(api_v1, "database", self.database)
        self.security_database_patch = patch.object(
            api_security,
            "database",
            self.database,
        )
        self.store_patch = patch.object(
            api_v1,
            "_store",
            OperatorSettingsStore(self.database),
        )
        self.event_store_patch = patch.object(
            api_logs,
            "_store",
            EventStore(self.database),
        )
        self.proxy_database_patch = patch.object(
            api_proxy,
            "database",
            self.database,
        )
        self.scraping_database_patch = patch.object(
            api_scraping,
            "database",
            self.database,
        )
        self.usenet_database_patch = patch.object(
            api_usenet,
            "database",
            self.database,
        )
        self.cometnet_events_patch = patch.object(
            api_cometnet,
            "_events",
            EventStore(self.database),
        )
        self.system_database_patch = patch.object(
            api_system,
            "database",
            self.database,
        )
        self.system_store_patch = patch.object(
            api_system,
            "_store",
            OperatorSettingsStore(self.database),
        )
        self.readiness_database_patch = patch.object(
            base_endpoint,
            "database",
            self.database,
        )
        self.database_patch.start()
        self.security_database_patch.start()
        self.store_patch.start()
        self.event_store_patch.start()
        self.proxy_database_patch.start()
        self.scraping_database_patch.start()
        self.usenet_database_patch.start()
        self.cometnet_events_patch.start()
        self.system_database_patch.start()
        self.system_store_patch.start()
        self.readiness_database_patch.start()
        self.client = AsyncClient(
            transport=ASGITransport(
                app=app_module.app,
                raise_app_exceptions=False,
            ),
            base_url="http://testserver",
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        self.store_patch.stop()
        self.event_store_patch.stop()
        self.proxy_database_patch.stop()
        self.scraping_database_patch.stop()
        self.usenet_database_patch.stop()
        self.cometnet_events_patch.stop()
        self.system_store_patch.stop()
        self.system_database_patch.stop()
        self.readiness_database_patch.stop()
        self.security_database_patch.stop()
        self.database_patch.stop()
        await self.database.disconnect()
        self.temp_dir.cleanup()

    async def _login(self):
        response = await self.client.post(
            "/api/v1/auth/login",
            headers={"Origin": "http://testserver"},
            json={"password": app_module.settings.ADMIN_DASHBOARD_PASSWORD},
        )
        self.assertEqual(response.status_code, 200, response.text)
        set_cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", set_cookie)
        self.assertIn("samesite=strict", set_cookie)
        self.assertIn("path=/", set_cookie)
        return response.json()["data"]["csrf_token"]

    async def test_errors_use_stable_envelope_without_html_or_secret_echo(self):
        response = await self.client.get("/api/v1/auth/session")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.json()["error"]["code"],
            "authentication_required",
        )
        self.assertEqual(
            response.json()["error"]["request_id"],
            response.headers["x-request-id"],
        )
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertNotIn("text/html", response.headers["content-type"])

        secret = "must-not-be-reflected"
        invalid = await self.client.post(
            "/api/v1/auth/login",
            headers={"Origin": "http://testserver"},
            json={"password": secret, "unexpected": secret},
        )
        self.assertEqual(invalid.status_code, 422)
        self.assertNotIn(secret, invalid.text)
        self.assertEqual(invalid.json()["error"]["code"], "validation_failed")
        self.assertEqual(
            invalid.json()["error"]["details"],
            [
                {
                    "location": ["unexpected"],
                    "type": "extra_forbidden",
                    "message": "Extra inputs are not permitted",
                }
            ],
        )

    async def test_checked_in_typescript_contract_matches_openapi(self):
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="Duplicate Operation ID.*",
                category=UserWarning,
            )
            generated = generate()
        self.assertEqual(OUTPUT.read_text(encoding="utf-8"), generated)

    async def test_login_accepts_a_single_character_password(self):
        password = "é"
        with patch.object(api_v1.settings, "ADMIN_DASHBOARD_PASSWORD", password):
            response = await self.client.post(
                "/api/v1/auth/login",
                headers={"Origin": "http://testserver"},
                json={"password": password},
            )

        self.assertEqual(response.status_code, 200, response.text)

    async def test_login_cookie_session_and_origin_csrf_policy(self):
        no_origin = await self.client.post(
            "/api/v1/auth/login",
            json={"password": app_module.settings.ADMIN_DASHBOARD_PASSWORD},
        )
        self.assertEqual(no_origin.status_code, 403)
        self.assertEqual(no_origin.json()["error"]["code"], "origin_mismatch")

        csrf = await self._login()
        cookie = self.client.cookies.get("admin_session")
        self.assertIsNotNone(cookie)
        session = await self.client.get("/api/v1/auth/session")
        self.assertEqual(session.status_code, 200)
        self.assertEqual(session.json()["data"]["csrf_token"], csrf)

        missing = await self.client.put(
            "/api/v1/admin/settings",
            headers={"Origin": "http://testserver"},
            json={"updates": {"HTTP_CLIENT_LIMIT": 75}},
        )
        self.assertEqual(missing.status_code, 403)
        self.assertEqual(missing.json()["error"]["code"], "csrf_failed")

        cross_origin = await self.client.put(
            "/api/v1/admin/settings",
            headers={
                "Origin": "https://attacker.invalid",
                "X-CSRF-Token": csrf,
            },
            json={"updates": {"HTTP_CLIENT_LIMIT": 75}},
        )
        self.assertEqual(cross_origin.status_code, 403)
        self.assertEqual(cross_origin.json()["error"]["code"], "origin_mismatch")

    async def test_password_protected_configure_session_has_own_csrf_scope(self):
        password = "configure-secret"
        configure_secret = derive_session_secret(password, "configure-page")
        with (
            patch.object(
                api_v1.settings,
                "CONFIGURE_PAGE_PASSWORD",
                password,
            ),
            patch.object(
                api_security,
                "_CONFIGURE_SESSION_SECRET",
                configure_secret,
            ),
        ):
            unauthenticated = await self.client.get("/api/v1/auth/configure/session")
            self.assertEqual(unauthenticated.status_code, 401)

            login = await self.client.post(
                "/api/v1/auth/configure/login",
                headers={"Origin": "http://testserver"},
                json={"password": password},
            )
            self.assertEqual(login.status_code, 200, login.text)
            configure_csrf = login.json()["data"]["csrf_token"]
            self.assertTrue(login.json()["data"]["protected"])
            self.assertNotEqual(
                configure_csrf,
                api_security.csrf_token(self.client.cookies.get("configure_session")),
            )

            session = await self.client.get("/api/v1/auth/configure/session")
            self.assertEqual(session.status_code, 200)
            self.assertEqual(session.json()["data"]["csrf_token"], configure_csrf)

            logout = await self.client.post(
                "/api/v1/auth/configure/logout",
                headers={
                    "Origin": "http://testserver",
                    "X-CSRF-Token": configure_csrf,
                },
            )
            self.assertEqual(logout.status_code, 200)

    async def test_configurator_bootstrap_and_backend_validation(self):
        bootstrap = await self.client.get("/api/v1/configure/bootstrap")
        self.assertEqual(bootstrap.status_code, 200, bootstrap.text)
        data = bootstrap.json()["data"]
        self.assertEqual(data["default_configuration"]["schemaVersion"], 1)
        self.assertNotIn(
            "remove_ranks_under",
            data["default_configuration"]["options"],
        )
        self.assertNotIn("rtnSettings", data["default_configuration"])
        self.assertNotIn("rtnRanking", data["default_configuration"])
        self.assertEqual(
            data["capabilities"]["torrent_streams"],
            not app_module.settings.DISABLE_TORRENT_STREAMS,
        )
        self.assertEqual(
            set(data["debrid_services"]),
            {
                "realdebrid",
                "alldebrid",
                "premiumize",
                "torbox",
                "debrider",
                "easydebrid",
                "debridlink",
                "offcloud",
                "pikpak",
            },
        )

        validated = await self.client.post(
            "/api/v1/configure/validate",
            headers={"Origin": "http://testserver"},
            json={
                "configuration": {
                    "schemaVersion": 1,
                    "debridServices": [{"service": "realdebrid", "apiKey": "secret"}],
                    "enableTorrent": True,
                }
            },
        )
        self.assertEqual(validated.status_code, 200, validated.text)
        self.assertEqual(
            validated.json()["data"]["debridServices"],
            [{"service": "realdebrid", "apiKey": "secret"}],
        )

        missing_key = await self.client.post(
            "/api/v1/configure/validate",
            headers={"Origin": "http://testserver"},
            json={
                "configuration": {
                    "schemaVersion": 1,
                    "debridServices": [{"service": "realdebrid", "apiKey": ""}],
                    "enableTorrent": True,
                }
            },
        )
        self.assertEqual(missing_key.status_code, 200)
        self.assertEqual(
            missing_key.json()["data"]["debridServices"],
            [{"service": "realdebrid", "apiKey": ""}],
        )

        disabled = await self.client.post(
            "/api/v1/configure/validate",
            headers={"Origin": "http://testserver"},
            json={
                "configuration": {
                    "schemaVersion": 2,
                    "enabledTransports": [],
                }
            },
        )
        self.assertEqual(disabled.status_code, 200, disabled.text)
        self.assertEqual(disabled.json()["data"]["enabledTransports"], [])

        duplicate_p2p = await self.client.post(
            "/api/v1/configure/validate",
            headers={"Origin": "http://testserver"},
            json={
                "configuration": {
                    "schemaVersion": 2,
                    "enabledTransports": ["bittorrent"],
                    "playbackProviders": [
                        {
                            "configurationId": "11111111-1111-4111-8111-111111111111",
                            "kind": "direct_torrent",
                            "options": {},
                        },
                        {
                            "configurationId": "22222222-2222-4222-8222-222222222222",
                            "kind": "direct_torrent",
                            "options": {},
                        },
                    ],
                }
            },
        )
        self.assertEqual(duplicate_p2p.status_code, 422)
        self.assertEqual(duplicate_p2p.json()["error"]["code"], "validation_failed")

        cross_site_guard = await self.client.post(
            "/api/v1/configure/validate",
            json={"configuration": {"schemaVersion": 1}},
        )
        self.assertEqual(cross_site_guard.status_code, 403)
        self.assertEqual(cross_site_guard.json()["error"]["code"], "origin_mismatch")

        oversized = await self.client.post(
            "/api/v1/configure/validate",
            headers={"Origin": "http://testserver"},
            json={
                "configuration": {
                    "schemaVersion": 1,
                    "debridApiKey": "x" * (24 * 1024),
                }
            },
        )
        self.assertEqual(oversized.status_code, 422)
        self.assertEqual(oversized.json()["error"]["code"], "validation_failed")

    async def test_event_history_filters_and_safe_log_export(self):
        await self._login()
        request_id = "b" * 32
        await api_logs._store.append(
            [
                EventWrite(
                    created_at=100,
                    instance_id="a" * 32,
                    process_id=123,
                    role="web_worker",
                    level="INFO",
                    category="SCRAPER",
                    event="search.accepted",
                    message="Search accepted",
                    request_id=request_id,
                    run_id=None,
                    connection_id=None,
                    media_type="movie",
                    provider_name="torrentio",
                    outcome="ok",
                    error_code=None,
                    details={"result_count": 4},
                ),
                EventWrite(
                    created_at=101,
                    instance_id="a" * 32,
                    process_id=123,
                    role="web_worker",
                    level="ERROR",
                    category="DATABASE",
                    event="database.query.failed",
                    message="Database query failed",
                    request_id=None,
                    run_id=None,
                    connection_id=None,
                    media_type=None,
                    provider_name=None,
                    outcome="failed",
                    error_code="query_failed",
                    details={"duration_ms": 12.5},
                ),
            ],
            dropped=3,
        )

        history = await self.client.get(
            "/api/v1/admin/logs",
            params={"category": "SCRAPER", "request_id": request_id},
        )
        self.assertEqual(history.status_code, 200, history.text)
        data = history.json()["data"]
        self.assertEqual([item["event"] for item in data["items"]], ["search.accepted"])
        self.assertEqual(data["items"][0]["details"], {"result_count": 4})
        self.assertEqual(data["dropped_events"], 3)

        exported = await self.client.get(
            "/api/v1/admin/logs/export",
            params={"format": "text", "level": "ERROR"},
        )
        self.assertEqual(exported.status_code, 200, exported.text)
        self.assertIn("database.query.failed", exported.text)
        self.assertNotIn("search.accepted", exported.text)
        self.assertRegex(
            exported.headers["content-disposition"],
            r'^attachment; filename="comet-logs-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z\.txt"$',
        )
        exported_jsonl = await self.client.get(
            "/api/v1/admin/logs/export",
            params={"format": "jsonl"},
        )
        self.assertRegex(
            exported_jsonl.headers["content-disposition"],
            r'^attachment; filename="comet-logs-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z\.jsonl"$',
        )

    async def test_event_stream_resumes_after_last_event_id(self):
        await api_logs._store.append(
            [
                EventWrite(
                    created_at=100 + offset,
                    instance_id="a" * 32,
                    process_id=123,
                    role="web_worker",
                    level="INFO",
                    category="SCRAPER",
                    event=f"search.stage.{offset}",
                    message=f"Search stage {offset}",
                    request_id="b" * 32,
                    run_id=None,
                    connection_id=None,
                    media_type="movie",
                    provider_name=None,
                    outcome="ok",
                    error_code=None,
                    details={},
                )
                for offset in range(2)
            ]
        )
        request = Mock()
        request.is_disconnected = AsyncMock(return_value=False)
        response = await api_logs.stream_events(
            request=request,
            session="session",
            filters=EventFilters(),
            cursor=0,
            last_event_id=1,
        )

        chunk = await anext(response.body_iterator)
        await response.body_iterator.aclose()

        self.assertIn(b"id: 2\n", chunk)
        self.assertNotIn(b"id: 1\n", chunk)

    async def test_event_stream_expires_revoked_session(self):
        request = Mock()
        request.is_disconnected = AsyncMock(return_value=False)
        with (
            patch.object(api_logs, "_SESSION_CHECK_SECONDS", 0),
            patch.object(
                api_logs,
                "admin_session_active",
                AsyncMock(return_value=False),
            ),
        ):
            response = await api_logs.stream_events(
                request=request,
                session="revoked",
                filters=EventFilters(),
                cursor=None,
                last_event_id=None,
            )
            chunk = await anext(response.body_iterator)
            await response.body_iterator.aclose()

        self.assertEqual(chunk, b"event: session_expired\ndata: {}\n\n")

    async def test_current_metrics_are_private_and_history_is_explicit(self):
        unauthenticated = await self.client.get("/api/v1/admin/metrics/current")
        self.assertEqual(unauthenticated.status_code, 401)

        await self._login()
        with patch.object(
            api_metrics,
            "current_metric_samples",
            AsyncMock(
                return_value=(
                    MetricSample(
                        name="comet_http_requests_total",
                        labels={"status": "200"},
                        value=12,
                    ),
                )
            ),
        ):
            current = await self.client.get("/api/v1/admin/metrics/current")
        self.assertEqual(current.status_code, 200, current.text)
        data = current.json()["data"]
        self.assertEqual(data["samples"][0]["value"], 12)
        self.assertFalse(data["history_available"])
        self.assertEqual(
            data["history_ranges"], ["15m", "1h", "6h", "24h", "7d", "30d"]
        )

        unavailable = await self.client.get("/api/v1/admin/metrics/range/http_requests")
        self.assertEqual(unavailable.status_code, 404)
        self.assertEqual(
            unavailable.json()["error"]["code"],
            "metrics_history_unavailable",
        )

    async def test_database_metrics_are_private_and_keep_numeric_inventory(self):
        unauthenticated = await self.client.get("/api/v1/admin/metrics/database")
        self.assertEqual(unauthenticated.status_code, 401)
        await self._login()
        snapshot = DatabaseMetricsSnapshot(
            collected_at=100,
            torrents=TorrentInventoryMetrics(
                total=42,
                size_distribution=(DistributionMetric(label="1-5GB", count=30),),
                media_distribution=(DistributionMetric(label="Movies", count=24),),
                summary=TorrentInventorySummary(
                    unique_media=18,
                    seen_24h=7,
                    seen_7d=31,
                    average_size=1_500_000_000.5,
                    maximum_size=8_000_000_000,
                ),
            ),
            searches=SearchInventoryMetrics(
                total_unique=50,
                last_24h=4,
                last_7d=20,
                last_30d=45,
            ),
            scrapers=ScraperInventoryMetrics(active_locks=2),
            debrid_cache=DebridInventoryMetrics(total=9, by_service=()),
        )
        with patch.object(
            api_metrics,
            "current_database_metrics",
            AsyncMock(return_value=snapshot),
        ):
            response = await self.client.get("/api/v1/admin/metrics/database")

        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()["data"]
        self.assertEqual(data["torrents"]["total"], 42)
        self.assertEqual(data["torrents"]["summary"]["unique_media"], 18)
        self.assertEqual(
            data["torrents"]["summary"]["average_size"],
            1_500_000_000.5,
        )
        self.assertEqual(data["searches"]["last_24h"], 4)

    async def test_cometnet_snapshot_is_stable_and_omits_backend_secrets(self):
        await self._login()
        backend = Mock()
        backend.get_stats = AsyncMock(
            return_value={
                "enabled": True,
                "node_id": "node-1",
                "public_key": "must-not-leak",
                "uptime_seconds": 120,
                "contribution_mode": "full",
                "connection_stats": {
                    "connected_peers": 1,
                    "inbound": 0,
                    "outbound": 1,
                    "avg_latency_ms": 12.5,
                    "bytes_sent": 100,
                    "bytes_received": 200,
                    "messages_sent": 4,
                    "messages_received": 5,
                },
                "gossip_stats": {
                    "torrents_propagated": 7,
                    "torrents_received": 9,
                    "invalid_messages": 1,
                },
            }
        )
        backend.get_peers = AsyncMock(
            return_value={
                "count": 1,
                "peers": [
                    {
                        "node_id": "peer-1",
                        "address": "wss://must-not-leak.invalid",
                        "connected_at": 10,
                        "last_activity": 20,
                        "is_outbound": True,
                        "latency_ms": 12.5,
                        "bytes_sent": 50,
                        "bytes_received": 75,
                    }
                ],
            }
        )
        backend.get_pools = AsyncMock(
            return_value={
                "memberships": ["trusted"],
                "subscriptions": ["trusted"],
                "pools": {
                    "trusted": {
                        "display_name": "Trusted",
                        "description": "Main pool",
                        "members": [{"signature": "must-not-leak"}],
                        "signatures": {"key": "must-not-leak"},
                        "version": 2,
                        "updated_at": 30,
                    }
                },
            }
        )
        with patch.object(api_cometnet, "get_active_backend", return_value=backend):
            response = await self.client.get("/api/v1/admin/cometnet/snapshot")

        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()["data"]
        self.assertEqual(data["node"]["torrents_sent"], 7)
        self.assertEqual(data["node"]["bytes_received"], 200)
        self.assertEqual(data["pools"][0]["member_count"], 1)
        self.assertNotIn("public_key", data["node"])
        self.assertNotIn("address", data["peers"][0])
        self.assertNotIn("must-not-leak", response.text)

    async def test_proxy_snapshot_history_and_owned_cancellation(self):
        csrf = await self._login()
        connection_id = "8af5d66a-1dd2-4c35-8f5e-8b5138f33c40"
        await self.database.execute(
            """
            INSERT INTO bandwidth_stats (id, total_bytes, updated_at)
            VALUES (1, 9000, 100)
            """
        )
        await self.database.execute(
            """
            INSERT INTO active_connections (
                id, ip, content, service, instance_id, process_id,
                started_at, updated_at, bytes_transferred,
                current_speed, peak_speed
            ) VALUES (
                :id, '192.0.2.8', 'Example', 'realdebrid',
                :instance_id, 321, :started_at, :started_at,
                2000, 500, 900
            )
            """,
            {
                "id": connection_id,
                "instance_id": "a" * 32,
                "started_at": 100.0,
            },
        )
        now = api_proxy.time.time()
        await self.database.execute(
            """
            INSERT INTO proxy_connection_history (
                id, ip, content, service, instance_id, process_id,
                started_at, finished_at, duration, bytes_transferred,
                average_speed, peak_speed, outcome, error_code
            ) VALUES (
                :id, '192.0.2.9', 'Previous', 'alldebrid',
                :instance_id, 322, :started_at, :finished_at,
                20, 4000, 200, 400, 'completed', NULL
            )
            """,
            {
                "id": "history",
                "instance_id": "b" * 32,
                "started_at": now - 40,
                "finished_at": now - 20,
            },
        )

        snapshot = await self.client.get("/api/v1/admin/proxy/snapshot")
        self.assertEqual(snapshot.status_code, 200, snapshot.text)
        data = snapshot.json()["data"]
        self.assertEqual(data["enabled"], api_proxy.settings.PROXY_DEBRID_STREAM)
        self.assertEqual(data["summary"]["active_connections"], 1)
        self.assertEqual(data["summary"]["all_time_bytes"], 9000)
        self.assertEqual(data["active"][0]["ip"], "192.0.2.8")
        self.assertEqual(data["active"][0]["service"], "realdebrid")

        activity = await self.client.get(
            "/api/v1/admin/proxy/activity",
            params={"range": "auto"},
        )
        self.assertEqual(activity.status_code, 200, activity.text)
        activity_data = activity.json()["data"]
        self.assertEqual(activity_data["selection"], "auto")
        self.assertLessEqual(len(activity_data["buckets"]), 85)
        self.assertEqual(
            sum(bucket["bytes_transferred"] for bucket in activity_data["buckets"]),
            4000,
        )
        self.assertEqual(
            sum(bucket["active"] for bucket in activity_data["buckets"]), 1
        )

        async def acknowledge():
            while True:
                row = await self.database.fetch_one(
                    "SELECT cancel_requested FROM active_connections WHERE id = :id",
                    {"id": connection_id},
                )
                if row["cancel_requested"]:
                    break
                await asyncio.sleep(0.01)
            await self.database.execute(
                """
                INSERT INTO proxy_connection_history (
                    id, ip, content, service, instance_id, process_id,
                    started_at, finished_at, duration, bytes_transferred,
                    average_speed, peak_speed, outcome, error_code
                ) VALUES (
                    :id, '192.0.2.8', 'Example', 'realdebrid',
                    :instance_id, 321, 100, :finished_at, 10,
                    2000, 200, 900, 'cancelled', NULL
                )
                """,
                {
                    "id": connection_id,
                    "instance_id": "a" * 32,
                    "finished_at": api_proxy.time.time(),
                },
            )
            await self.database.execute(
                "DELETE FROM active_connections WHERE id = :id",
                {"id": connection_id},
            )

        acknowledgement = asyncio.create_task(acknowledge())
        cancelled = await self.client.post(
            f"/api/v1/admin/proxy/connections/{connection_id}/cancel",
            headers={
                "Origin": "http://testserver",
                "X-CSRF-Token": csrf,
            },
        )
        await acknowledgement
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["data"]["outcome"], "cancelled")

    async def test_scraping_snapshot_queue_and_targeted_actions(self):
        csrf = await self._login()
        now = api_scraping.time.time()
        await self.database.execute(
            """
            INSERT INTO background_scraper_items (
                media_id, media_type, title, year, priority_score,
                status, consecutive_failures, total_torrents_found,
                created_at, updated_at
            ) VALUES (
                'tt123', 'series', 'Example series', 2024, 12,
                'failed', 2, 5, :now, :now
            )
            """,
            {"now": now - 100},
        )
        await self.database.execute(
            """
            INSERT INTO background_scraper_episodes (
                episode_media_id, series_id, season, episode,
                status, consecutive_failures, total_torrents_found,
                created_at, updated_at
            ) VALUES (
                'tt123:1:1', 'tt123', 1, 1,
                'dead', 4, 1, :now, :now
            )
            """,
            {"now": now - 90},
        )
        await self.database.execute(
            """
            INSERT INTO background_scraper_runs (
                run_id, started_at, finished_at, status,
                processed_count, success_count, failed_count,
                torrents_found_count, duration_ms, worker_count
            ) VALUES (
                :run_id, :started_at, :finished_at, 'completed',
                4, 3, 1, 8, 1200, 2
            )
            """,
            {
                "run_id": "11111111-1111-4111-8111-111111111111",
                "started_at": now - 60,
                "finished_at": now - 58,
            },
        )
        await self.database.execute(
            """
            INSERT INTO background_scraper_runtimes (
                instance_id, process_id, state, draining, run_id,
                started_at, processed, success, failed, torrents_found,
                discovered_items, errors, last_heartbeat
            ) VALUES (
                :instance_id, 321, 'running', 0, NULL,
                :started_at, 2, 1, 1, 3, 4, 1, :heartbeat
            )
            """,
            {
                "instance_id": "a" * 32,
                "started_at": now - 30,
                "heartbeat": now,
            },
        )

        snapshot = await self.client.get("/api/v1/admin/scraping/snapshot")
        self.assertEqual(snapshot.status_code, 200, snapshot.text)
        data = snapshot.json()["data"]
        self.assertEqual(data["queue"]["items"], 1)
        self.assertEqual(data["queue"]["episodes"], 1)
        self.assertEqual(data["queue"]["dead"], 1)
        self.assertEqual(data["runtimes"][0]["processed"], 2)

        queue = await self.client.get(
            "/api/v1/admin/scraping/queue/item",
            params={"search": "example"},
        )
        self.assertEqual(queue.status_code, 200, queue.text)
        self.assertEqual(queue.json()["data"]["items"][0]["id"], "tt123")

        retried = await self.client.post(
            "/api/v1/admin/scraping/queue/item/tt123/retry",
            headers={
                "Origin": "http://testserver",
                "X-CSRF-Token": csrf,
            },
        )
        self.assertEqual(retried.status_code, 200, retried.text)
        item = await self.database.fetch_one(
            "SELECT status, consecutive_failures FROM background_scraper_items WHERE media_id = 'tt123'"
        )
        self.assertEqual(
            dict(item), {"status": "discovered", "consecutive_failures": 0}
        )

        with patch.object(
            api_scraping,
            "dispatch_scraper_command",
            AsyncMock(return_value=[{"outcome": "succeeded"}]),
        ):
            paused = await self.client.post(
                "/api/v1/admin/scraping/control/pause",
                headers={
                    "Origin": "http://testserver",
                    "X-CSRF-Token": csrf,
                },
            )
        self.assertEqual(paused.status_code, 200, paused.text)
        self.assertEqual(paused.json()["data"]["action"], "pause")

    async def test_usenet_snapshot_inventory_controls_and_owned_cancellation(self):
        csrf = await self._login()
        now = api_usenet.time.time()
        stats = {
            **{field: 0 for field in ENGINE_STAT_INTEGER_FIELDS},
            **{field: False for field in ENGINE_STAT_BOOLEAN_FIELDS},
            "sessions": 2,
            "nntp_connections_open": 3,
        }
        await self.database.execute(
            """
            INSERT INTO usenet_engine_runtimes (
                instance_id, process_id, healthy, mode, stats_json, collected_at
            ) VALUES (:instance_id, 321, 1, 'native', :stats_json, :collected_at)
            """,
            {
                "instance_id": "a" * 32,
                "stats_json": json.dumps(stats),
                "collected_at": now,
            },
        )
        operation_id = "b" * 32
        await self.database.execute(
            """
            INSERT INTO usenet_active_operations (
                id, instance_id, process_id, client_ip, content_id, title,
                member_path, source_kind, started_at, updated_at,
                total_bytes, bytes_transferred, cancel_requested
            ) VALUES (
                :id, :instance_id, 321, '192.0.2.10', 'tt123', 'Example',
                'Example.mkv', 'session', :started_at, :updated_at,
                1000, 400, 0
            )
            """,
            {
                "id": operation_id,
                "instance_id": "a" * 32,
                "started_at": now - 10,
                "updated_at": now,
            },
        )
        await self.database.execute(
            """
            INSERT INTO nzb_artifacts (
                artifact_sha256, byte_size, storage_kind, relative_path,
                publication_state, refcount, created_at, last_used_at
            ) VALUES (
                :artifact, 2000, 'nzb', :relative_path,
                'published', 0, :created_at, :last_used_at
            )
            """,
            {
                "artifact": "c" * 64,
                "relative_path": f"nzb/{'c' * 64}.nzb",
                "created_at": now - 500,
                "last_used_at": now - 400,
            },
        )

        snapshot = await self.client.get("/api/v1/admin/usenet/snapshot")
        self.assertEqual(snapshot.status_code, 200, snapshot.text)
        data = snapshot.json()["data"]
        self.assertEqual(data["runtimes"][0]["stats"]["sessions"], 2)
        self.assertEqual(data["active"][0]["bytes_transferred"], 400)
        self.assertEqual(data["inventory"]["artifacts"], 1)
        self.assertEqual(data["inventory"]["eligible_for_prune"], 1)

        activity = await self.client.get(
            "/api/v1/admin/usenet/activity",
            params={"range": "auto"},
        )
        self.assertEqual(activity.status_code, 200, activity.text)
        activity_data = activity.json()["data"]
        self.assertLessEqual(len(activity_data["buckets"]), 5)
        self.assertEqual(
            sum(bucket["bytes_transferred"] for bucket in activity_data["buckets"]),
            400,
        )
        self.assertEqual(
            sum(bucket["active"] for bucket in activity_data["buckets"]), 1
        )

        artifacts = await self.client.get("/api/v1/admin/usenet/artifacts")
        self.assertEqual(artifacts.status_code, 200, artifacts.text)
        artifact = artifacts.json()["data"]["items"][0]
        self.assertEqual(artifact["artifact_sha256"], "c" * 64)
        self.assertTrue(artifact["eligible_for_prune"])

        async def acknowledge_cancellation():
            while True:
                row = await self.database.fetch_one(
                    """
                    SELECT cancel_requested
                    FROM usenet_active_operations
                    WHERE id = :id
                    """,
                    {"id": operation_id},
                )
                if row["cancel_requested"]:
                    break
                await asyncio.sleep(0.01)
            await self.database.execute(
                """
                INSERT INTO usenet_operation_history (
                    id, instance_id, process_id, client_ip, content_id, title,
                    member_path, source_kind, started_at, finished_at, duration,
                    total_bytes, bytes_transferred, outcome, error_code
                ) VALUES (
                    :id, :instance_id, 321, '192.0.2.10', 'tt123', 'Example',
                    'Example.mkv', 'session', :started_at, :finished_at, 10,
                    1000, 400, 'cancelled', NULL
                )
                """,
                {
                    "id": operation_id,
                    "instance_id": "a" * 32,
                    "started_at": now - 10,
                    "finished_at": api_usenet.time.time(),
                },
            )
            await self.database.execute(
                "DELETE FROM usenet_active_operations WHERE id = :id",
                {"id": operation_id},
            )

        acknowledgement = asyncio.create_task(acknowledge_cancellation())
        cancelled = await self.client.post(
            f"/api/v1/admin/usenet/operations/{operation_id}/cancel",
            headers={
                "Origin": "http://testserver",
                "X-CSRF-Token": csrf,
            },
        )
        await acknowledgement
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["data"]["outcome"], "cancelled")

        with patch.object(
            api_usenet,
            "dispatch_usenet_command",
            AsyncMock(return_value={"outcome": "succeeded", "error_code": None}),
        ):
            drained = await self.client.post(
                f"/api/v1/admin/usenet/runtimes/{'a' * 32}/321/drain",
                headers={
                    "Origin": "http://testserver",
                    "X-CSRF-Token": csrf,
                },
            )
        self.assertEqual(drained.status_code, 200, drained.text)

        with patch(
            "comet.api.v1.usenet.SharedArtifactGarbageCollector.prune",
            AsyncMock(return_value=True),
        ):
            pruned = await self.client.post(
                f"/api/v1/admin/usenet/artifacts/{'c' * 64}/prune",
                headers={
                    "Origin": "http://testserver",
                    "X-CSRF-Token": csrf,
                },
            )
        self.assertEqual(pruned.status_code, 200, pruned.text)

    async def test_historical_metrics_use_closed_server_owned_query(self):
        class Response:
            status = 200

            def __init__(self):
                self.content = self
                self._body = (
                    b'{"status":"success","data":{"resultType":"matrix",'
                    b'"result":[{"metric":{"outcome":"error"},'
                    b'"values":[[100.0,"1.5"],[101.0,"NaN"]]}]}}'
                )

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def read(self, _size):
                body, self._body = self._body, b""
                return body

        await self._login()
        session = Mock()
        session.get.return_value = Response()
        with (
            patch.object(
                api_metrics.settings,
                "PROMETHEUS_QUERY_URL",
                "http://prometheus.internal:9090",
            ),
            patch.object(
                api_metrics.http_client_manager,
                "get_session",
                AsyncMock(return_value=session),
            ),
        ):
            response = await self.client.get(
                "/api/v1/admin/metrics/range/database_errors",
                params={"range": "15m"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()["data"]
        self.assertEqual(data["step"], 5)
        self.assertEqual(data["series"][0]["points"][0]["value"], 1.5)
        self.assertIsNone(data["series"][0]["points"][1]["value"])
        request = session.get.call_args
        self.assertEqual(
            request.args[0],
            "http://prometheus.internal:9090/api/v1/query_range",
        )
        self.assertEqual(
            request.kwargs["params"]["query"],
            'sum(rate(comet_database_operations_total{outcome="error"}[5m]))',
        )

    async def test_settings_revision_snapshot_returns_exact_values(self):
        csrf = await self._login()
        headers = {
            "Origin": "http://testserver",
            "X-CSRF-Token": csrf,
        }
        secret = "dashboard-secret-not-for-audit"
        saved = await self.client.put(
            "/api/v1/admin/settings",
            headers=headers,
            json={
                "updates": {
                    "ADMIN_DASHBOARD_PASSWORD": secret,
                    "HTTP_CLIENT_LIMIT": 75,
                }
            },
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["data"]["revision"], 1)
        self.assertFalse(saved.json()["data"]["restart_required"])
        self.assertEqual(
            saved.json()["data"]["live_applied_keys"],
            [],
        )
        self.assertEqual(
            saved.json()["data"]["component_reloaded_keys"],
            ["ADMIN_DASHBOARD_PASSWORD", "HTTP_CLIENT_LIMIT"],
        )
        self.assertEqual(saved.json()["data"]["restart_required_keys"], [])

        csrf = await self._login()
        invalid_secret = await self.client.put(
            "/api/v1/admin/settings",
            headers={
                "Origin": "http://testserver",
                "X-CSRF-Token": csrf,
            },
            json={"updates": {"USENET_NATIVE_ACCESS_TOKEN": "bad token"}},
        )
        self.assertEqual(invalid_secret.status_code, 422)
        self.assertEqual(invalid_secret.json()["error"]["code"], "settings_invalid")
        self.assertEqual(
            invalid_secret.json()["error"]["details"],
            [
                {
                    "location": ["USENET_NATIVE_ACCESS_TOKEN"],
                    "type": "value_error",
                    "message": (
                        "USENET_NATIVE_ACCESS_TOKEN must be a non-empty opaque value "
                        "of at most 256 bytes"
                    ),
                }
            ],
        )
        self.assertNotIn("bad token", invalid_secret.text)

        await self._login()
        snapshot = await self.client.get("/api/v1/admin/settings")
        self.assertEqual(snapshot.status_code, 200)
        by_key = {
            item["catalog"]["key"]: item for item in snapshot.json()["data"]["settings"]
        }
        self.assertEqual(by_key["ADMIN_DASHBOARD_PASSWORD"]["value"], secret)
        self.assertEqual(
            by_key["ADMIN_DASHBOARD_PASSWORD"]["source"],
            "dashboard",
        )
        self.assertIsNone(by_key["DATABASE_URL"]["catalog"]["default"])
        self.assertEqual(
            by_key["DATABASE_PATH"]["catalog"]["default"],
            "data/comet.db",
        )
        self.assertEqual(
            by_key["DATABASE_PATH"]["value"],
            app_module.settings.DATABASE_PATH,
        )
        self.assertEqual(by_key["HTTP_CLIENT_LIMIT"]["value"], 75)

        audit = await self.client.get(
            "/api/v1/admin/settings/audit",
            params={"limit": 10},
        )
        self.assertEqual(audit.status_code, 200)
        setting_actions = {
            item["key"]: item["action"]
            for item in audit.json()["data"]["items"]
            if item["key"] in {"ADMIN_DASHBOARD_PASSWORD", "HTTP_CLIENT_LIMIT"}
        }
        self.assertEqual(
            setting_actions,
            {
                "ADMIN_DASHBOARD_PASSWORD": "set",
                "HTTP_CLIENT_LIMIT": "set",
            },
        )
        self.assertNotIn(secret, audit.text)
        first_page = await self.client.get(
            "/api/v1/admin/settings/audit",
            params={"limit": 1},
        )
        cursor = first_page.json()["data"]["next_cursor"]
        self.assertIsNotNone(cursor)
        next_page = await self.client.get(
            "/api/v1/admin/settings/audit",
            params={"limit": 1, "cursor": cursor},
        )
        self.assertEqual(next_page.status_code, 200)
        self.assertNotEqual(
            first_page.json()["data"]["items"][0]["id"],
            next_page.json()["data"]["items"][0]["id"],
        )

    async def test_system_snapshot_and_logout_are_private_and_enveloped(self):
        csrf = await self._login()
        session_cookie = self.client.cookies.get("admin_session")
        snapshot = await self.client.get("/api/v1/admin/system/snapshot")
        self.assertEqual(snapshot.status_code, 200, snapshot.text)
        self.assertEqual(
            snapshot.json()["data"]["stored_revision"],
            0,
        )
        self.assertEqual(snapshot.headers["cache-control"], "private, no-store")

        logout = await self.client.post(
            "/api/v1/auth/logout",
            headers={
                "Origin": "http://testserver",
                "X-CSRF-Token": csrf,
            },
        )
        self.assertEqual(logout.status_code, 200)
        self.assertFalse(logout.json()["data"]["authenticated"])
        session = await self.client.get("/api/v1/auth/session")
        self.assertEqual(session.status_code, 401)
        replay = await self.client.get(
            "/api/v1/auth/session",
            headers={"Cookie": f"admin_session={session_cookie}"},
        )
        self.assertEqual(replay.status_code, 401)
        actions = await self.database.fetch_all(
            "SELECT action FROM operator_settings_audit"
        )
        self.assertEqual(
            [row["action"] for row in actions],
            ["session_invalidate"],
        )

    async def test_system_details_update_and_retention_are_safe(self):
        csrf = await self._login()
        headers = {
            "Origin": "http://testserver",
            "X-CSRF-Token": csrf,
        }

        details = await self.client.get("/api/v1/admin/system/details")
        self.assertEqual(details.status_code, 200, details.text)
        self.assertEqual(details.json()["data"]["database"]["backend"], "sqlite")
        self.assertTrue(details.json()["data"]["database"]["schema_current"])

        update_status = UpdateStatus(
            has_update=True,
            latest_commit_hash="abcdef0",
            latest_url="https://github.com/g0ldyy/comet/commit/" + ("a" * 40),
            checked_at=datetime.now(UTC),
        )
        with (
            patch.object(
                api_system.UpdateManager,
                "check_for_updates",
                new=AsyncMock(return_value=update_status),
            ),
            patch.object(
                api_system.UpdateManager,
                "get_version_info",
                return_value=VersionInfo(is_docker=True),
            ),
        ):
            update = await self.client.post(
                "/api/v1/admin/system/update-check",
                headers=headers,
            )
        self.assertEqual(update.status_code, 200, update.text)
        self.assertTrue(update.json()["data"]["has_update"])
        self.assertEqual(
            update.json()["data"]["install_method"],
            "redeploy_container",
        )

        with patch.object(
            api_system,
            "run_retention_cleanup",
            new=AsyncMock(return_value=True),
        ) as cleanup:
            retention = await self.client.post(
                "/api/v1/admin/system/maintenance/retention",
                headers=headers,
            )
        self.assertEqual(retention.status_code, 200, retention.text)
        cleanup.assert_awaited_once_with()

        instance_id = "a" * 32
        await self.database.execute(
            """
            INSERT INTO runtime_instances (
                instance_id, hostname, started_at, last_heartbeat,
                branch, applied_revision, readiness_json, restart_capable
            ) VALUES (
                :instance_id, 'replica-a', 1, 1,
                'development', 0, '{"state":"ready","components":{}}', 1
            )
            """,
            {"instance_id": instance_id},
        )
        with patch.object(
            api_system,
            "dispatch_runtime_restart",
            new=AsyncMock(
                return_value={
                    "outcome": "succeeded",
                    "error_code": None,
                }
            ),
        ) as dispatch:
            restarted = await self.client.post(
                f"/api/v1/admin/system/runtimes/{instance_id}/restart",
                headers=headers,
            )
        self.assertEqual(restarted.status_code, 200, restarted.text)
        dispatch.assert_awaited_once_with(instance_id)


class PostgresAggregateTests(unittest.IsolatedAsyncioTestCase):
    async def test_activity_queries_floor_bucket_indexes(self):
        request = Mock(headers={})
        proxy_database = Mock(
            fetch_val=AsyncMock(return_value=None),
            fetch_all=AsyncMock(side_effect=[[], [], []]),
        )
        with patch.object(api_proxy, "database", proxy_database):
            await api_proxy.proxy_activity(request, "session")

        usenet_database = Mock(
            fetch_val=AsyncMock(return_value=None),
            fetch_all=AsyncMock(return_value=[]),
            fetch_one=AsyncMock(return_value={"active": 0, "bytes_transferred": 0}),
        )
        with patch.object(api_usenet, "database", usenet_database):
            await api_usenet.usenet_activity(request, "session")

        queries = [
            call.args[0]
            for call in (
                *proxy_database.fetch_all.await_args_list,
                *usenet_database.fetch_all.await_args_list,
            )
            if "bucket_index" in call.args[0]
        ]
        self.assertEqual(len(queries), 3)
        self.assertTrue(all("CAST(FLOOR(" in query for query in queries))

    async def test_proxy_snapshot_converts_bigint_aggregates(self):
        database = Mock(
            fetch_all=AsyncMock(return_value=[]),
            fetch_one=AsyncMock(
                side_effect=[
                    {
                        "active_connections": 0,
                        "current_speed": 0.0,
                        "session_bytes": Decimal(0),
                        "all_time_bytes": 0,
                    },
                    {
                        "completed": 0,
                        "failed": 0,
                        "bytes_transferred": Decimal(0),
                        "average_duration": 0.0,
                    },
                ]
            ),
        )
        with patch.object(api_proxy, "database", database):
            response = await api_proxy.proxy_snapshot(Mock(headers={}), "session")

        summary = json.loads(response.body)["data"]["summary"]
        self.assertEqual(summary["session_bytes"], 0)
        self.assertEqual(summary["bytes_7d"], 0)

    async def test_usenet_snapshot_converts_bigint_aggregates(self):
        database = Mock(
            fetch_all=AsyncMock(return_value=[]),
            fetch_one=AsyncMock(
                side_effect=[
                    {
                        "artifacts": 0,
                        "nzb_bytes": Decimal(0),
                        "materialized_bytes": Decimal(0),
                        "active_readers": 0,
                        "eligible_for_prune": 0,
                    },
                    {
                        "streams_7d": 0,
                        "failed_7d": 0,
                        "bytes_7d": Decimal(0),
                    },
                ]
            ),
        )
        with patch.object(api_usenet, "database", database):
            response = await api_usenet.usenet_snapshot(Mock(headers={}), "session")

        data = json.loads(response.body)["data"]
        self.assertEqual(data["inventory"]["nzb_bytes"], 0)
        self.assertEqual(data["inventory"]["materialized_bytes"], 0)
        self.assertEqual(data["history"]["bytes_7d"], 0)

    async def test_database_metrics_converts_bigint_aggregates(self):
        database = Mock(
            fetch_all=AsyncMock(
                side_effect=[
                    [
                        {
                            "dimension": "summary",
                            "label": "",
                            "count": 0,
                            "unique_media": 0,
                            "seen_24h": 0,
                            "seen_7d": 0,
                            "average_size": None,
                            "maximum_size": None,
                        }
                    ],
                    [
                        {
                            "service": "realdebrid",
                            "count": 1,
                            "average_size": Decimal(42),
                            "total_size": Decimal(42),
                        }
                    ],
                ]
            ),
            fetch_one=AsyncMock(
                return_value={
                    "total_unique": 0,
                    "last_24h": 0,
                    "last_7d": 0,
                    "last_30d": 0,
                }
            ),
            fetch_val=AsyncMock(side_effect=[0, 1]),
        )

        snapshot = await _collect_database_metrics(
            database,
            now=100.0,
            debrid_cache_ttl=60,
        )

        self.assertEqual(snapshot.debrid_cache.by_service[0].total_size, 42)
