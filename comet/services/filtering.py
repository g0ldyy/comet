import re
from collections import OrderedDict, defaultdict
from collections.abc import Collection
from threading import Event, Lock

from pydantic import ValidationError
from RTN import normalize_title, parse, title_match

from comet.core.logger import logger
from comet.core.models import settings
from comet.utils.languages import alias_language
from comet.utils.parsing import ensure_multi_language

_TITLE_MATCH_CACHE_MAX_ENTRIES = 65_536

if settings.RTN_FILTER_DEBUG:

    def _log_exclusion(msg):
        logger.log("FILTER", msg)
else:

    def _log_exclusion(msg):
        pass


def exact_alias_match(
    text_normalized: str, ez_aliases_normalized: Collection[str]
) -> bool:
    # Exact membership prevents short aliases from matching unrelated release text.
    return bool(text_normalized) and text_normalized in ez_aliases_normalized


def _normalize_aliases(aliases: object) -> dict[str, list[str]]:
    if type(aliases) is not dict:
        return {}

    normalized = {}
    for country, titles in aliases.items():
        if type(country) is not str or not country or type(titles) is not list:
            continue
        current_titles = list(
            dict.fromkeys(
                normalized_title
                for title in titles
                if type(title) is str and (normalized_title := title.strip())
            )
        )
        if current_titles:
            normalized[country] = current_titles
    return normalized


# Bracketed metadata (e.g. "[1999, BDRip]", "(S2)", "{HEVC}") that pollutes a
# title segment and breaks RTN parsing.
_BRACKET_CONTENT = re.compile(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}")


def alternate_title_match(torrent_title: str, title: str, aliases) -> bool:
    """Match multi-title release names that RTN can't fully parse.

    Releases (common for anime / RU scene) often list several titles separated
    by "/", e.g. "Инициал «Ди» / Initial D: Second Stage / Второй этап". RTN
    only parses the first one, so a non-English first title fails title_match.
    Here we split on the separator, strip bracketed metadata from each segment,
    and try to match each remaining segment against the expected title/aliases.
    """
    if "/" not in torrent_title:
        return False

    for segment in torrent_title.split("/"):
        segment = _BRACKET_CONTENT.sub(" ", segment).strip()
        if not segment:
            continue

        try:
            parsed_segment = _parse_with_cache(segment)
        except ValidationError:
            continue

        if parsed_segment.parsed_title and title_match(
            title, parsed_segment.parsed_title, aliases=aliases
        ):
            return True

    return False


def scrub(t: str):
    return " ".join(normalize_title(t).split())


class TitleMatcher:
    """Prepared title/year matcher shared by live and persisted torrents."""

    __slots__ = (
        "_matches_cache",
        "aliases",
        "aliases_normalized",
        "max_year",
        "min_year",
        "title",
        "year",
        "year_end",
    )

    def __init__(self, title, year, year_end, media_type, aliases):
        self._matches_cache = None
        self.title = title
        self.year = year
        self.year_end = year_end
        self.aliases = _normalize_aliases(aliases)
        self.aliases_normalized = frozenset(
            normalized
            for titles in self.aliases.values()
            for alias in titles
            if (normalized := scrub(alias))
        )

        self.min_year = 0
        self.max_year = float("inf")
        if year:
            if year_end:
                self.min_year = year
                self.max_year = year_end
            elif media_type == "series":
                self.min_year = year - 1
            else:
                self.min_year = year - 1
                self.max_year = year + 1

    def matches_title(self, torrent_title: str, parsed_title: str) -> bool:
        if exact_alias_match(scrub(parsed_title), self.aliases_normalized):
            return True
        return title_match(
            self.title, parsed_title, aliases=self.aliases
        ) or alternate_title_match(torrent_title, self.title, self.aliases)

    def matches_year(self, parsed_year: int | None) -> bool:
        return not (
            self.year
            and parsed_year
            and not (self.min_year <= parsed_year <= self.max_year)
        )

    def matches(
        self, torrent_title: str, parsed_title: str, parsed_year: int | None
    ) -> bool:
        cache_key = (
            torrent_title if "/" in torrent_title else None,
            parsed_title,
            parsed_year,
        )
        cache = self._matches_cache
        if cache is None:
            cache = self._matches_cache = {}
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        matched = self.matches_title(torrent_title, parsed_title) and self.matches_year(
            parsed_year
        )
        if len(cache) < _TITLE_MATCH_CACHE_MAX_ENTRIES:
            cache[cache_key] = matched
        return matched


class _ParseCacheShard:
    __slots__ = ("lock", "data", "inflight")

    def __init__(self):
        self.lock = Lock()
        self.data = OrderedDict()
        self.inflight = {}


_PARSE_CACHE_SIZE = settings.FILTER_PARSE_CACHE_SIZE
_PARSE_CACHE_SHARDS = max(settings.FILTER_PARSE_CACHE_SHARDS, 1)
_PARSE_CACHE_DEDUP_INFLIGHT = settings.FILTER_PARSE_CACHE_DEDUP_INFLIGHT
_PARSE_CACHE_DEDUP_TIMEOUT = 5.0

