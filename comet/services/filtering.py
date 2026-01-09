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


def filter_worker(torrents, title, year, year_end, aliases, remove_adult_content):
    results = []

    ez_aliases = aliases.get("ez", [])
    if ez_aliases:
        ez_aliases_normalized = [normalize_title(a) for a in ez_aliases]

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
            if year_end is not None:
                if not (year <= parsed.year <= year_end):
                    _log_exclusion(
                        f"📅 Rejected (Year Mismatch) | {torrent_title} | Year: {parsed.year} | Expected: {year}-{year_end}"
                    )
                    continue
            else:
                if year < (parsed.year - 1) or year > (parsed.year + 1):
                    _log_exclusion(
                        f"📅 Rejected (Year Mismatch) | {torrent_title} | Year: {parsed.year} | Expected: ~{year}"
                    )
                    continue

        torrent["parsed"] = parsed
        results.append(torrent)
    return results
