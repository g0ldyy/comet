import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from comet.services import status_video


class DashboardTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dashboard = Path("comet/templates/admin_dashboard.html").read_text(
            encoding="utf-8"
        )

    def test_polling_loaders_are_coalesced(self):
        for name in (
            "connections",
            "logs",
            "cometnet",
            "background-scraper",
        ):
            with self.subTest(name=name):
                self.assertIsNotNone(
                    re.search(
                        rf'coalesceRefresh\(\s*"{re.escape(name)}"',
                        self.dashboard,
                    )
                )
        self.assertIn("const activeRefreshes = new Map()", self.dashboard)

    def test_external_update_url_is_protocol_checked(self):
        self.assertIn("function safeGitHubUrl", self.dashboard)
        self.assertIn('url.protocol === "https:"', self.dashboard)
        self.assertIn('url.hostname === "github.com"', self.dashboard)
        self.assertNotIn('href="${status.latest_url}"', self.dashboard)
        self.assertNotIn("${status.latest_commit_hash}", self.dashboard)

    def test_log_and_toast_text_do_not_use_html_injection(self):
        self.assertNotIn("logEntry.innerHTML", self.dashboard)
        self.assertIn("message.textContent = String(log.message", self.dashboard)
        self.assertIn(
            'alert.append(icon, document.createTextNode(String(message ?? "")))',
            self.dashboard,
        )
        self.assertIn("level.style.color = safeLogColor", self.dashboard)

    def test_connection_identity_fields_are_escaped(self):
        self.assertNotIn('<span class="ip-tag">${conn.ip}</span>', self.dashboard)
        self.assertIn('escapeHtml(String(conn.ip || ""))', self.dashboard)
        self.assertIn(
            'escapeHtml(String(conn.id || "").substring(0, 8))', self.dashboard
        )

    def test_template_sources_are_not_publicly_mounted(self):
        app_source = Path("comet/api/app.py").read_text(encoding="utf-8")

        self.assertNotIn("StaticFiles", app_source)
        self.assertNotIn('app.mount("/static"', app_source)


class StatusVideoIndexTests(unittest.TestCase):
    def tearDown(self):
        status_video._build_status_video_index.cache_clear()

    def test_directory_revision_refreshes_added_and_removed_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "UNKNOWN.mp4"
            first.write_bytes(b"first")

            with patch.object(status_video, "STATUS_VIDEO_DIR", root):
                self.assertEqual(
                    status_video.resolve_status_video_path(["UNKNOWN"]), str(first)
                )

                second = root / "BAD_REQUEST.mp4"
                second.write_bytes(b"second")
                os.utime(
                    root, ns=(root.stat().st_atime_ns, root.stat().st_mtime_ns + 1)
                )
                self.assertEqual(
                    status_video.resolve_status_video_path(["BAD_REQUEST"]),
                    str(second),
                )

                first.unlink()
                os.utime(
                    root, ns=(root.stat().st_atime_ns, root.stat().st_mtime_ns + 1)
                )
                self.assertIsNone(status_video.resolve_status_video_path(["UNKNOWN"]))

    def test_missing_key_never_selects_arbitrary_first_asset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "BAD_REQUEST.mp4").write_bytes(b"video")

            with patch.object(status_video, "STATUS_VIDEO_DIR", root):
                self.assertIsNone(
                    status_video.resolve_status_video_path(
                        ["MISSING"], default_key="ALSO_MISSING"
                    )
                )


if __name__ == "__main__":
    unittest.main()
