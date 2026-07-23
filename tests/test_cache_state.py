import asyncio
import unittest
from unittest.mock import patch

from comet.services.cache_state import CacheState, CacheStateManager, ScrapeDecision


class CacheStateManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_independent_cache_checks_run_concurrently(self):
        manager = CacheStateManager(
            media_id="tt123",
            media_only_id="tt123",
            season=None,
            episode=None,
        )
        fresh_started = asyncio.Event()
        demand_started = asyncio.Event()

        async def fresh_count():
            fresh_started.set()
            await demand_started.wait()
            return 1

        async def first_search():
            demand_started.set()
            await fresh_started.wait()
            return False

        with (
            patch.object(manager, "get_fresh_torrent_count", new=fresh_count),
            patch.object(manager, "check_is_first_search", new=first_search),
        ):
            result = await asyncio.wait_for(manager.check_and_decide(1), timeout=1)

        self.assertEqual(result.state, CacheState.FRESH)
        self.assertEqual(result.decision, ScrapeDecision.USE_CACHE)
        self.assertEqual(result.fresh_torrent_count, 1)
