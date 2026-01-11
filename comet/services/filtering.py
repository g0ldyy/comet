from RTN import normalize_title, parse, title_match

from comet.core.logger import logger
from comet.core.models import settings

if settings.RTN_FILTER_DEBUG:

    def _log_exclusion(msg):
        logger.log("FILTER", msg)
else:

    def _log_exclusion(msg):
        pass


def quick_alias_match(text_normalized: str, ez_aliases_normalized: list[str]):
    return any(alias in text_normalized for alias in ez_aliases_normalized)


def filter_worker(
    torrents, title, year, year_end, media_type, aliases, remove_adult_content
):
    results = []

    ez_aliases = aliases.get("ez", [])
    if ez_aliases:
        ez_aliases_normalized = [normalize_title(a) for a in ez_aliases]

    min_year = 0
    max_year = float("inf")

    if year:
        if year_end:
            min_year = year
            max_year = year_end
        elif media_type == "series":
            min_year = year - 1
        else:
            min_year = year - 1
            max_year = year + 1

    for torrent in torrents:
        torrent_title = torrent["title"]
        torrent_title_lower = torrent_title.lower()

        if "sample" in torrent_title_lower or torrent_title == "":
            _log_exclusion(f"🚫 Rejected (Sample/Empty) | {torrent_title}")
            continue

        parsed = parse(torrent_title)

        if remove_adult_content and parsed.adult:
            _log_exclusion(f"🔞 Rejected (Adult) | {torrent_title}")
            continue

        if not parsed.parsed_title:
            _log_exclusion(f"❌ Rejected (No Parsed Title) | {torrent_title}")
            continue

        alias_matched = ez_aliases and quick_alias_match(
            normalize_title(torrent_title), ez_aliases_normalized
        )
        if not alias_matched:
            if not title_match(title, parsed.parsed_title, aliases=aliases):
                _log_exclusion(
                    f"❌ Rejected (Title Mismatch) | {torrent_title} | Parsed: {parsed.parsed_title} | Expected: {title}"
                )
                continue

        if year and parsed.year:
            if not (min_year <= parsed.year <= max_year):
                if year_end:
                    expected = f"{year}-{year_end}"
                elif media_type == "series":
                    expected = f">{year}"
                else:
                    expected = f"~{year}"

                _log_exclusion(
                    f"📅 Rejected (Year Mismatch) | {torrent_title} | Year: {parsed.year} | Expected: {expected}"
                )
                continue

        torrent["parsed"] = parsed
        results.append(torrent)
    return results
