from collections import OrderedDict, defaultdict
from threading import Event, Lock

from pydantic import ValidationError
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
    if hasattr(parsed, "model_copy"):
        return parsed.model_copy(deep=True)
    return parsed.copy(deep=True)


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
