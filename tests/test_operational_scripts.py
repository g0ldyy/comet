import argparse
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import generate_status_videos, uptime_monitor


class _Response:
    def __init__(self, status, payload):
        self.status = status
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def json(self):
        return self.payload


class _Session:
    def __init__(self, responses):
        self.responses = iter(responses)

    def get(self, _url):
        return next(self.responses)


class UptimeMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_check_instance_requires_current_manifest_schema(self):
        session = _Session([_Response(200, {"id": "comet"})])

        status = await uptime_monitor.check_instance(session, "https://comet.test")

        self.assertFalse(status.is_online)
        self.assertFalse(status.manifest_ok)
        self.assertFalse(status.search_ok)
        self.assertEqual(status.error, "invalid manifest response schema")

    async def test_check_instance_requires_stream_objects(self):
        session = _Session(
            [
                _Response(200, {"id": "comet", "resources": []}),
                _Response(200, {"streams": ["invalid"]}),
            ]
        )

        status = await uptime_monitor.check_instance(session, "https://comet.test")

        self.assertTrue(status.is_online)
        self.assertTrue(status.manifest_ok)
        self.assertFalse(status.search_ok)
        self.assertEqual(status.error, "invalid stream response schema")

    async def test_check_instance_accepts_empty_current_stream_response(self):
        session = _Session(
            [
                _Response(200, {"id": "comet", "resources": []}),
                _Response(200, {"streams": []}),
            ]
        )

        status = await uptime_monitor.check_instance(session, "https://comet.test")

        self.assertTrue(status.is_online)
        self.assertTrue(status.search_ok)
        self.assertIsNone(status.error)

    def test_configuration_rejects_duplicate_normalized_instances(self):
        with (
            patch.object(
                uptime_monitor,
                "INSTANCES",
                ["https://comet.test", "https://comet.test/"],
            ),
            self.assertRaisesRegex(ValueError, "duplicate"),
        ):
            uptime_monitor.validate_configuration()


