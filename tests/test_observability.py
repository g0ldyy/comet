import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from starlette.requests import Request

from comet.api import app as app_module
from comet.api import configure_metrics
from comet.api.endpoints import prometheus
from comet.observability import metric_snapshot, metrics
from comet.observability.metrics import (
    CometMetrics,
    configure_multiprocess_directory,
    load_auth_token,
    prepare_multiprocess_directory,
)
from comet.usenet.engine_stats import ENGINE_STAT_FIELDS


class ObservabilityConfigurationTests(unittest.TestCase):
    def test_internal_metrics_remain_enabled_without_prometheus_endpoint(self):
        settings = SimpleNamespace(
            PROMETHEUS_ENABLED=False,
            PROMETHEUS_MULTIPROC_DIR="/tmp/comet-prometheus",
            USENET_ENABLED=False,
        )

        with (
            patch(
                "comet.observability.metrics.configure_multiprocess_directory"
            ) as configure_directory,
            patch("comet.observability.metrics.metrics.configure") as configure,
            patch(
                "comet.observability.metrics.metrics.set_usenet_engine_configured"
            ) as configure_usenet,
        ):
            configure_metrics(settings)

        configure_directory.assert_called_once_with("/tmp/comet-prometheus")
        configure.assert_called_once_with(True, None)
        configure_usenet.assert_called_once_with(False)

    def test_native_gauges_zero_all_stats_and_track_configuration(self):
        observed = {name: Mock() for name in ENGINE_STAT_FIELDS}
        instance = CometMetrics()
        instance.enabled = True
        instance.usenet_engine_up = Mock()
        instance.usenet_engine_configured = Mock()
        instance.usenet_engine_last_snapshot_timestamp = Mock()
        instance.ready = Mock()
        instance.readiness_degraded = Mock()
        instance._child = lambda _collector, name: observed[name]

        instance.set_usenet_engine_configured(True)
        instance.set_usenet_engine_stats(None)
        instance.set_readiness("degraded")

        instance.usenet_engine_configured.set.assert_called_once_with(1)
        instance.usenet_engine_up.set.assert_called_once_with(0)
        instance.usenet_engine_last_snapshot_timestamp.set.assert_called_once_with(0)
        self.assertTrue(
            all(gauge.set.call_args.args == (0,) for gauge in observed.values())
        )
        instance.ready.set.assert_called_once_with(1)
        instance.readiness_degraded.set.assert_called_once_with(1)

    def test_auth_token_file_is_trimmed_and_direct_token_takes_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "token"
            token_file.write_text(" file-secret\n", encoding="utf-8")

            self.assertEqual(load_auth_token(None, str(token_file)), "file-secret")
            self.assertEqual(
                load_auth_token("direct-secret", str(token_file)),
                "direct-secret",
            )

    def test_empty_auth_token_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "token"
            token_file.write_text("\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "must not be empty"):
                load_auth_token(None, str(token_file))

    def test_auth_tokens_have_one_bounded_header_safe_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "token"
            for value in (
                "x" * 4_097,
                "contains space",
                "non-ascii-é",
                "\x7f",
            ):
                with self.subTest(value=value):
                    token_file.write_text(value, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        load_auth_token(None, str(token_file))

        with self.assertRaises(ValueError):
            load_auth_token("x" * 4_097, None)

    def test_multiprocess_cleanup_only_removes_prometheus_mmap_files(self):
        with tempfile.TemporaryDirectory() as directory:
            metrics_dir = Path(directory) / "comet-prometheus"
            metrics_dir.mkdir()
            mmap_file = metrics_dir / "counter_123.db"
            unrelated_file = metrics_dir / "keep.txt"
            unrelated_database = metrics_dir / "comet.db"
            lookalike_database = metrics_dir / "counter_current.db"
            mmap_file.write_bytes(b"stale")
            unrelated_file.write_bytes(b"keep")
            unrelated_database.write_bytes(b"keep")
            lookalike_database.write_bytes(b"keep")

            with patch.dict(os.environ, {}, clear=False):
                prepare_multiprocess_directory(str(metrics_dir))

                self.assertFalse(mmap_file.exists())
                self.assertTrue(unrelated_file.exists())
                self.assertTrue(unrelated_database.exists())
                self.assertTrue(lookalike_database.exists())
                self.assertEqual(
                    os.environ["PROMETHEUS_MULTIPROC_DIR"],
                    str(metrics_dir.resolve()),
                )

    def test_single_level_multiprocess_directory_is_accepted(self):
        with (
            patch.object(Path, "mkdir") as mkdir,
            patch.dict(os.environ, {}, clear=False),
        ):
            directory = configure_multiprocess_directory("/metrics")
            self.assertEqual(os.environ["PROMETHEUS_MULTIPROC_DIR"], "/metrics")

        self.assertEqual(directory, Path("/metrics"))
        mkdir.assert_called_once_with(parents=True, exist_ok=True)

    def test_multiprocessing_import_does_not_clear_metric_files(self):
        script = """
import os
import runpy
import sys
from pathlib import Path

directory = Path(sys.argv[1])
metric_file = directory / "counter_123.db"
metric_file.write_bytes(b"live")
os.environ["PROMETHEUS_ENABLED"] = "true"
os.environ["PROMETHEUS_MULTIPROC_DIR"] = str(directory)
runpy.run_module("comet.main", run_name="__mp_main__")
assert metric_file.read_bytes() == b"live"
"""
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.run(
                [sys.executable, "-c", script, directory],
                cwd=Path(__file__).parents[1],
                capture_output=True,
                check=False,
                text=True,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_protected_api_prefix_is_redacted_from_route_label(self):
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/s/private-token/stream/movie/id.json",
                "headers": [],
                "route": SimpleNamespace(
                    path="/s/private-token/stream/{media_type}/{media_id}.json"
                ),
            }
        )
        request.scope["route"].name = "stream"
        route = app_module._route_name(request.scope)

        self.assertEqual(route, "stream")
        self.assertNotIn("private-token", route)

    def test_configuration_segment_is_not_exposed_by_route_label(self):
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/encoded-secret/stream/movie/id.json",
                "headers": [],
                "route": SimpleNamespace(
                    path="/{b64config}/stream/{media_type}/{media_id}.json"
                ),
            }
        )

        request.scope["route"].name = "stream"
        route = app_module._route_name(request.scope)

        self.assertEqual(route, "stream")
        self.assertNotIn("encoded-secret", route)

    def test_metric_names_are_exposed_with_bounded_labels(self):
        script = """
from comet.observability.metrics import CometMetrics
from comet.usenet.engine_stats import ENGINE_STAT_FIELDS
from prometheus_client import generate_latest

instance = CometMetrics()
instance.configure(True)
instance.observe_scraper("Zilean #42", "live", "success", 0.25, 3)
snapshot = {name: 0 for name in ENGINE_STAT_FIELDS}
snapshot["disk_cache_stats_available"] = True
snapshot["spool_stats_available"] = True
snapshot["sessions"] = 2
instance.set_usenet_engine_stats(snapshot)
payload = generate_latest().decode()
assert 'comet_scraper_requests_total{context="live",outcome="success",scraper="zilean"} 1.0' in payload
assert "comet_usenet_engine_up 1.0" in payload
assert 'comet_usenet_engine_stat{stat="sessions"} 2.0' in payload
assert "Zilean #42" not in payload
assert "configuration_partition" not in payload
instance.http_started("attacker-method-1")
instance.http_finished("attacker-method-1", "unmatched", 404, 0.1, None)
instance.http_started("attacker-method-2")
instance.http_finished("attacker-method-2", "unmatched", 404, 0.1, None)
assert 'comet_http_requests_total{method="OTHER",route="unmatched",status="404"} 2.0' in generate_latest().decode()
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).parents[1],
            capture_output=True,
            check=False,
            env={
                key: value
                for key, value in os.environ.items()
                if key != "PROMETHEUS_MULTIPROC_DIR"
            },
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_enabled_app_uses_multiprocess_registry_and_mounts_endpoint(self):
        script = """
