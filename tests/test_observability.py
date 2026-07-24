import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

from comet.api import app as app_module
from comet.api.endpoints import prometheus
from comet.observability import metrics
from comet.observability.metrics import (
    configure_multiprocess_directory,
    load_auth_token,
    prepare_multiprocess_directory,
)


class ObservabilityConfigurationTests(unittest.TestCase):
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

    def test_multiprocess_cleanup_only_removes_prometheus_mmap_files(self):
        with tempfile.TemporaryDirectory() as directory:
            metrics_dir = Path(directory) / "comet-prometheus"
            metrics_dir.mkdir()
            mmap_file = metrics_dir / "counter_123.db"
            unrelated_file = metrics_dir / "keep.txt"
            mmap_file.write_bytes(b"stale")
            unrelated_file.write_bytes(b"keep")

            with patch.dict(os.environ, {}, clear=False):
                prepare_multiprocess_directory(str(metrics_dir))

                self.assertFalse(mmap_file.exists())
                self.assertTrue(unrelated_file.exists())
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
        with patch.object(app_module, "STREMIO_API_PREFIX", "/s/private-token"):
            route = app_module._metrics_route(request)

        self.assertEqual(
            route,
            "/s/{token}/stream/{media_type}/{media_id}.json",
        )
        self.assertNotIn("private-token", route)

    def test_metric_names_are_exposed_with_bounded_labels(self):
        script = """
from comet.observability.metrics import CometMetrics
from prometheus_client import generate_latest

instance = CometMetrics()
instance.configure(True)
instance.observe_scraper("Zilean #42", "live", "success", 0.25, 3)
payload = generate_latest().decode()
assert 'comet_scraper_requests_total{context="live",outcome="success",scraper="zilean"} 1.0' in payload
assert "Zilean #42" not in payload
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
from comet.api.app import app
from comet.observability import metrics, render_metrics

metrics.observe_torrent_cache("movie", "hit", 3)
assert any(route.path == "/metrics" for route in app.routes)
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
        self.assertIn("PROMETHEUS_AUTH_TOKEN_FILE: /run/secrets/", compose)
        self.assertIn("no-new-privileges:true", compose)
        self.assertNotRegex(compose, r"(?m)^\s+ports:\n\s+- [\"']?9090")


class PrometheusEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_bearer_token_is_required_when_configured(self):
        with patch.object(metrics, "auth_token", "secret"):
            for authorization in (None, "Bearer incorrect", "Bearer secrét"):
                with (
                    self.subTest(authorization=authorization),
                    self.assertRaises(HTTPException) as context,
                ):
                    await prometheus.prometheus_metrics(authorization)

                self.assertEqual(context.exception.status_code, 401)

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


if __name__ == "__main__":
    unittest.main()
