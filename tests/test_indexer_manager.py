import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from unittest.mock import patch

from comet.services.indexer_manager import (
    IndexerManager,
    _active_jackett_ids,
    _active_prowlarr_ids,
)


class _ResponseContext:
    def __init__(self, status, payload=None, error=None):
        self.status = status
        self.payload = payload
        self.error = error
        self.exited = False
        self.json_called = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.exited = True

    async def json(self):
        self.json_called = True
        if self.error is not None:
            raise self.error
        return self.payload


class _Session:
    def __init__(self, response):
        self.response = response

    def get(self, url, **kwargs):
        del url, kwargs
        return self.response


class IndexerManagerTests(unittest.IsolatedAsyncioTestCase):
    def test_jackett_active_ids_isolate_malformed_entries(self):
        root = ET.fromstring(
            """
            <indexers>
                <indexer><title>missing id</title></indexer>
                <indexer id="empty-title"><title /></indexer>
                <indexer id="wanted"><title>Wanted Name</title></indexer>
            </indexers>
            """
        )

        self.assertEqual(_active_jackett_ids(root, ["wanted name"]), ["wanted"])

    def test_prowlarr_active_ids_reject_invalid_health_without_losing_siblings(self):
        now = datetime(2026, 7, 22, tzinfo=timezone.utc)
        indexers = [
            None,
            {"id": None, "enable": True, "protocol": "torrent"},
            {"id": 1, "enable": True, "protocol": "torrent", "name": "Healthy"},
            {"id": 2, "enable": True, "protocol": "torrent", "name": "Disabled"},
            {"id": 3, "enable": True, "protocol": "torrent", "name": "Bad Date"},
            {"id": 4, "enable": True, "protocol": "torrent", "name": "Later"},
        ]
        statuses = [
            None,
            {"indexerId": 2, "disabledTill": "2026-07-23T00:00:00Z"},
            {"indexerId": 3, "disabledTill": "not-a-date"},
        ]

        self.assertEqual(
            _active_prowlarr_ids(indexers, statuses, [], now),
            ["1", "4"],
        )

    async def test_prowlarr_response_closes_without_reading_error_body(self):
        response = _ResponseContext(503)
        manager = IndexerManager()

        with patch("comet.services.indexer_manager.settings.PROWLARR_URL", "http://p"):
            result = await manager._fetch_prowlarr_json(
                _Session(response), "/api/v1/indexer", {}
            )

        self.assertEqual(result, (503, None))
        self.assertFalse(response.json_called)
        self.assertTrue(response.exited)

    async def test_prowlarr_response_closes_when_json_decode_fails(self):
        response = _ResponseContext(200, error=ValueError("invalid JSON"))
        manager = IndexerManager()

        with (
            patch("comet.services.indexer_manager.settings.PROWLARR_URL", "http://p"),
            self.assertRaisesRegex(ValueError, "invalid JSON"),
        ):
            await manager._fetch_prowlarr_json(
                _Session(response), "/api/v1/indexer", {}
            )

        self.assertTrue(response.json_called)
        self.assertTrue(response.exited)
