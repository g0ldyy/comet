from enum import StrEnum

_MAX_SCRAPER_NAME_BYTES = 64
_MAX_SCRAPER_SELECTOR_BYTES = 80


class ScrapeContext(StrEnum):
    LIVE = "live"
    BACKGROUND = "background"


def normalize_scraper_name(name: str) -> str:
    if type(name) is not str or not name.strip():
        raise ValueError("scraper name is invalid")
    name = name.strip()
    try:
        if len(name.encode("utf-8")) > _MAX_SCRAPER_NAME_BYTES:
            raise ValueError
    except (UnicodeEncodeError, ValueError):
        raise ValueError("scraper name is invalid") from None

    normalized = name.casefold()
    if normalized.endswith("scraper"):
        normalized = normalized.removesuffix("scraper")
    if not normalized or not normalized.isascii() or not normalized.isalnum():
        raise ValueError("scraper name is invalid")
    return normalized


def normalize_scraper_timeout_selector(selector: str) -> str:
    if type(selector) is not str or not selector.strip():
        raise ValueError("scraper timeout selector is invalid")
    selector = selector.strip()
    try:
        if len(selector.encode("utf-8")) > _MAX_SCRAPER_SELECTOR_BYTES:
            raise ValueError
    except (UnicodeEncodeError, ValueError):
        raise ValueError("scraper timeout selector is invalid") from None

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
