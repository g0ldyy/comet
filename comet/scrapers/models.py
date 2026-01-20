from typing import List, Optional, TypedDict

from RTN import normalize_title
from pydantic import BaseModel, Field


class ScrapeRequest(BaseModel):
    media_type: str  # "movie" or "series"
    media_id: str  # Full ID (e.g., "tt1234567:1:1" or "kitsu:123")
    media_only_id: str  # Base ID (e.g., "tt1234567")
    title: str
    title_variants: List[str] = Field(default_factory=list)
    year: Optional[int] = None
    year_end: Optional[int] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    context: str = "live"  # "live" or "background"

    def model_post_init(self, __context):
        """
        Add a normalized variant of the title (without diacritics) so scrapers can
        query using both the original and normalized titles. This helps custom
        catalogs whose metadata contains diacritics find more torrent results.
        """
        variants = list(self.title_variants) if self.title_variants else [self.title]
        if self.title not in variants:
            variants.append(self.title)
        normalized = normalize_title(self.title)
        if normalized and normalized not in variants:
            variants.append(normalized)
        self.title_variants = variants


class ScrapeResult(TypedDict):
    title: str
    infoHash: str
    fileIndex: Optional[int]
    seeders: Optional[int]
    size: Optional[int]
    tracker: str
    sources: List[str]
