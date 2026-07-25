import asyncio
import unittest
from unittest.mock import patch

import aiohttp

from comet.cometnet.relay import CometNetRelay


class FakeSession:
    def __init__(self, response=None):
        self.closed = False
        self.response = response

    def get(self, *args, **kwargs):
        return self.response

    def post(self, *args, **kwargs):
        return self.response

    async def close(self):
        self.closed = True


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status = status
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return False

    async def json(self):
        return self.payload


class FailingResponse:
    async def __aenter__(self):
        raise aiohttp.ClientConnectionError("secret credential")

    async def __aexit__(self, exc_type, exc_value, traceback):
        return False


class CometNetRelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_pool_transport_error_is_redacted(self):
        relay = CometNetRelay("http://user:password@relay")
        relay._running = True
        relay._session = FakeSession(FailingResponse())

        with (
            patch("comet.cometnet.relay.logger.warning") as warning,
            self.assertRaisesRegex(RuntimeError, "Failed to connect to standalone"),
        ):
            await relay._pool_request("POST", "/pools", {})

        rendered = " ".join(
            str(value) for call in warning.call_args_list for value in call.args
        )
        self.assertNotIn("password", rendered)
        self.assertNotIn("secret credential", rendered)

    async def test_cancelled_flush_restores_batch_in_front(self):
        relay = CometNetRelay("http://relay")
        relay._session = FakeSession()
        relay._batch = [
            {"info_hash": "old-1"},
            {"info_hash": "old-2"},
        ]
        sending = asyncio.Event()

        async def block_send(torrents):
            sending.set()
            await asyncio.Event().wait()

        relay._send_batch = block_send
        task = asyncio.create_task(relay._flush_batch())
        await sending.wait()
        relay._batch.append({"info_hash": "new"})

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(
            [item["info_hash"] for item in relay._batch],
            ["old-1", "old-2", "new"],
        )

    async def test_waiting_producer_does_not_append_after_stop(self):
        relay = CometNetRelay("http://relay")
        relay._running = True
        await relay._batch_lock.acquire()
        task = asyncio.create_task(relay.relay_torrent("a" * 40, "title", 1))
        await asyncio.sleep(0)

        relay._running = False
        relay._batch_lock.release()

        self.assertFalse(await task)
        self.assertEqual(relay._batch, [])

    async def test_threshold_flushes_are_serialized_by_owned_worker(self):
        relay = CometNetRelay("http://relay")
        relay._running = True
        session = FakeSession()
        relay._session = session
        relay.batch_size = 2
        relay.batch_interval = 60
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        second_finished = asyncio.Event()
        active = 0
        peak = 0
        batches = []

        async def send_batch(torrents):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                batches.append([item["info_hash"] for item in torrents])
                if len(batches) == 1:
                    first_started.set()
                    await release_first.wait()
                else:
                    second_finished.set()
            finally:
                active -= 1
            return len(torrents)

        relay._send_batch = send_batch
        relay._batch_task = asyncio.create_task(relay._batch_flush_loop())
        for suffix in range(4):
            await relay.relay_torrent(f"{suffix:040x}", "title", 1)

        await first_started.wait()
        for suffix in range(4, 8):
            await relay.relay_torrent(f"{suffix:040x}", "title", 1)
        release_first.set()
        await second_finished.wait()
        await relay.stop()

        self.assertEqual(peak, 1)
        self.assertEqual(
            [item for batch in batches for item in batch],
            [f"{i:040x}" for i in range(8)],
        )
        self.assertTrue(session.closed)

    async def test_get_pools_requires_current_standalone_endpoint(self):
        pools = {"pools": {}, "memberships": [], "subscriptions": []}
        relay = CometNetRelay("http://relay")
        relay._running = True
        relay._session = FakeSession(FakeResponse(200, pools))

        self.assertEqual(await relay.get_pools(), pools)

        relay._session = FakeSession(FakeResponse(404))
        with self.assertRaisesRegex(ValueError, "Pool not found"):
            await relay.get_pools()

    async def test_get_pools_rejects_invalid_current_shape(self):
        relay = CometNetRelay("http://relay")
        relay._running = True
        relay._session = FakeSession(FakeResponse(200, {"pools": []}))

        with self.assertRaisesRegex(ValueError, "Invalid pools response"):
            await relay.get_pools()

    async def test_send_batch_validates_result_before_updating_counters(self):
        relay = CometNetRelay("http://relay")
        relay._session = FakeSession(
            FakeResponse(
                200,
                {
                    "status": "completed",
                    "queued": 1,
                    "errors": [{"info_hash": "b", "error": "invalid"}],
                    "total": 2,
                },
            )
        )

        self.assertEqual(await relay._send_batch([{"id": "a"}, {"id": "b"}]), 1)
        self.assertEqual(relay._total_relayed, 1)
        self.assertEqual(relay._total_errors, 1)

        relay._session = FakeSession(
            FakeResponse(
                200,
                {
                    "status": "completed",
                    "queued": True,
                    "errors": [],
                    "total": 2,
                },
            )
        )
        with self.assertRaisesRegex(ValueError, "Invalid relay batch response"):
            await relay._send_batch([{"id": "a"}, {"id": "b"}])

        self.assertEqual(relay._total_relayed, 1)
        self.assertEqual(relay._total_errors, 1)
