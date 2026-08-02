import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from comet.services import status_video


class StatusVideoResponseTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        status_video._build_status_video_index.cache_clear()

    async def test_range_requests_restart_status_video_from_the_beginning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "UNKNOWN.mp4").write_bytes(b"complete status video")
            with patch.object(status_video, "STATUS_VIDEO_DIR", root):
                response = status_video.build_status_video_response(["UNKNOWN"])

            messages = []

            async def receive():
                return {"type": "http.disconnect"}

            async def send(message):
                messages.append(message)

            await response(
                {
                    "type": "http",
                    "http_version": "1.1",
                    "method": "GET",
                    "scheme": "http",
                    "path": "/playback",
                    "raw_path": b"/playback",
                    "query_string": b"",
                    "headers": [(b"range", b"bytes=9-")],
                },
                receive,
                send,
            )

        headers = dict(messages[0]["headers"])
        self.assertEqual(messages[0]["status"], 200)
        self.assertEqual(headers[b"accept-ranges"], b"none")
        self.assertNotIn(b"content-range", headers)
        self.assertEqual(
            b"".join(message.get("body", b"") for message in messages[1:]),
            b"complete status video",
        )
