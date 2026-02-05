import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from comet.core.database import IS_SQLITE, ON_CONFLICT_DO_NOTHING, database
from comet.core.models import settings
from comet.services.lock import DistributedLock
from comet.utils.media_ids import normalize_cache_media_ids


class CacheState(Enum):
    """Represents the state of the cache for a given media_id."""

    FRESH = "fresh"  # Fresh cached torrents exist, no scraping needed
    STALE = "stale"  # Cached torrents exist but are old, background refresh needed
    EMPTY = "empty"  # No cached torrents, need to scrape
    FIRST_SEARCH = "first_search"  # Has cache but first time this media was searched


class ScrapeDecision(Enum):
    """What action to take based on cache state."""

    USE_CACHE = "use_cache"  # Just use cached results, no scraping
    SCRAPE_FOREGROUND = "scrape_foreground"  # Scrape now, block until done
    SCRAPE_BACKGROUND = "scrape_background"  # Return cache now, scrape in background
    WAIT_FOR_OTHER = "wait_for_other"  # Another instance is scraping, tell user to wait


@dataclass
class CacheCheckResult:
    """Result of checking cache state."""

    state: CacheState
    decision: ScrapeDecision
    has_cached_torrents: bool
    fresh_torrent_count: int
    is_first_search: bool
    lock: Optional[DistributedLock] = field(default=None, repr=False)
    lock_acquired: bool = False

    @property
    def should_show_first_search_message(self) -> bool:
        """Whether to show 'first search' message to user."""
        return self.is_first_search and self.has_cached_torrents

    @property
    def should_scrape_now(self) -> bool:
        """Whether scraping should happen in the current request."""
        return self.decision == ScrapeDecision.SCRAPE_FOREGROUND

    @property
    def should_scrape_background(self) -> bool:
        """Whether to start a background scrape task."""
        return self.decision == ScrapeDecision.SCRAPE_BACKGROUND

    @property
    def should_return_wait_message(self) -> bool:
        """Whether to return a 'please wait' message to user."""
        return self.decision == ScrapeDecision.WAIT_FOR_OTHER


