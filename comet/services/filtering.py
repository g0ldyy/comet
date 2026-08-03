import asyncio
import hashlib
import re
import unicodedata
from collections import OrderedDict, defaultdict
from collections.abc import Collection
from dataclasses import replace
from threading import Event, Lock

import orjson
from pydantic import ValidationError
from RTN import normalize_title, parse, title_match

from comet.core.execution import configured_max_workers, get_executor
from comet.core.locator_codec import parsed_json
from comet.core.models import settings
from comet.core.sources import ReleaseCandidate, TorrentLocator
from comet.observability import log
from comet.utils.languages import alias_language
from comet.utils.parsing import ensure_multi_language

_TITLE_MATCH_CACHE_MAX_ENTRIES = 65_536
_NORMALIZATION_PIPELINE_VERSION = 2
_FILTER_MESSAGES = {
    "adult_content": "Rejected adult release",
    "alias_language": "Added release language from alias",
    "empty_release": "Rejected empty release",
    "missing_title": "Rejected release without a parsed title",
    "parse_error": "Rejected release that RTN could not parse",
    "title_mismatch": "Rejected release with a title mismatch",
    "year_mismatch": "Rejected release with a year mismatch",
}


def _filter_debug_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    sanitized = "".join(
        " " if unicodedata.category(character) in {"Cc", "Cf"} else character
        for character in value
    ).strip()
    if not sanitized:
        return None
    return sanitized.encode("utf-8")[:512].decode("utf-8", "ignore")


def _debug_filter_decision(
    reason: str,
    release_title: str,
    *,
    content_id: str | None,
    matcher,
    parsed=None,
    detected_language: str | None = None,
    accepted: bool = False,
) -> None:
    fields = {
        "filter_reason": reason,
        "release_title": _filter_debug_text(release_title) or "unknown",
    }
    if content_id:
        fields["content_id"] = content_id
    if reason == "title_mismatch":
        expected_title = _filter_debug_text(matcher.title)
        parsed_title = _filter_debug_text(parsed.parsed_title)
        if parsed_title:
            fields["parsed_title"] = parsed_title
        if expected_title:
            fields["expected_title"] = expected_title
    elif reason == "year_mismatch":
        parsed_year = parsed.year
        if parsed_year is not None:
            fields["parsed_year"] = parsed_year
        if matcher.year:
            fields["expected_year_min"] = int(matcher.min_year)
            if matcher.max_year != float("inf"):
                fields["expected_year_max"] = int(matcher.max_year)
    elif reason == "alias_language":
        if parsed_title := _filter_debug_text(parsed.parsed_title):
            fields["parsed_title"] = parsed_title
        if detected_language := _filter_debug_text(detected_language):
            fields["detected_language"] = detected_language
    log.info(
        "filter.release.enriched" if accepted else "filter.release.rejected",
        _FILTER_MESSAGES[reason],
        **fields,
    )


def _ignore_filter_decision(*_args, **_kwargs) -> None:
    return


_log_filter_decision = _ignore_filter_decision


def exact_alias_match(
    text_normalized: str, ez_aliases_normalized: Collection[str]
) -> bool:
    # Exact membership prevents short aliases from matching unrelated release text.
    return bool(text_normalized) and text_normalized in ez_aliases_normalized


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
        self.aliases = aliases
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
    __slots__ = ("data", "inflight", "lock")

    def __init__(self):
        self.lock = Lock()
        self.data = OrderedDict()
        self.inflight = {}


_PARSE_CACHE_DEDUP_TIMEOUT = 5.0
_PARSE_CACHE_SIZE = 0
_PARSE_CACHE_EFFECTIVE_SHARDS = 0
_PARSE_CACHE_DEDUP_INFLIGHT = False
_PARSE_CACHE_SHARD_SIZES = []
_parse_cache = []


