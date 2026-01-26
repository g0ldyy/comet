from collections import defaultdict

from RTN import normalize_title, parse, title_match

from comet.core.logger import logger
from comet.core.models import settings
from comet.utils.languages import COUNTRY_TO_LANGUAGE
from comet.utils.parsing import ensure_multi_language

if settings.RTN_FILTER_DEBUG:

    def _log_exclusion(msg):
        logger.log("FILTER", msg)
else:

    def _log_exclusion(msg):
        pass


def quick_alias_match(text_normalized: str, ez_aliases_normalized: list[str]):
    return any(alias in text_normalized for alias in ez_aliases_normalized)


def scrub(t: str):
    return " ".join(normalize_title(t).split())


def filter_worker(
    torrents, title, year, year_end, media_type, aliases, remove_adult_content
):
    results = []

    tz_aliases = set()
    country_aliases = {}
    alias_to_langs = defaultdict(set)

    if settings.SMART_LANGUAGE_DETECTION:
        main_title_scrubbed = scrub(title)

        for country, titles in aliases.items():
            if country == "ez":
                for t in titles:
                    scrubbed_t = scrub(t)
                    tz_aliases.add(scrubbed_t)
                    alias_to_langs[scrubbed_t].add("neutral")
                continue

            lang = COUNTRY_TO_LANGUAGE.get(country)
            for t in titles:
                scrubbed_t = scrub(t)
                tz_aliases.add(scrubbed_t)
                if lang:
                    alias_to_langs[scrubbed_t].add(lang)
                else:
                    alias_to_langs[scrubbed_t].add("neutral")

        # Only trust aliases that map to exactly one non-english language
        # and are not the main title itself.
        for scrubbed_t, langs in alias_to_langs.items():
            if scrubbed_t == main_title_scrubbed:
                continue

            if len(langs) == 1:
                lang = list(langs)[0]
                if lang not in ("neutral", "en"):
                    country_aliases[scrubbed_t] = lang
    else:
        for country, titles in aliases.items():
            for t in titles:
                tz_aliases.add(scrub(t))

    ez_aliases_normalized = list(tz_aliases)

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

        if parsed.parsed_title and country_aliases:
            language = country_aliases.get(scrub(parsed.parsed_title))
            if language and language not in parsed.languages:
                _log_exclusion(
                    f"🏷️ Added Language (Alias) | {torrent_title} | {language}"
                )
                parsed.languages.append(language)

        ensure_multi_language(parsed)

        if remove_adult_content and parsed.adult:
            _log_exclusion(f"🔞 Rejected (Adult) | {torrent_title}")
            continue

        if not parsed.parsed_title:
            _log_exclusion(f"❌ Rejected (No Parsed Title) | {torrent_title}")
            continue

        alias_matched = ez_aliases_normalized and quick_alias_match(
            scrub(torrent_title), ez_aliases_normalized
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
