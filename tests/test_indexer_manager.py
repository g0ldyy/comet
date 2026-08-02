import json
import unittest
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from comet.services.indexer_manager import (
    IndexerManager,
    InvalidIndexerResponse,
    _active_jackett_ids,
    _active_prowlarr_ids,
    read_indexer_json,
    read_indexer_xml,
)


class _ResponseContext:
    def __init__(self, status, payload=None, error=None, *, body=None, headers=None):
        self.status = status
        self.payload = payload
        self.error = error
        self.exited = False
        self.body_read = False
        self.headers = headers or {
            "Content-Type": "application/json",
            "Content-Encoding": "identity",
        }
        self.content = self
        self._body = (
            body
            if body is not None
            else (
                b"invalid JSON" if error is not None else json.dumps(payload).encode()
            )
        )
        self._offset = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.exited = True

    async def read(self, size):
        self.body_read = True
        chunk = self._body[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


class _Session:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    def get(self, url, **kwargs):
        del url
        self.kwargs = kwargs
        return self.response


class IndexerManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_jackett_internal_failure_is_not_swallowed(self):
        manager = IndexerManager()
        manager.get_session = AsyncMock(side_effect=AssertionError("implementation"))

        with (
            patch.multiple(
                "comet.services.indexer_manager.settings",
                SCRAPE_JACKETT=True,
                JACKETT_URL="http://jackett",
                JACKETT_API_KEY="secret",
            ),
            self.assertRaisesRegex(AssertionError, "implementation"),
        ):
            await manager.update_jackett()

        self.assertTrue(manager.jackett_initialized.is_set())

    async def test_prowlarr_internal_failure_is_not_swallowed(self):
        manager = IndexerManager()
        manager.get_session = AsyncMock(side_effect=AssertionError("implementation"))

        with (
            patch.multiple(
                "comet.services.indexer_manager.settings",
                SCRAPE_PROWLARR=True,
                PROWLARR_URL="http://prowlarr",
                PROWLARR_API_KEY="secret",
            ),
            self.assertRaisesRegex(AssertionError, "implementation"),
        ):
            await manager.update_prowlarr()

        self.assertTrue(manager.prowlarr_initialized.is_set())

    def test_jackett_active_ids_match_configured_names_after_normalization(self):
        root = ET.fromstring(
            """
            <indexers>
                <indexer><title>missing id</title></indexer>
                <indexer id="empty-title"><title /></indexer>
                <indexer id="wanted"><title>Wanted Name</title></indexer>
            </indexers>
            """
        )

        self.assertEqual(_active_jackett_ids(root, ["wantedname"]), ["wanted"])

    def test_jackett_active_ids_are_unique_and_fanout_bounded(self):
        root = ET.fromstring(
            "<indexers>"
            '<indexer id="duplicate" />'
            '<indexer id="duplicate" />'
            '<indexer id="bad&#10;id" />'
            + "".join(f'<indexer id="indexer-{index}" />' for index in range(70))
            + "</indexers>"
        )

        active = _active_jackett_ids(root, [])

        self.assertEqual(active[0], "duplicate")
        self.assertEqual(len(active), 64)
        self.assertEqual(len(active), len(set(active)))

    def test_prowlarr_active_ids_ignore_unusable_optional_health(self):
        now = datetime(2026, 7, 22, tzinfo=UTC)
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
            ["1", "3", "4"],
        )

    def test_prowlarr_active_ids_match_normalized_names(self):
        now = datetime(2026, 7, 22, tzinfo=UTC)

        self.assertEqual(
            _active_prowlarr_ids(
                [
                    {
                        "id": 1,
                        "enable": True,
                        "protocol": "torrent",
                        "name": "Wanted Name",
                    }
                ],
                [],
                ["wantedname"],
                now,
            ),
            ["1"],
        )

    def test_prowlarr_invalid_collection_does_not_look_like_no_indexers(self):
        with self.assertRaisesRegex(
            InvalidIndexerResponse,
            "invalid Prowlarr indexer response",
        ):
            _active_prowlarr_ids({}, [], [], datetime.now(UTC))

    def test_prowlarr_active_ids_use_latest_status_and_bound_fanout(self):
        now = datetime(2026, 7, 22, tzinfo=UTC)
        indexers = [
            {
                "id": index,
                "enable": True,
                "protocol": "torrent",
            }
            for index in range(1, 70)
        ]
        statuses = [
            {"indexerId": 1, "disabledTill": "2027-01-01T00:00:00Z"},
            {"indexerId": 1},
        ]

        active = _active_prowlarr_ids(indexers, statuses, [], now)

        self.assertIn("1", active)
        self.assertEqual(len(active), 64)
        self.assertEqual(len(active), len(set(active)))

    async def test_prowlarr_response_closes_without_reading_error_body(self):
        response = _ResponseContext(503)
        manager = IndexerManager()

        with patch("comet.services.indexer_manager.settings.PROWLARR_URL", "http://p"):
            result = await manager._fetch_prowlarr_json(
                _Session(response), "/api/v1/indexer", {}
            )

        self.assertEqual(result, (503, None))
        self.assertFalse(response.body_read)
        self.assertTrue(response.exited)

    async def test_prowlarr_response_closes_when_json_decode_fails(self):
        response = _ResponseContext(200, error=ValueError("invalid JSON"))
        manager = IndexerManager()

        with (
            patch("comet.services.indexer_manager.settings.PROWLARR_URL", "http://p"),
            self.assertRaisesRegex(ValueError, "invalid indexer JSON"),
        ):
            await manager._fetch_prowlarr_json(
                _Session(response), "/api/v1/indexer", {}
            )

        self.assertTrue(response.body_read)
        self.assertTrue(response.exited)

    async def test_prowlarr_requests_are_nonredirecting_identity_framed(self):
        response = _ResponseContext(200, payload=[])
        session = _Session(response)
        manager = IndexerManager()

        with patch("comet.services.indexer_manager.settings.PROWLARR_URL", "http://p"):
            result = await manager._fetch_prowlarr_json(
                session,
                "/api/v1/indexer",
                {"X-Api-Key": "secret"},
            )

        self.assertEqual(result, (200, []))
        self.assertFalse(session.kwargs["allow_redirects"])
        self.assertEqual(session.kwargs["headers"]["Accept-Encoding"], "identity")
        self.assertEqual(session.kwargs["headers"]["X-Api-Key"], "secret")

    async def test_indexer_decoders_bound_observed_bytes_and_reject_active_xml(self):
        encoded = _ResponseContext(
            200,
            payload={},
            headers={
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
            },
        )
        self.assertEqual(await read_indexer_json(encoded), {})
        self.assertTrue(encoded.body_read)

        oversized = _ResponseContext(
            200,
            payload={},
            headers={
                "Content-Type": "application/json",
                "Content-Encoding": "identity",
                "Content-Length": str(2 * 1024 * 1024 + 1),
            },
        )
        self.assertEqual(await read_indexer_json(oversized), {})
        self.assertTrue(oversized.body_read)

        active_xml = _ResponseContext(
            200,
            body=b'<!DOCTYPE x [<!ENTITY y "value">]><indexers />',
            headers={
                "Content-Type": "application/xml",
                "Content-Encoding": "identity",
            },
        )
        with self.assertRaisesRegex(ValueError, "invalid indexer XML"):
            await read_indexer_xml(active_xml)

        inert_doctype = _ResponseContext(
            200,
            body=b'<!DOCTYPE indexers SYSTEM "indexers.dtd"><indexers />',
        )
        self.assertEqual((await read_indexer_xml(inert_doctype)).tag, "indexers")
