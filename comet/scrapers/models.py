from typing import List, Optional, TypedDict

from pydantic import BaseModel


class ScrapeRequest(BaseModel):
    media_type: str  # "movie" or "series"
    media_id: str  # Full ID (e.g., "tt1234567:1:1" or "kitsu:123")
    media_only_id: str  # Base ID (e.g., "tt1234567")
    title: str
    year: Optional[int] = None
    year_end: Optional[int] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    context: str = "live"  # "live" or "background"
    search_titles: tuple[str, ...] = ()

    @property
    def query_titles(self) -> tuple[str, ...]:
        return self.search_titles or (self.title,)

    def title_queries(self, *, include_episode_variants: bool = False):
        queries = []
        for title in self.query_titles:
            queries.append(title)
            if (
                include_episode_variants
                and self.media_type == "series"
                and self.season is not None
                and self.episode is not None
            ):
                queries.append(f"{title} S{self.season:02d}")
                queries.append(f"{title} S{self.season:02d}E{self.episode:02d}")
        return tuple(dict.fromkeys(queries))


class ScrapeResult(TypedDict):
    title: str
    infoHash: str
    fileIndex: Optional[int]
    seeders: Optional[int]
    size: Optional[int]
    tracker: str
    sources: List[str]
