from RTN import parse, title_match

from comet.core.logger import logger
from comet.core.models import settings

if settings.RTN_FILTER_DEBUG:

    def _log_exclusion(msg):
        logger.log("FILTER", msg)
else:

    def _log_exclusion(msg):
        pass


def filter_worker(torrents, title, year, year_end, aliases, remove_adult_content):
    results = []

    for torrent in torrents:
        torrent_title = torrent["title"]
        if "sample" in torrent_title.lower() or torrent_title == "":
            _log_exclusion(f"🚫 Rejected (Sample/Empty) | {torrent_title}")
            continue

        parsed = parse(torrent_title)

        if remove_adult_content and parsed.adult:
            _log_exclusion(f"🔞 Rejected (Adult) | {torrent_title}")
            continue

        if not parsed.parsed_title or not title_match(
            title, parsed.parsed_title, aliases=aliases
        ):
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