class CacheStateManager:
    """
    Manages cache state checking and lock acquisition for a media search.

    This class provides a single, consistent way to determine:
    1. What's the current cache state (fresh, stale, empty, first search)
    2. What action to take (use cache, scrape now, scrape background, wait)
    3. Whether a lock was acquired and needs to be released
    """

    def __init__(
        self,
        media_id: str,
        media_only_id: str,
        season: Optional[int],
        episode: Optional[int],
        is_kitsu: bool = False,
        search_episode: Optional[int] = None,
        search_season: Optional[int] = None,
        cache_media_ids: list[tuple[str, bool]] | None = None,
    ):
        self.media_id = media_id
        self.media_only_id = media_only_id
        self.season = season
        self.episode = episode
        self.is_kitsu = is_kitsu
        self.search_season = search_season if search_season is not None else season
        self.search_episode = search_episode if search_episode is not None else episode

        self._lock: Optional[DistributedLock] = None
        self._lock_acquired: bool = False

        self.cache_media_ids = normalize_cache_media_ids(
            self.media_only_id, self.is_kitsu, cache_media_ids
        )

    async def get_fresh_torrent_count(self) -> int:
        """
        Check for at least one 'fresh' cached torrent based on LIVE_TORRENT_CACHE_TTL.

        Returns 1 if any fresh torrent exists, otherwise 0.
        If TTL is -1 (never expires), checks for any cached torrent.
        """
        for cache_media_id, cache_is_kitsu in self.cache_media_ids:
            if cache_is_kitsu:
                base_query = """
                    SELECT 1
                    FROM torrents
                    WHERE media_id = :media_id
                    AND (episode IS NULL OR episode = CAST(:episode as INTEGER))
                """
                params = {
                    "media_id": cache_media_id,
                    "episode": self.search_episode,
                }
            else:
                base_query = """
                    SELECT 1
                    FROM torrents
                    WHERE media_id = :media_id
                    AND ((season IS NOT NULL AND season = CAST(:season as INTEGER)) 
                         OR (season IS NULL AND CAST(:season as INTEGER) IS NULL))
                    AND (episode IS NULL OR episode = CAST(:episode as INTEGER))
                """
                params = {
                    "media_id": cache_media_id,
                    "season": self.search_season,
                    "episode": self.search_episode,
                }

            if settings.LIVE_TORRENT_CACHE_TTL >= 0:
                min_timestamp = time.time() - settings.LIVE_TORRENT_CACHE_TTL
                ttl_condition = " AND timestamp >= :min_timestamp"
                params["min_timestamp"] = min_timestamp
                query = base_query + ttl_condition
            else:
                query = base_query

            result = await database.fetch_one(query + " LIMIT 1", params)
            if result:
                return 1

        return 0

    async def check_is_first_search(self) -> bool:
        """
        Check if this is the first search for this media_id.
        """
        params = {"media_id": self.media_id, "timestamp": time.time()}

        try:
            if IS_SQLITE:
                try:
                    await database.execute(
                        "INSERT INTO first_searches VALUES (:media_id, :timestamp)",
                        params,
                    )
                    return True
                except Exception:
                    return False

            inserted = await database.fetch_val(
                f"""
                INSERT INTO first_searches (media_id, timestamp)
                VALUES (:media_id, :timestamp)
                {ON_CONFLICT_DO_NOTHING}
                RETURNING 1
                """,
                params,
                force_primary=True,
            )
            return inserted == 1
        except Exception:
            return False

    async def _try_acquire_lock(self) -> bool:
        """Attempt to acquire the distributed lock."""
        if self._lock is None:
            self._lock = DistributedLock(self.media_id)

        self._lock_acquired = await self._lock.acquire()
        return self._lock_acquired

    async def try_acquire_lock(self) -> bool:
        """Public wrapper to acquire the distributed lock if needed."""
        return await self._try_acquire_lock()

    async def release_lock(self) -> None:
        """Release the lock if it was acquired."""
        if self._lock_acquired and self._lock:
            await self._lock.release()
            self._lock_acquired = False

    def _determine_state(
        self,
        fresh_count: int,
        torrent_count: int,
        is_first: bool,
    ) -> CacheState:
        """Determine the cache state based on counts and first search flag."""
        has_cached = torrent_count > 0
        has_fresh = fresh_count > 0

        if not has_cached:
            return CacheState.EMPTY

        if is_first:
            return CacheState.FIRST_SEARCH

        if not has_fresh:
            return CacheState.STALE

        return CacheState.FRESH

    def _determine_decision(
        self,
        state: CacheState,
        lock_acquired: bool,
    ) -> ScrapeDecision:
        """
        Determine what action to take based on state and lock status.

        Decision matrix:
        - FRESH: Always use cache, no scraping needed
        - STALE: Use cache now, refresh in background
        - FIRST_SEARCH: Use cache now, enrich in background
        - EMPTY + lock acquired: Scrape now (foreground)
        - EMPTY + no lock: Another instance is scraping, wait
        """
        if state == CacheState.FRESH:
            return ScrapeDecision.USE_CACHE

        if state in (CacheState.STALE, CacheState.FIRST_SEARCH):
            return ScrapeDecision.SCRAPE_BACKGROUND

        # state == CacheState.EMPTY
        if lock_acquired:
            return ScrapeDecision.SCRAPE_FOREGROUND
        else:
            return ScrapeDecision.WAIT_FOR_OTHER

    async def check_and_decide(self, torrent_count: int) -> CacheCheckResult:
        """
        Main entry point: check cache state and decide what action to take.

        Args:
            torrent_count: Number of cached torrents from TorrentManager.get_cached_torrents()

        Returns:
            CacheCheckResult with state, decision, and lock info
        """
        fresh_count = await self.get_fresh_torrent_count()
        is_first = await self.check_is_first_search()

        state = self._determine_state(fresh_count, torrent_count, is_first)

        lock_acquired = False
        if state in (CacheState.EMPTY, CacheState.STALE, CacheState.FIRST_SEARCH):
            # For STALE/FIRST_SEARCH, background task will acquire its own lock
            if state == CacheState.EMPTY:
                lock_acquired = await self._try_acquire_lock()

        decision = self._determine_decision(state, lock_acquired)

        return CacheCheckResult(
            state=state,
            decision=decision,
            has_cached_torrents=torrent_count > 0,
            fresh_torrent_count=fresh_count,
            is_first_search=is_first,
            lock=self._lock,
            lock_acquired=lock_acquired,
        )

    @property
    def lock(self) -> Optional[DistributedLock]:
        """Get the lock instance (for background task to use)."""
        return self._lock

    @property
    def has_lock(self) -> bool:
        """Check if lock was acquired."""
        return self._lock_acquired
