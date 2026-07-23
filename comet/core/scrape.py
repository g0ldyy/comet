from enum import StrEnum


class ScrapeContext(StrEnum):
    LIVE = "live"
    BACKGROUND = "background"


def normalize_scraper_name(name: str) -> str:
    if type(name) is not str:
        raise ValueError("scraper name must be a string")

    normalized = name.strip().casefold()
    if normalized.endswith("scraper"):
        normalized = normalized.removesuffix("scraper")
    if not normalized or not normalized.isalnum():
        raise ValueError(f"invalid scraper name: {name!r}")
    return normalized


def normalize_scraper_timeout_selector(selector: str) -> str:
    if type(selector) is not str:
        raise ValueError("scraper timeout selector must be a string")

    parts = selector.strip().split(":")
    if len(parts) > 2:
        raise ValueError(f"invalid scraper timeout selector: {selector!r}")

    scraper_name = normalize_scraper_name(parts[0])
    if len(parts) == 1:
        return scraper_name

    try:
        context = ScrapeContext(parts[1].strip().casefold())
    except ValueError:
        raise ValueError(
            f"invalid scraper timeout context in selector: {selector!r}"
        ) from None
    return f"{scraper_name}:{context.value}"
