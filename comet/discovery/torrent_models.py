from typing import TypedDict

from pydantic import BaseModel

from comet.core.scrape import ScrapeContext
from comet.utils.languages import MAX_INDEXER_TITLES


class ScrapeRequest(BaseModel):
    media_type: str  # "movie" or "series"
    media_id: str  # Full ID (e.g., "tt1234567:1:1" or "kitsu:123")
    media_only_id: str  # Base ID (e.g., "tt1234567")
    title: str
    year: int | None = None
    year_end: int | None = None
    season: int | None = None
    episode: int | None = None
    context: ScrapeContext = ScrapeContext.LIVE
    search_titles: tuple[str, ...] = ()

    @property
    def query_titles(self) -> tuple[str, ...]:
        return (self.search_titles or (self.title,))[:MAX_INDEXER_TITLES]

    def scoped_query_titles(self) -> tuple[str, ...]:
        if self.media_type != "series" or self.season is None:
            return self.query_titles
        suffix = f" S{self.season:02d}"
        if self.episode is not None:
            suffix += f"E{self.episode:02d}"
        return tuple(f"{title}{suffix}" for title in self.query_titles)

    def title_queries(self, *, include_episode_variants: bool = False):
        queries = []
        for title in self.query_titles:
            queries.append(title)
            if (
                include_episode_variants
                and self.media_type == "series"
                and self.season is not None
            ):
                queries.append(f"{title} S{self.season:02d}")
                if self.episode is not None:
                    queries.append(f"{title} S{self.season:02d}E{self.episode:02d}")
        return tuple(dict.fromkeys(queries))


class ScrapeResult(TypedDict):
    title: str
    infoHash: str
    fileIndex: int | None
    seeders: int | None
    size: int | None
    tracker: str
    sources: list[str]
