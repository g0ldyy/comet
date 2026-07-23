import asyncio
import unittest
from unittest.mock import patch

from comet.api.endpoints.stream import (
    check_multi_service_availability,
    get_and_cache_multi_service_availability,
)
from comet.debrid.exceptions import DebridAuthError


class _DebridService:
    def __init__(self, service, api_key, ip):
        self.service = service

    async def get_and_cache_availability(self, *args, **kwargs):
        info_hash = "a" * 40
        if self.service == "first":
            await asyncio.sleep(0.01)
            title = "First.mkv"
        else:
            title = "Second.mkv"
        return {info_hash}, {info_hash: {"title": title}}

    async def check_existing_availability(self, *args, **kwargs):
        return await self.get_and_cache_availability(*args, **kwargs)


class _CredentialDebridService:
    attempts = []

    def __init__(self, service, api_key, ip):
        del ip
        self.service = service
        self.api_key = api_key

    async def get_and_cache_availability(self, *args, **kwargs):
        del args, kwargs
        self.attempts.append((self.service, self.api_key))
        if self.api_key == "invalid":
            raise DebridAuthError(self.service)
        info_hash = "a" * 40
        return {info_hash}, {info_hash: {"title": "Valid account.mkv"}}


class MultiServiceDebridTests(unittest.IsolatedAsyncioTestCase):
    async def test_cached_enrichment_uses_configured_order_not_completion_order(self):
        info_hash = "a" * 40
        torrents = {info_hash: {"title": "Original.mkv"}}
        entries = [
            {"service": "first", "apiKey": "one"},
            {"service": "second", "apiKey": "two"},
        ]

        with patch(
            "comet.api.endpoints.stream.DebridService",
            new=_DebridService,
        ):
            status = await check_multi_service_availability(
                entries, torrents, None, None
            )

        self.assertEqual(torrents[info_hash]["title"], "First.mkv")
        self.assertTrue(status[info_hash]["first"])
        self.assertTrue(status[info_hash]["second"])

    async def test_enrichment_uses_configured_order_not_completion_order(self):
        info_hash = "a" * 40
        torrents = {
            info_hash: {
                "title": "Original.mkv",
                "seeders": 1,
                "tracker": "tracker",
                "sources": [],
            }
        }
        entries = [
            {"service": "first", "apiKey": "one"},
            {"service": "second", "apiKey": "two"},
        ]

        with patch(
            "comet.api.endpoints.stream.DebridService",
            new=_DebridService,
        ):
            status, errors = await get_and_cache_multi_service_availability(
                None,
                entries,
                torrents,
                "tt123",
                "tt123",
                None,
                None,
                "",
            )

        self.assertFalse(errors)
        self.assertEqual(torrents[info_hash]["title"], "First.mkv")
        self.assertTrue(status[info_hash]["first"])
        self.assertTrue(status[info_hash]["second"])

    async def test_duplicate_service_tries_next_account_after_auth_failure(self):
        info_hash = "a" * 40
        torrents = {
            info_hash: {
                "title": "Original.mkv",
                "seeders": 1,
                "tracker": "tracker",
                "sources": [],
            }
        }
        entries = [
            {"service": "realdebrid", "apiKey": "invalid"},
            {"service": "realdebrid", "apiKey": "invalid"},
            {"service": "realdebrid", "apiKey": "valid"},
        ]
        _CredentialDebridService.attempts = []

        with patch(
            "comet.api.endpoints.stream.DebridService",
            new=_CredentialDebridService,
        ):
            status, errors = await get_and_cache_multi_service_availability(
                None,
                entries,
                torrents,
                "tt123",
                "tt123",
                None,
                None,
                "",
            )

        self.assertFalse(errors)
        self.assertEqual(
            _CredentialDebridService.attempts,
            [("realdebrid", "invalid"), ("realdebrid", "valid")],
        )
        self.assertEqual(torrents[info_hash]["title"], "Valid account.mkv")
        self.assertTrue(status[info_hash]["realdebrid"])