class StatusVideoGeneratorTests(unittest.TestCase):
    def _encode(self, background: Path, output: Path) -> None:
        generate_status_videos.encode_status_video(
            "ffmpeg",
            background,
            output,
            "Status message",
            width=1280,
            height=720,
            duration=8,
            fps=18,
            crf=24,
            maxrate="1200k",
            bufsize="2400k",
            preset="veryslow",
            font_file=None,
            timeout=30,
        )

    def test_encode_publishes_nonempty_video_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            background = root / "background.mp4"
            output = root / "status.mp4"
            background.write_bytes(b"background")
            output.write_bytes(b"old")
            os.chmod(output, 0o640)

            def generate(command, *, check, timeout):
                self.assertTrue(check)
                self.assertEqual(timeout, 30)
                temporary_output = Path(command[-1])
                self.assertNotEqual(temporary_output, output)
                temporary_output.write_bytes(b"new-video")

            with patch.object(generate_status_videos.subprocess, "run", generate):
                self._encode(background, output)

            self.assertEqual(output.read_bytes(), b"new-video")
            self.assertEqual(output.stat().st_mode & 0o777, 0o640)
            self.assertEqual(list(root.glob(".status.mp4.*.tmp.mp4")), [])

    def test_encode_failure_preserves_existing_video(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            background = root / "background.mp4"
            output = root / "status.mp4"
            background.write_bytes(b"background")
            output.write_bytes(b"old")

            with (
                patch.object(
                    generate_status_videos.subprocess,
                    "run",
                    side_effect=subprocess.CalledProcessError(1, ["ffmpeg"]),
                ),
                self.assertRaises(subprocess.CalledProcessError),
            ):
                self._encode(background, output)

            self.assertEqual(output.read_bytes(), b"old")
            self.assertEqual(list(root.glob(".status.mp4.*.tmp.mp4")), [])

    def test_encode_publishes_new_video_with_readable_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            background = root / "background.mp4"
            output = root / "status.mp4"
            background.write_bytes(b"background")

            def generate(command, *, check, timeout):
                Path(command[-1]).write_bytes(b"new-video")

            with patch.object(generate_status_videos.subprocess, "run", generate):
                self._encode(background, output)

            self.assertEqual(output.stat().st_mode & 0o777, 0o644)

    def test_clean_output_waits_for_all_generation_to_succeed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            background = root / "background.mp4"
            output_dir = root / "output"
            stale = output_dir / "STALE.mp4"
            background.write_bytes(b"background")
            output_dir.mkdir()
            stale.write_bytes(b"keep-on-failure")
            args = SimpleNamespace(
                background=str(background),
                output_dir=str(output_dir),
                font_file=None,
                ffmpeg_bin="ffmpeg",
                ffmpeg_timeout=30,
                single_file=None,
                single_message=None,
                messages_file=None,
                code=["NEW_STATUS"],
                stremthru_root=str(root / "missing"),
                scope=generate_status_videos.SCOPE_ESSENTIAL,
                limit=0,
                clean_output=True,
                overwrite=True,
                width=1280,
                height=720,
                duration=8,
                fps=18,
                crf=24,
                maxrate="1200k",
                bufsize="2400k",
                preset="veryslow",
            )

            with (
                patch.object(generate_status_videos, "parse_args", return_value=args),
                patch.object(generate_status_videos.subprocess, "run"),
                patch.object(
                    generate_status_videos,
                    "encode_status_video",
                    side_effect=RuntimeError("encode failed"),
                ),
                self.assertRaisesRegex(RuntimeError, "encode failed"),
            ):
                generate_status_videos.main()

            self.assertEqual(stale.read_bytes(), b"keep-on-failure")

    def test_message_overrides_reject_invalid_or_colliding_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            invalid = Path(directory) / "invalid.json"
            invalid.write_text('{"STATUS": true}', encoding="utf-8")
            with self.assertRaisesRegex(TypeError, "values"):
                generate_status_videos.load_message_overrides(str(invalid))

            colliding = Path(directory) / "colliding.json"
            colliding.write_text(
                '{"bad-status": "one", "bad status": "two"}', encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                generate_status_videos.load_message_overrides(str(colliding))

            duplicate = Path(directory) / "duplicate.json"
            duplicate.write_text('{"STATUS": "one", "STATUS": "two"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                generate_status_videos.load_message_overrides(str(duplicate))

    def test_numeric_argument_types_reject_out_of_contract_values(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            generate_status_videos.positive_int("0")
        with self.assertRaises(argparse.ArgumentTypeError):
            generate_status_videos.non_negative_int("-1")
        with self.assertRaises(argparse.ArgumentTypeError):
            generate_status_videos.h264_crf("52")
        with self.assertRaises(argparse.ArgumentTypeError):
            generate_status_videos.bitrate("0M")
        with self.assertRaises(argparse.ArgumentTypeError):
            generate_status_videos.status_code("---")

    def test_status_collection_rejects_invalid_utf8_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status_file = root / "internal" / "server" / "error.go"
            status_file.parent.mkdir(parents=True)
            status_file.write_bytes(b"\xff")

            with self.assertRaises(UnicodeDecodeError):
                generate_status_videos.collect_status_keys(
                    root, generate_status_videos.SCOPE_DEBRID
                )

    def test_single_mode_rejects_empty_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            background = root / "background.mp4"
            background.write_bytes(b"background")
            args = SimpleNamespace(
                background=str(background),
                output_dir=str(root / "output"),
                font_file=None,
                ffmpeg_bin="ffmpeg",
                ffmpeg_timeout=30,
                single_file="status.mp4",
                single_message=" ",
            )

            with (
                patch.object(generate_status_videos, "parse_args", return_value=args),
                patch.object(generate_status_videos.subprocess, "run"),
                self.assertRaisesRegex(ValueError, "non-empty"),
            ):
                generate_status_videos.main()


if __name__ == "__main__":
    unittest.main()