from comet.api.app import fastapi_app
from comet.observability import metrics, render_metrics

metrics.observe_torrent_cache("movie", "hit", 3)
assert str(fastapi_app.url_path_for("prometheus_metrics")) == "/metrics"
assert b"comet_torrent_cache_lookups_total" in render_metrics()
"""
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment.update(
                {
                    "PROMETHEUS_ENABLED": "true",
                    "PROMETHEUS_AUTH_TOKEN": "secret",
                    "PROMETHEUS_MULTIPROC_DIR": directory,
                }
            )
            completed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=Path(__file__).parents[1],
                capture_output=True,
                check=False,
                env=environment,
                text=True,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_provisioned_dashboard_and_stack_are_safe_by_default(self):
        root = Path(__file__).parents[1]
        compose = (
            root / "deployment/monitoring/docker-compose.monitoring.yml"
        ).read_text(encoding="utf-8")
        dashboard = json.loads(
            (
                root / "deployment/monitoring/grafana/dashboards/comet-overview.json"
            ).read_text(encoding="utf-8")
        )

        self.assertEqual(dashboard["uid"], "comet-production-overview")
        self.assertGreaterEqual(len(dashboard["panels"]), 20)
        healthy_zero_panels = {
            panel["title"]: panel["targets"][0]["expr"]
            for panel in dashboard["panels"]
            if panel["title"]
            in {"HTTP 5xx ratio", "Torrent cache hit ratio", "DB errors · 5m"}
        }
        self.assertEqual(
            set(healthy_zero_panels),
            {"HTTP 5xx ratio", "Torrent cache hit ratio", "DB errors · 5m"},
        )
        self.assertTrue(
            all("or vector(0)" in query for query in healthy_zero_panels.values())
        )
        self.assertIn("PROMETHEUS_AUTH_TOKEN_FILE: /run/secrets/", compose)
        self.assertIn("no-new-privileges:true", compose)
        self.assertNotRegex(compose, r"(?m)^\s+ports:\n\s+- [\"']?9090")


class PrometheusEndpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        metric_snapshot._engine_lock = asyncio.Lock()
        metric_snapshot._engine_refreshed_at = float("-inf")

    async def test_bearer_token_is_required_when_configured(self):
        with patch.object(metrics, "auth_token", "secret"):
            for authorization in (None, "Bearer incorrect", "Bearer secrét"):
                with (
                    self.subTest(authorization=authorization),
                    self.assertRaises(HTTPException) as context,
                ):
                    await prometheus.prometheus_metrics(authorization)

                self.assertEqual(context.exception.status_code, 401)
                self.assertEqual(
                    context.exception.headers["Cache-Control"],
                    "no-store",
                )

    async def test_endpoint_returns_prometheus_content_type(self):
        with (
            patch.object(metrics, "auth_token", "secret"),
            patch.object(prometheus, "render_metrics", return_value=b"metric 1\n"),
        ):
            response = await prometheus.prometheus_metrics("Bearer secret")

        self.assertEqual(response.body, b"metric 1\n")
        self.assertEqual(
            response.headers["content-type"],
            "text/plain; version=0.0.4; charset=utf-8",
        )
        self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_authorized_scrape_refreshes_only_bounded_native_stats(self):
        engine = type(
            "Engine",
            (),
            {"stats": AsyncMock(return_value={"sessions": 2})},
        )()
        with (
            patch.object(metrics, "auth_token", "secret"),
            patch.object(metrics, "enabled", True),
            patch.object(metrics, "set_usenet_engine_stats") as set_stats,
            patch.object(prometheus.settings, "USENET_ENABLED", True),
            patch.object(metric_snapshot, "EngineClient", return_value=engine),
            patch.object(prometheus, "render_metrics", return_value=b"metric 1\n"),
        ):
            await prometheus.prometheus_metrics("Bearer secret")

        engine.stats.assert_awaited_once_with()
        set_stats.assert_called_once_with({"sessions": 2})

    async def test_native_stats_outage_marks_engine_down_without_breaking_scrape(self):
        engine = type(
            "Engine",
            (),
            {
                "stats": AsyncMock(
                    side_effect=metric_snapshot.EngineUnavailable("engine unavailable")
                )
            },
        )()
        with (
            patch.object(metrics, "auth_token", "secret"),
            patch.object(metrics, "enabled", True),
            patch.object(metrics, "set_usenet_engine_stats") as set_stats,
            patch.object(prometheus.settings, "USENET_ENABLED", True),
            patch.object(metric_snapshot, "EngineClient", return_value=engine),
            patch.object(prometheus, "render_metrics", return_value=b"metric 1\n"),
        ):
            response = await prometheus.prometheus_metrics("Bearer secret")

        self.assertEqual(response.body, b"metric 1\n")
        set_stats.assert_called_once_with(None)

    async def test_unauthorized_scrape_never_touches_the_native_socket(self):
        with (
            patch.object(metrics, "auth_token", "secret"),
            patch.object(metrics, "enabled", True),
            patch.object(prometheus.settings, "USENET_ENABLED", True),
            patch.object(metric_snapshot, "EngineClient") as engine_client,
            self.assertRaises(HTTPException),
        ):
            await prometheus.prometheus_metrics("Bearer incorrect")

        engine_client.assert_not_called()

    async def test_concurrent_scrapes_share_one_native_refresh(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def stats():
            started.set()
            await release.wait()
            return {"sessions": 2}

        engine = type("Engine", (), {"stats": AsyncMock(side_effect=stats)})()
        with (
            patch.object(metrics, "auth_token", "secret"),
            patch.object(metrics, "enabled", True),
            patch.object(metrics, "set_usenet_engine_stats"),
            patch.object(prometheus.settings, "USENET_ENABLED", True),
            patch.object(metric_snapshot, "EngineClient", return_value=engine),
            patch.object(prometheus, "render_metrics", return_value=b"metric 1\n"),
        ):
            first = asyncio.create_task(prometheus.prometheus_metrics("Bearer secret"))
            await started.wait()
            second = asyncio.create_task(prometheus.prometheus_metrics("Bearer secret"))
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(first, second)

        self.assertEqual(engine.stats.await_count, 1)


if __name__ == "__main__":
    unittest.main()