def configure_filtering(config) -> None:
    global _PARSE_CACHE_DEDUP_INFLIGHT, _PARSE_CACHE_EFFECTIVE_SHARDS
    global _PARSE_CACHE_SHARD_SIZES, _PARSE_CACHE_SIZE, _log_filter_decision
    global _parse_cache

    size = config.FILTER_PARSE_CACHE_SIZE
    configured_shards = max(config.FILTER_PARSE_CACHE_SHARDS, 1)
    effective_shards = min(configured_shards, size) if size > 0 else 0
    shard_sizes = [
        (size // effective_shards) + (1 if i < size % effective_shards else 0)
        for i in range(effective_shards)
    ]
    _PARSE_CACHE_SIZE = size
    _PARSE_CACHE_EFFECTIVE_SHARDS = effective_shards
    _PARSE_CACHE_DEDUP_INFLIGHT = config.FILTER_PARSE_CACHE_DEDUP_INFLIGHT
    _PARSE_CACHE_SHARD_SIZES = shard_sizes
    _parse_cache = [_ParseCacheShard() for _ in range(effective_shards)]
    _log_filter_decision = (
        _debug_filter_decision if config.RTN_FILTER_DEBUG else _ignore_filter_decision
    )


configure_filtering(settings)


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


def filter_release_records(
    records,
    title,
    year,
    year_end,
    media_type,
    aliases,
    remove_adult_content,
    content_id=None,
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
                lang = next(iter(langs))
                if lang not in ("neutral", "en"):
                    country_aliases[scrubbed_t] = lang
    for record in records:
        if not isinstance(record, dict):
            continue
        release_title = record.get("title")
        if not isinstance(release_title, str):
            continue

        if release_title == "":
            _log_filter_decision(
                "empty_release",
                release_title,
                content_id=content_id,
                matcher=matcher,
            )
            continue

        # temp fix while waiting for RTN to fix their parsing
        try:
            parsed = _parse_with_cache(release_title)
        except ValidationError:
            _log_filter_decision(
                "parse_error",
                release_title,
                content_id=content_id,
                matcher=matcher,
            )
            continue

        if parsed.parsed_title and country_aliases:
            language = country_aliases.get(scrub(parsed.parsed_title))
            if language and language not in parsed.languages:
                _log_filter_decision(
                    "alias_language",
                    release_title,
                    content_id=content_id,
                    matcher=matcher,
                    parsed=parsed,
                    detected_language=language,
                    accepted=True,
                )
                parsed.languages.append(language)

        ensure_multi_language(parsed)

        if remove_adult_content and parsed.adult:
            _log_filter_decision(
                "adult_content",
                release_title,
                content_id=content_id,
                matcher=matcher,
                parsed=parsed,
            )
            continue

        if not parsed.parsed_title:
            _log_filter_decision(
                "missing_title",
                release_title,
                content_id=content_id,
                matcher=matcher,
                parsed=parsed,
            )
            continue

        if not matcher.matches_title(release_title, parsed.parsed_title):
            _log_filter_decision(
                "title_mismatch",
                release_title,
                content_id=content_id,
                matcher=matcher,
                parsed=parsed,
            )
            continue

        if not matcher.matches_year(parsed.year):
            _log_filter_decision(
                "year_mismatch",
                release_title,
                content_id=content_id,
                matcher=matcher,
                parsed=parsed,
            )
            continue

        record["parsed"] = parsed
        results.append(record)
    return results


def filter_release_candidates(
    candidates: tuple[ReleaseCandidate, ...],
    title: str,
    year: int | None,
    year_end: int | None,
    media_type: str,
    aliases: dict,
    remove_adult_content: bool,
    content_id: str | None = None,
) -> tuple[ReleaseCandidate, ...]:
    """Parse and media-match transport-neutral discovery candidates."""
    records = [
        {"title": candidate.title, "candidate": candidate}
        for candidate in candidates
    ]
    filtered = filter_release_records(
        records,
        title,
        year,
        year_end,
        media_type,
        aliases,
        remove_adult_content,
        content_id,
    )
    normalized = []
    for record in filtered:
        candidate = record["candidate"]
        parsed = record["parsed"]
        encoded_parsed = parsed_json(parsed, trusted=True)
        locators = tuple(
            replace(locator, selection_parsed_json=encoded_parsed)
            if isinstance(locator, TorrentLocator)
            else locator
            for locator in candidate.locators
        )
        normalized.append(replace(candidate, parsed=parsed, locators=locators))
    return tuple(normalized)


async def normalize_release_candidates(
    candidates: tuple[ReleaseCandidate, ...],
    *,
    title: str,
    year: int | None,
    year_end: int | None,
    media_type: str,
    aliases: dict[str, list[str]],
    content_id: str | None = None,
) -> tuple[ReleaseCandidate, ...]:
    """Run the deterministic, shareable RTN stage outside request policy."""
    if not candidates:
        return ()
    worker_count = max(1, configured_max_workers() or 1)
    chunk_size = max(
        20,
        (len(candidates) + worker_count * 4 - 1) // (worker_count * 4),
    )
    loop = asyncio.get_running_loop()
    executor = get_executor()
    chunks = await asyncio.gather(
        *(
            loop.run_in_executor(
                executor,
                filter_release_candidates,
                candidates[offset : offset + chunk_size],
                title,
                year,
                year_end,
                media_type,
                aliases,
                False,
                content_id,
            )
            for offset in range(0, len(candidates), chunk_size)
        )
    )
    return tuple(candidate for chunk in chunks for candidate in chunk)


def apply_release_candidate_policy(
    candidates: tuple[ReleaseCandidate, ...],
    *,
    remove_adult_content: bool,
) -> tuple[ReleaseCandidate, ...]:
    """Apply the cheap request-specific policy after shared normalization."""
    return tuple(
        candidate
        for candidate in candidates
        if candidate.parsed is not None
        and (not remove_adult_content or not candidate.parsed.adult)
    )


def release_normalization_fingerprint(
    *,
    title: str,
    year: int | None,
    year_end: int | None,
    media_type: str,
    aliases: dict[str, list[str]],
) -> str:
    payload = orjson.dumps(
        [
            _NORMALIZATION_PIPELINE_VERSION,
            title,
            year,
            year_end,
            media_type,
            [[key, aliases[key]] for key in sorted(aliases)],
        ]
    )
    return hashlib.sha256(b"comet-release-normalization\0" + payload).hexdigest()
