import asyncio
from dataclasses import dataclass, field

from comet.core.scrape import ScrapeContext
from comet.core.sources import ReleaseCandidate, ReleaseScope


@dataclass(frozen=True)
class MediaQuery:
    media_id: str
    media_type: str
    season: int | None = None
    episode: int | None = None
    title_aliases: tuple[str, ...] = ()
    year: int | None = None
    air_date: str | None = None
    absolute_episode: int | None = None
    requested_language: str | None = None
    search_scope: str | None = None
    request_media_id: str | None = None
    title: str | None = None
    year_end: int | None = None
    search_titles: tuple[str, ...] = ()
    normalization_fingerprint: str | None = None

    @property
    def scope(self) -> ReleaseScope:
        if self.search_scope is not None:
            try:
                return ReleaseScope(self.search_scope)
            except ValueError:
                raise ValueError("media query scope is invalid") from None
        if self.media_type == "movie":
            return ReleaseScope.MOVIE
        if self.media_type != "series":
            raise ValueError("media query scope is invalid")
        if self.air_date is not None:
            return ReleaseScope.DAILY_EPISODE
        if self.episode is not None:
            return ReleaseScope.EPISODE
        if self.season is not None:
            return ReleaseScope.SEASON_PACK
        return ReleaseScope.SERIES_PACK


@dataclass(frozen=True)
class DiscoveryContext:
    branches: frozenset[str]
    account_partition: bytes | None = None
    configuration_id: str | None = None
    hard_deadline: float | None = None
    cancellation: asyncio.Event | None = None
    trace_id: str | None = None
    work_class: ScrapeContext = ScrapeContext.LIVE

    def cancelled(self) -> bool:
        return self.cancellation is not None and self.cancellation.is_set()


@dataclass(frozen=True)
class DiscoveryBatch:
    candidates: tuple[ReleaseCandidate, ...] = ()
    diagnostics: tuple[str, ...] = ()
    coverage: frozenset[str] = field(default_factory=frozenset)
    inflight: bool = False
