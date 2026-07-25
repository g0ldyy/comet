import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from comet.utils.atomic_file import write_text_atomic


class AtomicFileTests(unittest.IsolatedAsyncioTestCase):
    async def test_file_sync_failure_preserves_previous_file_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text("previous")

            with (
                patch("comet.utils.atomic_file.os.fsync", side_effect=OSError("fail")),
                self.assertRaisesRegex(OSError, "fail"),
            ):
                await write_text_atomic(path, "replacement")

            self.assertEqual(path.read_text(), "previous")
            self.assertEqual(list(Path(directory).glob(".*.tmp")), [])

    async def test_replace_failure_preserves_previous_file_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text("previous")

            with (
                patch(
                    "comet.utils.atomic_file.os.replace", side_effect=OSError("fail")
                ),
                self.assertRaisesRegex(OSError, "fail"),
            ):
                await write_text_atomic(path, "replacement")

            self.assertEqual(path.read_text(), "previous")
            self.assertEqual(list(Path(directory).glob(".*.tmp")), [])

    async def test_success_replaces_complete_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text("previous")

            await write_text_atomic(path, "replacement")

            self.assertEqual(path.read_text(), "replacement")
            self.assertEqual(list(Path(directory).glob(".*.tmp")), [])

    async def test_success_syncs_file_before_replace_and_directory_after(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            events = []

            def fsync(fd):
                del fd
                events.append("fsync")

            def replace(source, target):
                events.append("replace")
                Path(source).rename(target)

            with (
                patch("comet.utils.atomic_file.os.fsync", side_effect=fsync),
                patch("comet.utils.atomic_file.os.replace", side_effect=replace),
            ):
                await write_text_atomic(path, "complete")

            self.assertEqual(events, ["fsync", "replace", "fsync"])
            self.assertEqual(path.read_text(), "complete")
