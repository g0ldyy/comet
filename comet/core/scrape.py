from enum import StrEnum
from typing import Annotated

from pydantic import BeforeValidator, Field


class ScrapeContext(StrEnum):
    LIVE = "live"
    BACKGROUND = "background"


def normalize_scraper_mode(value: object) -> bool | str:
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "both", "on", "t", "true", "y", "yes"}:
            return True
        if normalized in {"0", "f", "false", "n", "no", "off"}:
            return False
        if normalized in ScrapeContext:
            return normalized
    elif isinstance(value, bool):
        return value
    raise ValueError("scraper mode must be true, false, both, live, or background")


ScraperMode = Annotated[
    bool | str,
    BeforeValidator(normalize_scraper_mode),
    Field(validate_default=True),
]


def normalize_scraper_name(name: str) -> str:
    normalized = name.strip().casefold()
    if normalized.endswith("scraper"):
        normalized = normalized.removesuffix("scraper")
    if not normalized or not normalized.isascii() or not normalized.isalnum():
        raise ValueError("scraper name is invalid")
    return normalized


def normalize_scraper_timeout_selector(selector: str) -> str:
    selector = selector.strip()
    parts = selector.split(":")
    if len(parts) > 2:
        raise ValueError("scraper timeout selector is invalid")

    scraper_name = normalize_scraper_name(parts[0])
    if len(parts) == 1:
        return scraper_name

    try:
        context = ScrapeContext(parts[1].strip().casefold())
    except ValueError:
        raise ValueError("scraper timeout selector is invalid") from None
    return f"{scraper_name}:{context.value}"
