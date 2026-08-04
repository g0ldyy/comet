import asyncio
import hashlib
import sqlite3
import unittest
from unittest.mock import AsyncMock, Mock, patch

import bencodepy

from comet.cometnet.manager import CometNetService
from comet.services import torrent_manager
from comet.services.torrent_manager import extract_torrent_metadata
from comet.usenet.outbound import ValidatedUrl, configured_http_origin
from comet.utils.parsing import is_video


class _TorrentContent:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    async def read(self, _maximum):
        return self.chunks.pop(0) if self.chunks else b""


class _BlockingTorrentContent:
    async def read(self, _maximum):
        await asyncio.Event().wait()


class _TorrentResponse:
    def __init__(self, status, headers, chunks):
        self.status = status
        self.headers = headers
        self.content = _TorrentContent(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class _TorrentSession:
    def __init__(self, response):
        self.response = response
        self.request_kwargs = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def get(self, _url, **kwargs):
        self.request_kwargs = kwargs
        return self.response


class TorrentMetadataTests(unittest.TestCase):
    def test_parsed_merge_uses_new_nonempty_values_not_value_length(self):
        self.assertEqual(
            torrent_manager._merge_parsed_payloads(
                {
                    "resolution": "unknown",
                    "languages": ["en", "fr"],
                    "nested": {"codec": "unknown"},
                },
                {
                    "resolution": "4k",
                    "languages": ["fr"],
                    "nested": {"codec": "av1"},
                },
            ),
            {
                "resolution": "4k",
                "languages": ["fr"],
                "nested": {"codec": "av1"},
            },
        )

    def test_parsed_payload_serialization_failure_propagates(self):
        with (
            patch.object(
                torrent_manager,
                "default_dump",
                side_effect=RuntimeError("serialization failure"),
            ),
            self.assertRaisesRegex(RuntimeError, "serialization failure"),
        ):
            torrent_manager._coerce_parsed_payload(object())

    def test_video_title_parser_failure_propagates(self):
        with (
            patch.object(
                torrent_manager,
                "parse",
                side_effect=RuntimeError("parser failure"),
            ),
            self.assertRaisesRegex(RuntimeError, "parser failure"),
        ):
            torrent_manager._parse_video_title_payload("Movie.2026.mkv")

    def test_info_hash_normalization_rejects_non_hexadecimal_length_match(self):
        self.assertIsNone(torrent_manager._normalize_valid_info_hash("z" * 40))
        self.assertIsNone(torrent_manager._normalize_valid_info_hash("a" * 39))
        self.assertEqual(
            torrent_manager._normalize_valid_info_hash("A" * 40),
            "a" * 40,
        )

    def test_magnet_hash_uses_only_the_scheme_and_unique_hash(self):
        info_hash = "a" * 40
        self.assertEqual(
            torrent_manager._extract_info_hash_from_magnet(
                f"magnet:?xt=urn:btih:{info_hash}"
            ),
            info_hash,
        )
        self.assertEqual(
            torrent_manager._extract_info_hash_from_magnet(
                f"magnet://provider.example/download#fragment?xt=urn:btih:{info_hash}"
            ),
            None,
        )
        self.assertEqual(
            torrent_manager._extract_info_hash_from_magnet(
                f"magnet://provider.example/download?xt=urn:btih:{info_hash}#fragment"
            ),
            info_hash,
        )
        self.assertIsNone(
            torrent_manager._extract_info_hash_from_magnet(
                f"https://private.example/?xt=urn:btih:{info_hash}"
            )
        )
        self.assertIsNone(
            torrent_manager._extract_info_hash_from_magnet(
                f"magnet:?xt=urn:btih:{info_hash}&xt=urn:btih:{'b' * 40}"
            )
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

    def test_video_title_containing_sample_is_not_pre_filtered(self):
        info = {b"name": b"The.Sample.2026.mkv", b"length": 1234}

        actual = extract_torrent_metadata(bencodepy.encode({b"info": info}))

        self.assertEqual(
            actual["files"],
            [{"index": 0, "title": "The.Sample.2026.mkv", "size": 1234}],
        )

    def test_video_extension_matching_is_case_insensitive(self):
        self.assertTrue(is_video("Movie.MKV"))
        self.assertTrue(is_video("Movie.mKv"))
        self.assertTrue(is_video("Movie.M2TS"))
        self.assertTrue(is_video("Movie.mts"))
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


class TorrentDownloadLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_expected_download_failure_is_non_fatal(self):
        url = "https://user:password@private.example/signed-token"

        with patch.object(
            torrent_manager,
            "validate_http_url",
            new=AsyncMock(side_effect=torrent_manager.OutboundUrlError("invalid URL")),
        ):
            result = await torrent_manager.download_torrent(None, url)

        self.assertEqual(result, (None, None, None))

    async def test_unexpected_download_failure_propagates(self):
        with (
            patch.object(
                torrent_manager,
                "validate_http_url",
                new=AsyncMock(side_effect=RuntimeError("implementation failure")),
            ),
            self.assertRaisesRegex(RuntimeError, "implementation failure"),
        ):
            await torrent_manager.download_torrent(None, "https://example.com/torrent")

    async def test_expected_magnet_resolution_failure_is_non_fatal(self):
        with patch.object(
            torrent_manager,
            "demagnetizer",
            Mock(
                demagnetize=AsyncMock(
                    side_effect=torrent_manager.DemagnetizeError("no responsive peer")
                )
            ),
        ):
            torrent = await torrent_manager.get_torrent_from_magnet(
                f"magnet:?xt=urn:btih:{'a' * 40}&tr=https://tracker.example"
            )

        self.assertIsNone(torrent)

    async def test_unexpected_magnet_resolution_failure_propagates(self):
        with (
            patch.object(
                torrent_manager,
                "demagnetizer",
                Mock(
                    demagnetize=AsyncMock(
                        side_effect=RuntimeError("implementation failure")
                    )
                ),
            ),
            self.assertRaisesRegex(RuntimeError, "implementation failure"),
        ):
            await torrent_manager.get_torrent_from_magnet(
                f"magnet:?xt=urn:btih:{'a' * 40}&tr=https://tracker.example"
            )

    async def test_download_is_pinned_identity_encoded_and_exactly_framed(self):
        document = b"d4:infod4:name5:teste"
        response = _TorrentResponse(
            200,
            {
                "Content-Encoding": "identity",
                "Content-Length": str(len(document)),
            },
            [document, b""],
        )
        session = _TorrentSession(response)
        target = ValidatedUrl(
            "http://indexer.internal/download/1",
            "http",
            "indexer.internal",
            80,
            "http://indexer.internal:80",
            ((2, "192.168.1.10"),),
        )
        allowed = frozenset({"http://indexer.internal:80"})

        with (
            patch.object(
                torrent_manager,
                "validate_http_url",
                new=AsyncMock(return_value=target),
            ) as validate,
            patch.object(torrent_manager.aiohttp, "TCPConnector") as connector,
            patch.object(
                torrent_manager.aiohttp,
                "ClientSession",
                return_value=session,
            ) as client_session,
        ):
            result = await torrent_manager.download_torrent(
                object(),
                target.url,
                allowed_private_origins=allowed,
            )

        self.assertEqual(result, (document, None, None))
        validate.assert_awaited_once_with(
            target.url,
            allowed_private_origins=allowed,
        )
        connector.assert_called_once()
        client_session.assert_called_once()
        self.assertNotIn("auto_decompress", client_session.call_args.kwargs)
        self.assertEqual(
            session.request_kwargs,
            {
                "headers": {
                    "Accept": "application/x-bittorrent",
                    "Accept-Encoding": "identity",
                },
                "allow_redirects": False,
            },
        )

    async def test_magnet_redirect_is_returned_without_following_http(self):
        info_hash = "a" * 40
        magnet = f"magnet:?xt=urn:btih:{info_hash}"
        response = _TorrentResponse(302, {"Location": magnet}, [])
        session = _TorrentSession(response)
        target = ValidatedUrl(
            "https://indexer.example/download/1",
            "https",
            "indexer.example",
            443,
            "https://indexer.example:443",
            ((2, "203.0.113.10"),),
        )

        with (
            patch.object(
                torrent_manager,
                "validate_http_url",
                new=AsyncMock(return_value=target),
            ),
            patch.object(torrent_manager.aiohttp, "TCPConnector"),
            patch.object(
                torrent_manager.aiohttp,
                "ClientSession",
                return_value=session,
            ),
        ):
            result = await torrent_manager.download_torrent(None, target.url)

        self.assertEqual(result, (None, info_hash, magnet))

    async def test_download_ignores_a_lying_declared_size_and_reads_the_body(self):
        response = _TorrentResponse(
            200,
            {
                "Content-Encoding": "identity",
                "Content-Length": str(torrent_manager.MAX_TORRENT_DOCUMENT_BYTES + 1),
            },
            [],
        )
        session = _TorrentSession(response)
        target = ValidatedUrl(
            "https://indexer.example/download/large",
            "https",
            "indexer.example",
            443,
            "https://indexer.example:443",
            ((2, "203.0.113.10"),),
        )

        with (
            patch.object(
                torrent_manager,
                "validate_http_url",
                new=AsyncMock(return_value=target),
            ),
            patch.object(torrent_manager.aiohttp, "TCPConnector"),
            patch.object(
                torrent_manager.aiohttp,
                "ClientSession",
                return_value=session,
            ),
        ):
            result = await torrent_manager.download_torrent(None, target.url)

        self.assertEqual(result, (None, None, None))

    async def test_download_timeout_is_global_and_non_fatal(self):
        response = _TorrentResponse(200, {}, [])
        response.content = _BlockingTorrentContent()
        session = _TorrentSession(response)
        target = ValidatedUrl(
            "https://indexer.example/download/slow",
            "https",
            "indexer.example",
            443,
            "https://indexer.example:443",
            ((2, "203.0.113.10"),),
        )

        with (
            patch.object(
                torrent_manager,
                "validate_http_url",
                new=AsyncMock(return_value=target),
            ),
            patch.object(torrent_manager.aiohttp, "TCPConnector"),
            patch.object(
                torrent_manager.aiohttp,
                "ClientSession",
                return_value=session,
            ),
            patch.object(torrent_manager.settings, "GET_TORRENT_TIMEOUT", 0.01),
        ):
            result = await torrent_manager.download_torrent(None, target.url)

        self.assertEqual(result, (None, None, None))

    def test_configured_private_origin_is_exact_and_credential_free(self):
        self.assertEqual(
            configured_http_origin("http://INDEXER.internal:9696/base/"),
            "http://indexer.internal:9696",
        )
        with self.assertRaisesRegex(ValueError, "origin is invalid"):
            configured_http_origin("http://user:secret@indexer.internal/base/")
        with self.assertRaisesRegex(ValueError, "origin is invalid"):
            configured_http_origin("http://./base/")


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

    async def test_queue_propagates_unexpected_persistence_failure(self):
        queue = torrent_manager.TorrentUpdateQueue(batch_size=1, flush_interval=0)

        with patch.object(
            torrent_manager,
            "_execute_batched_upsert",
            side_effect=RuntimeError("connection closed"),
        ):
            await queue.add_torrent_info(
                {
                    "info_hash": "a" * 40,
                    "title": "Movie.2026.mkv",
                    "size": 100,
                },
                media_id="tt1234567",
            )
            worker = queue._tasks["persistence"]
            with self.assertRaisesRegex(RuntimeError, "connection closed"):
                await worker

        await queue.queue.join()

    async def test_stopping_queue_ignores_valid_network_metadata(self):
        queue = torrent_manager.TorrentUpdateQueue()
        queue._stopping = True
        metadata = torrent_manager._construct_torrent_metadata(
            info_hash="a" * 40,
            title="Movie.2026.mkv",
            size=100,
            seeders=1,
            tracker="CometNet",
            imdb_id="tt1234567",
            file_index=0,
            season=None,
            episode=None,
            sources=[],
            parsed={},
            updated_at=123.0,
        )

        with patch.object(queue, "_enqueue_prepared_item", new=AsyncMock()) as enqueue:
            await queue.add_network_torrent(metadata)

        enqueue.assert_not_awaited()

    async def test_queue_broadcasts_account_metadata_and_skips_invalid_rows(self):
        queue = torrent_manager.TorrentUpdateQueue(batch_size=3, flush_interval=0)
        backend = Mock(spec=CometNetService)
        backend.broadcast_torrents = AsyncMock()
        valid = self._make_update("valid.mkv", 1)
        valid.tracker = "DebridAccount|torbox"
        missing_size = self._make_update("missing-size.mkv", 2)
        missing_size.size = None
        zero_size = self._make_update("zero-size.mkv", 3)
        zero_size.size = 0

        with patch.object(torrent_manager, "get_active_backend", return_value=backend):
            await queue._enqueue_broadcast_items(
                [missing_size, valid, zero_size], updated_at=123.0
            )
            await queue._broadcast_queue.join()
            await queue.stop()

        backend.broadcast_torrents.assert_awaited_once()
        metadata_batch = backend.broadcast_torrents.await_args.args[0]
        self.assertEqual([metadata.title for metadata in metadata_batch], ["valid.mkv"])
        self.assertEqual(metadata_batch[0].size, 1)
        self.assertEqual(metadata_batch[0].tracker, "DebridAccount|torbox")

    def test_retryable_database_errors_use_structured_codes(self):
        locked = sqlite3.OperationalError("opaque")
        locked.sqlite_errorcode = sqlite3.SQLITE_BUSY
        self.assertTrue(torrent_manager.is_retryable_database_error(locked))
        self.assertFalse(
            torrent_manager.is_retryable_database_error(
                RuntimeError("database is locked")
            )
        )

    async def test_resolution_worker_propagates_unexpected_failure(self):
        queue = torrent_manager.AddTorrentQueue(max_concurrent=1)
        await queue.queue.put(
            (
                ("tt1234567", "a" * 40, None),
                f"magnet:?xt=urn:btih:{'a' * 40}",
            )
        )

        with (
            patch.object(
                queue,
                "_get_resolved_torrent",
                side_effect=RuntimeError("resolution failure"),
            ),
            self.assertRaisesRegex(RuntimeError, "resolution failure"),
        ):
            await queue._worker()

        await queue.queue.join()

    async def test_concurrent_resolutions_share_unexpected_failure(self):
        queue = torrent_manager.AddTorrentQueue(max_concurrent=1)

        async def fail_resolution(_magnet_url):
            await asyncio.sleep(0)
            raise RuntimeError("resolution failure")

        with patch.object(
            torrent_manager,
            "get_torrent_from_magnet",
            side_effect=fail_resolution,
        ) as resolve:
            results = await asyncio.gather(
                queue._get_resolved_torrent(
                    "a" * 40, f"magnet:?xt=urn:btih:{'a' * 40}"
                ),
                queue._get_resolved_torrent(
                    "a" * 40, f"magnet:?xt=urn:btih:{'a' * 40}"
                ),
                return_exceptions=True,
            )

        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(
            [str(result) for result in results],
            ["resolution failure", "resolution failure"],
        )
        self.assertTrue(all(isinstance(result, RuntimeError) for result in results))

    async def test_broadcast_worker_propagates_unexpected_failure(self):
        queue = torrent_manager.TorrentUpdateQueue()
        backend = Mock(spec=CometNetService)
        backend.broadcast_torrents = AsyncMock(
            side_effect=RuntimeError("broadcast failure")
        )
        await queue._broadcast_queue.put((backend, [object()]))

        with self.assertRaisesRegex(RuntimeError, "broadcast failure"):
            await queue._process_broadcast_queue()

        backend.broadcast_torrents.assert_awaited_once()
        await queue._broadcast_queue.join()

    async def test_stop_propagates_completed_worker_failure(self):
        queue = torrent_manager.TorrentUpdateQueue()

        async def fail():
            raise RuntimeError("worker failure")

        queue._tasks["persistence"] = asyncio.create_task(fail())
        await asyncio.sleep(0)

        with self.assertRaisesRegex(RuntimeError, "worker failure"):
            await queue.stop()


if __name__ == "__main__":
    unittest.main()
