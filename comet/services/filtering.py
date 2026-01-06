from RTN import parse, title_match

from comet.core.logger import logger
from comet.core.models import settings


def filter_worker(torrents, title, year, year_end, aliases, remove_adult_content):
    results = []
    for torrent in torrents:
        torrent_title = torrent["title"]
        if "sample" in torrent_title.lower() or torrent_title == "":
            if settings.RTN_FILTER_DEBUG:
                logger.log("FILTER", f"🚫 Rejected (Sample/Empty) | {torrent_title}")
            continue

        parsed = parse(torrent_title)

        if remove_adult_content and parsed.adult:
            if settings.RTN_FILTER_DEBUG:
                logger.log("FILTER", f"🔞 Rejected (Adult) | {torrent_title}")
            continue

        if not parsed.parsed_title or not title_match(
            title, parsed.parsed_title, aliases=aliases
        ):
            if settings.RTN_FILTER_DEBUG:
                logger.log(
                    "FILTER",
                    f"❌ Rejected (Title Mismatch) | {torrent_title} | Parsed: {parsed.parsed_title} | Expected: {title}",
                )
            continue

        if year and parsed.year:
            if year_end is not None:
                if not (year <= parsed.year <= year_end):
                    if settings.RTN_FILTER_DEBUG:
                        logger.log(
                            "FILTER",
                            f"📅 Rejected (Year Mismatch) | {torrent_title} | Year: {parsed.year} | Expected: {year}-{year_end}",
                        )
                    continue
            else:
                if year < (parsed.year - 1) or year > (parsed.year + 1):
                    if settings.RTN_FILTER_DEBUG:
                        logger.log(
                            "FILTER",
                            f"📅 Rejected (Year Mismatch) | {torrent_title} | Year: {parsed.year} | Expected: ~{year}",
                        )
                    continue

        torrent["parsed"] = parsed
        results.append(torrent)
    return results
