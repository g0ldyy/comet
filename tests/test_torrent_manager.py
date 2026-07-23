import hashlib
import unittest
from unittest.mock import patch

import bencodepy

from comet.services import torrent_manager
from comet.services.torrent_manager import extract_torrent_metadata
from comet.utils.parsing import is_video


class TorrentMetadataTests(unittest.TestCase):
    def test_info_hash_normalization_rejects_non_hexadecimal_length_match(self):
        self.assertIsNone(torrent_manager._normalize_valid_info_hash("z" * 40))
        self.assertIsNone(torrent_manager._normalize_valid_info_hash("a" * 39))
        self.assertEqual(
            torrent_manager._normalize_valid_info_hash("A" * 40),
            "a" * 40,
        )

    def test_extracts_every_tracker_and_uppercase_video_file(self):
        info = {b"name": b"Movie.MKV", b"length": 1234}
        content = bencodepy.encode(
            {
                b"announce": b"udp://fallback.example",
                b"announce-list": [
                    [b"udp://one.example", b"udp://two.example"],
                    [b"udp://three.example", b"\xff"],
                    b"invalid-tier",
                ],
                b"info": info,
            }
        )

        actual = extract_torrent_metadata(content)

        self.assertEqual(
            actual["sources"],
            [
                "udp://one.example",
                "udp://two.example",
                "udp://three.example",
                "udp://fallback.example",
            ],
        )
        self.assertEqual(
            actual["info_hash"], hashlib.sha1(bencodepy.encode(info)).hexdigest()
        )
        self.assertEqual(
            actual["files"], [{"index": 0, "title": "Movie.MKV", "size": 1234}]
        )

    def test_video_extension_matching_is_case_insensitive(self):
        self.assertTrue(is_video("Movie.MKV"))
        self.assertTrue(is_video("Movie.mKv"))
        self.assertFalse(is_video("Movie.txt"))

    def test_skips_corrupt_file_entries_without_dropping_valid_files(self):
        info = {
            b"name": b"collection",
            b"files": [
                {b"path": [b"valid.mkv"], b"length": 100},
                {b"path": [b"invalid-\xff.mkv"], b"length": 200},
                {b"path": [], b"length": 300},
                {b"path": [b"missing-size.mp4"]},
                {b"path": [b"notes.txt"], b"length": 400},
                {b"path": [b"also-valid.MP4"], b"length": 500},
            ],
        }
        content = bencodepy.encode({b"info": info})

        actual = extract_torrent_metadata(content)

        self.assertEqual(
            actual["info_hash"], hashlib.sha1(bencodepy.encode(info)).hexdigest()
        )
        self.assertEqual(
            actual["files"],
            [
                {"index": 0, "title": "valid.mkv", "size": 100},
                {"index": 5, "title": "also-valid.MP4", "size": 500},
            ],
        )


class TorrentPersistenceTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _make_update(title: str, index: int):
        return torrent_manager._construct_torrent_update(
            media_id="tt1234567",
            info_hash=f"{index:040x}",
            season=None,
            episode=None,
            file_index=index,
            title=title,
            seeders=index,
            size=index,
            tracker=None,
            sources=[],
            parsed={},
            from_cometnet=False,
        )

    async def test_row_error_isolated_without_dropping_valid_updates(self):
        rows = [
            self._make_update("valid-a.mkv", 1),
            self._make_update("poison.mkv", 2),
            self._make_update("valid-b.mkv", 3),
        ]
        calls = []

        async def reject_poison(batch, *, updated_at):
            calls.append([row.title for row in batch])
            if any(row.title == "poison.mkv" for row in batch):
                raise ValueError("invalid input syntax for type bigint")

        with patch.object(
            torrent_manager, "_execute_batched_upsert", new=reject_poison
        ):
            persisted = await torrent_manager._execute_isolated_batched_upsert(
                rows, updated_at=123.0
            )

        self.assertEqual(
            [row.title for row in persisted], ["valid-a.mkv", "valid-b.mkv"]
        )
        self.assertEqual(calls[0], ["valid-a.mkv", "poison.mkv", "valid-b.mkv"])
        self.assertIn(["poison.mkv"], calls)

    async def test_queue_broadcasts_only_rows_persisted_after_isolation(self):
        queue = torrent_manager.TorrentUpdateQueue(batch_size=3, flush_interval=0)
        broadcasts = []

        async def reject_poison(batch, *, updated_at):
            if any(row.title == "poison.mkv" for row in batch):
                raise ValueError("invalid input syntax for type bigint")

        async def record_broadcast(batch, updated_at):
            broadcasts.append([row.title for row in batch])

        infos = [
            {
                "info_hash": f"{index:040x}",
                "title": title,
                "size": index,
            }
            for index, title in enumerate(
                ("valid-a.mkv", "poison.mkv", "valid-b.mkv"), 1
            )
        ]
        with (
            patch.object(torrent_manager, "_execute_batched_upsert", new=reject_poison),
            patch.object(queue, "_enqueue_broadcast_items", new=record_broadcast),
        ):
            await queue.add_torrent_infos(infos, media_id="tt1234567")
            await queue.queue.join()
            await queue.stop()

        self.assertEqual(broadcasts, [["valid-a.mkv", "valid-b.mkv"]])

    async def test_retryable_error_is_not_split(self):
        rows = [
            self._make_update("valid-a.mkv", 1),
            self._make_update("valid-b.mkv", 2),
        ]
        calls = 0

        async def locked(batch, *, updated_at):
            nonlocal calls
            calls += 1
            raise RuntimeError("database is locked")

        with patch.object(torrent_manager, "_execute_batched_upsert", new=locked):
            with self.assertRaisesRegex(RuntimeError, "database is locked"):
                await torrent_manager._execute_isolated_batched_upsert(
                    rows, updated_at=123.0
                )

        self.assertEqual(calls, 1)

    async def test_global_error_is_not_split(self):
        rows = [
            self._make_update("valid-a.mkv", 1),
            self._make_update("valid-b.mkv", 2),
        ]
        calls = 0

        async def disconnected(batch, *, updated_at):
            nonlocal calls
            calls += 1
            raise RuntimeError("connection closed")

        with patch.object(torrent_manager, "_execute_batched_upsert", new=disconnected):
            with self.assertRaisesRegex(RuntimeError, "connection closed"):
                await torrent_manager._execute_isolated_batched_upsert(
                    rows, updated_at=123.0
                )

        self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()
