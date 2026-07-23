import time
from dataclasses import dataclass
from enum import Enum

from comet.core.database import database
from comet.core.logger import logger
from comet.core.models import settings
from comet.services.lock import DistributedLock


async def _upsert_scope_demand(
    media_id: str,
    *,
    scraped: bool,
) -> float | None:
    current_time = time.time()
    try:
        row = await database.fetch_one(
            """
            INSERT INTO media_demand (
                media_id,
                first_seen_at,
                last_seen_at,
                last_scraped_at
            )
            VALUES (
                :media_id,
                :current_time,
                :current_time,
                :last_scraped_at
            )
            ON CONFLICT (media_id) DO UPDATE SET
                last_seen_at = EXCLUDED.last_seen_at,
                last_scraped_at = COALESCE(
                    EXCLUDED.last_scraped_at,
                    media_demand.last_scraped_at
                )
            RETURNING last_scraped_at
            """,
            {
                "media_id": media_id,
                "current_time": current_time,
                "last_scraped_at": current_time if scraped else None,
            },
            force_primary=True,
        )
    except Exception as exc:
        logger.opt(exception=True).debug(
            f"Failed to update scope demand for {media_id}: {exc}",
        )
        return None

    return row["last_scraped_at"] if row is not None else None


async def mark_scope_scraped(media_id: str) -> None:
    """Record successful completion for one exact request scope."""
    await _upsert_scope_demand(media_id, scraped=True)


class CacheState(Enum):
    FRESH = "fresh"
    STALE = "stale"
    EMPTY = "empty"
    FIRST_SEARCH = "first_search"


class ScrapeDecision(Enum):
    USE_CACHE = "use_cache"
    SCRAPE_FOREGROUND = "scrape_foreground"
    SCRAPE_BACKGROUND = "scrape_background"
    WAIT_FOR_OTHER = "wait_for_other"


@dataclass(frozen=True, slots=True)
class CacheCheckResult:
    state: CacheState
    decision: ScrapeDecision
    has_cached_torrents: bool
    scope_scraped_at: float | None
    lock_acquired: bool = False

    @property
    def should_scrape_now(self) -> bool:
        return self.decision is ScrapeDecision.SCRAPE_FOREGROUND

    @property
    def should_scrape_background(self) -> bool:
        return self.decision is ScrapeDecision.SCRAPE_BACKGROUND

    @property
    def should_return_wait_message(self) -> bool:
        return self.decision is ScrapeDecision.WAIT_FOR_OTHER


class CacheStateManager:
    """Tracks reusable results separately from exact-scope scrape coverage."""

    def __init__(self, media_id: str):
        self.media_id = media_id
        self._lock: DistributedLock | None = None
        self._lock_acquired = False

    async def register_demand(self) -> float | None:
        """Record demand and return the last scrape for this exact media ID."""
        return await _upsert_scope_demand(self.media_id, scraped=False)

    @staticmethod
    def _is_scope_fresh(last_scraped_at: float | None) -> bool:
        if last_scraped_at is None:
            return False
        if settings.LIVE_TORRENT_CACHE_TTL < 0:
            return True
        return last_scraped_at >= time.time() - settings.LIVE_TORRENT_CACHE_TTL

    @classmethod
    def _determine_state(
        cls,
        torrent_count: int,
        last_scraped_at: float | None,
    ) -> CacheState:
        has_cached_torrents = torrent_count > 0

        if last_scraped_at is None:
            return CacheState.FIRST_SEARCH if has_cached_torrents else CacheState.EMPTY
        if cls._is_scope_fresh(last_scraped_at):
            return CacheState.FRESH
        return CacheState.STALE if has_cached_torrents else CacheState.EMPTY

    @staticmethod
    def _determine_decision(
        state: CacheState,
        lock_acquired: bool,
    ) -> ScrapeDecision:
        if state is CacheState.FRESH:
            return ScrapeDecision.USE_CACHE
        if state is CacheState.STALE:
            return ScrapeDecision.SCRAPE_BACKGROUND
        if lock_acquired:
            return ScrapeDecision.SCRAPE_FOREGROUND
        return ScrapeDecision.WAIT_FOR_OTHER

    async def _try_acquire_lock(self) -> bool:
        if self._lock is None:
            self._lock = DistributedLock(self.media_id)
        self._lock_acquired = await self._lock.acquire()
        return self._lock_acquired

    async def try_acquire_lock(self) -> bool:
        return await self._try_acquire_lock()

    async def release_lock(self) -> None:
        if self._lock_acquired and self._lock is not None:
            await self._lock.release()
            self._lock_acquired = False

    async def check_and_decide(self, torrent_count: int) -> CacheCheckResult:
        last_scraped_at = await self.register_demand()
        state = self._determine_state(torrent_count, last_scraped_at)
        lock_acquired = False
        if state in (CacheState.EMPTY, CacheState.FIRST_SEARCH):
            lock_acquired = await self._try_acquire_lock()

        return CacheCheckResult(
            state=state,
            decision=self._determine_decision(state, lock_acquired),
            has_cached_torrents=torrent_count > 0,
            scope_scraped_at=last_scraped_at,
            lock_acquired=lock_acquired,
        )