if _PARSE_CACHE_SIZE > 0:
    _PARSE_CACHE_EFFECTIVE_SHARDS = min(_PARSE_CACHE_SHARDS, _PARSE_CACHE_SIZE)
else:
    _PARSE_CACHE_EFFECTIVE_SHARDS = 0

if _PARSE_CACHE_EFFECTIVE_SHARDS > 0:
    _PARSE_CACHE_SHARD_SIZES = [
        (_PARSE_CACHE_SIZE // _PARSE_CACHE_EFFECTIVE_SHARDS)
        + (1 if i < (_PARSE_CACHE_SIZE % _PARSE_CACHE_EFFECTIVE_SHARDS) else 0)
        for i in range(_PARSE_CACHE_EFFECTIVE_SHARDS)
    ]
else:
    _PARSE_CACHE_SHARD_SIZES = []

_parse_cache = [_ParseCacheShard() for _ in range(_PARSE_CACHE_EFFECTIVE_SHARDS)]


def _parse_cache_shard_for(title: str):
    shard_idx = hash(title) % _PARSE_CACHE_EFFECTIVE_SHARDS
    return shard_idx, _parse_cache[shard_idx], _PARSE_CACHE_SHARD_SIZES[shard_idx]


def _clone_parsed(parsed):
    # Filtering only mutates languages; keep immutable parse fields shared.
    clone = parsed.model_copy()
    clone.languages = list(parsed.languages)
    return clone


def _parse_with_cache(title: str):
    if _PARSE_CACHE_SIZE <= 0 or _PARSE_CACHE_EFFECTIVE_SHARDS <= 0:
        return parse(title)

    _, shard, max_size = _parse_cache_shard_for(title)
    if max_size <= 0:
        return parse(title)

    if _PARSE_CACHE_DEDUP_INFLIGHT:
        return _parse_with_cache_dedup(title, shard, max_size)
    else:
        return _parse_with_cache_simple(title, shard, max_size)


def _parse_with_cache_simple(title: str, shard: _ParseCacheShard, max_size: int):
    with shard.lock:
        cached = shard.data.get(title)
        if cached is not None:
            shard.data.move_to_end(title)
            return _clone_parsed(cached)

    parsed = parse(title)
    cached = _clone_parsed(parsed)

    with shard.lock:
        shard.data[title] = cached
        if len(shard.data) > max_size:
            shard.data.popitem(last=False)

    return parsed


def _parse_with_cache_dedup(title: str, shard: _ParseCacheShard, max_size: int):
    inflight_event = None
    do_parse = False

    with shard.lock:
        cached = shard.data.get(title)
        if cached is not None:
            shard.data.move_to_end(title)
            return _clone_parsed(cached)

        inflight_event = shard.inflight.get(title)
        if inflight_event is None:
            inflight_event = Event()
            shard.inflight[title] = inflight_event
            do_parse = True

    if not do_parse:
        if not inflight_event.wait(timeout=_PARSE_CACHE_DEDUP_TIMEOUT):
            return parse(title)

        with shard.lock:
            cached = shard.data.get(title)
            if cached is not None:
                shard.data.move_to_end(title)
                return _clone_parsed(cached)

        return parse(title)

    return _do_parse_and_cache(title, shard, max_size, inflight_event)


def _do_parse_and_cache(
    title: str,
    shard: _ParseCacheShard,
    max_size: int,
    inflight_event: Event,
):
    try:
        parsed = parse(title)
        cached = _clone_parsed(parsed)
        with shard.lock:
            shard.data[title] = cached
            if len(shard.data) > max_size:
                shard.data.popitem(last=False)
            shard.inflight.pop(title, None)
        return parsed
    except BaseException:
        with shard.lock:
            shard.inflight.pop(title, None)
        raise
    finally:
        inflight_event.set()


def filter_worker(
    torrents, title, year, year_end, media_type, aliases, remove_adult_content
):
    results = []
    matcher = TitleMatcher(title, year, year_end, media_type, aliases)
    aliases = matcher.aliases

    country_aliases = {}
    alias_to_langs = defaultdict(set)

    if settings.SMART_LANGUAGE_DETECTION:
        main_title_scrubbed = scrub(title)

        for country, titles in aliases.items():
            if country == "ez":
                for t in titles:
                    scrubbed_t = scrub(t)
                    alias_to_langs[scrubbed_t].add("neutral")
                continue

            lang = alias_language(country)
            for t in titles:
                scrubbed_t = scrub(t)
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
    for torrent in torrents:
        torrent_title = torrent["title"]
        torrent_title_lower = torrent_title.lower()

        if "sample" in torrent_title_lower or torrent_title == "":
            _log_exclusion(f"🚫 Rejected (Sample/Empty) | {torrent_title}")
            continue

        # temp fix while waiting for RTN to fix their parsing
        try:
            parsed = _parse_with_cache(torrent_title)
        except ValidationError:
            _log_exclusion(f"❌ Rejected (Parse Error) | {torrent_title}")
            continue

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

        if not matcher.matches_title(torrent_title, parsed.parsed_title):
            _log_exclusion(
                f"❌ Rejected (Title Mismatch) | {torrent_title} | Parsed: {parsed.parsed_title} | Expected: {title}"
            )
            continue

        if not matcher.matches_year(parsed.year):
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
