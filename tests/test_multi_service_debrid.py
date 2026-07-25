import asyncio
import unittest
from typing import ClassVar
from unittest.mock import patch

from comet.services.media_search import (
    check_multi_service_availability,
    get_and_cache_multi_service_availability,
    select_debrid_refresh_hashes,
)
from comet.debrid.exceptions import DebridAuthError
from comet.utils.parsing import MediaScope


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
    attempts: ClassVar[list] = []

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


class _SelectiveDebridService:
    checked_hashes: ClassVar[dict] = {}

    def __init__(self, service, api_key, ip):
        del api_key, ip
        self.service = service

    async def get_and_cache_availability(
        self,
        session,
        info_hashes,
        *args,
        **kwargs,
    ):
        del session, args, kwargs
        self.checked_hashes[self.service] = info_hashes
        return set(), {}


class MultiServiceDebridTests(unittest.IsolatedAsyncioTestCase):
    def test_partial_cache_refresh_selects_only_new_hashes_at_zero_ratio(self):
        old_hash = "a" * 40
        new_hash = "b" * 40

        with patch(
            "comet.services.media_search.settings.DEBRID_CACHE_CHECK_RATIO",
            0.0,
        ):
            selected = select_debrid_refresh_hashes(
                {old_hash, new_hash},
                {old_hash},
                {old_hash: {"torbox": True}},
                had_cached_torrents=True,
                use_account_scrape=False,
            )

        self.assertEqual(selected, {new_hash})

    async def test_fresh_check_skips_service_specific_positive_cache_hits(self):
        first_hash = "a" * 40
        second_hash = "b" * 40
        torrents = {
            info_hash: {
                "title": f"{info_hash}.mkv",
                "seeders": 1,
                "tracker": "tracker",
                "sources": [],
            }
            for info_hash in (first_hash, second_hash)
        }
        entries = [
            {"service": "first", "apiKey": "one"},
            {"service": "second", "apiKey": "two"},
        ]
        _SelectiveDebridService.checked_hashes = {}

        with patch(
            "comet.services.media_search.DebridService",
            new=_SelectiveDebridService,
        ):
            await get_and_cache_multi_service_availability(
                None,
                entries,
                torrents,
                "tt123:1",
                "tt123",
                1,
                None,
                MediaScope.SEASON,
                "",
                known_cache_status={
                    first_hash: {"first": True},
                    second_hash: {"second": True},
                },
            )

        self.assertEqual(_SelectiveDebridService.checked_hashes["first"], [second_hash])
        self.assertEqual(_SelectiveDebridService.checked_hashes["second"], [first_hash])

    async def test_cached_enrichment_uses_configured_order_not_completion_order(self):
        info_hash = "a" * 40
        torrents = {info_hash: {"title": "Original.mkv"}}
        entries = [
            {"service": "first", "apiKey": "one"},
            {"service": "second", "apiKey": "two"},
        ]

        with patch(
            "comet.services.media_search.DebridService",
            new=_DebridService,
        ):
            status = await check_multi_service_availability(
                entries, torrents, None, None, MediaScope.MOVIE
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
            "comet.services.media_search.DebridService",
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
                MediaScope.MOVIE,
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
            "comet.services.media_search.DebridService",
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
                MediaScope.MOVIE,
                "",
            )

        self.assertFalse(errors)
        self.assertEqual(
            _CredentialDebridService.attempts,
            [("realdebrid", "invalid"), ("realdebrid", "valid")],
        )
        self.assertEqual(torrents[info_hash]["title"], "Valid account.mkv")
        self.assertTrue(status[info_hash]["realdebrid"])
