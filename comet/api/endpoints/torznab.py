import math
import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email.utils import format_datetime
from functools import lru_cache
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, BackgroundTasks, Request, Response

from comet.core.config_validation import config_check
from comet.core.logger import logger
from comet.core.models import database, settings
from comet.metadata.episode_index import EpisodeIndexService
from comet.metadata.imdb import resolve_imdb_title
from comet.metadata.tmdb import TMDBApi
from comet.observability import metrics
from comet.services.media_search import MediaSearchResult, MediaSearchStatus, search_media
from comet.services.trackers import trackers as global_trackers
from comet.utils.cache import CachePolicies, cached_response
from comet.utils.http_client import http_client_manager
from comet.utils.network import get_client_ip
from comet.utils.parsing import MediaScope, parse_media_id
from comet.utils.torrent_cache import build_torrent_cache_where

router = APIRouter()

TORZNAB_NAMESPACE = "http://torznab.com/schemas/2015/feed"
NEWZNAB_NAMESPACE = "http://www.newznab.com/DTD/2010/feeds/attributes/"
RECENT_CANDIDATE_LIMIT = 20
FEED_LIMIT = 10_000

_IMDB_ID = re.compile(r"tt([0-9]{7,10})", re.IGNORECASE)
_IMDB_ID_WITHOUT_PREFIX = re.compile(r"[0-9]{7,10}")
_NONNEGATIVE_INTEGER = re.compile(r"[0-9]{1,18}", re.ASCII)
_YEAR = re.compile(r"[0-9]{4}", re.ASCII)
_SEASON = re.compile(r"[sS]?([0-9]{1,4})", re.ASCII)
_EPISODE = re.compile(r"[eE]?([0-9]{1,6})", re.ASCII)
_DAILY_EPISODE = re.compile(r"([0-9]{1,2})/([0-9]{1,2})", re.ASCII)
_TITLE_SCOPE = re.compile(
    r"(?i)(?:[ ._-]+)S([0-9]{1,4})(?:E([0-9]{1,6}))?\s*$"
)
_INFO_HASH = re.compile(r"[0-9a-fA-F]{40}", re.ASCII)
_ILLEGAL_XML_CHARACTER = re.compile(
    "[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]"
)
_XML_DECLARATION = "<?xml version='1.0' encoding='utf-8'?>\n"
_XML_ESCAPES = (
    ("&", "&amp;"),
    ("<", "&lt;"),
    (">", "&gt;"),
    ('"', "&quot;"),
    ("\t", "&#9;"),
    ("\n", "&#10;"),
    ("\r", "&#13;"),
)


class TorznabProtocolError(ValueError):
    def __init__(self, code: int, description: str):
        super().__init__(description)
        self.code = code
        self.description = description


@dataclass(frozen=True, slots=True)
class TorznabQuery:
    function: str
    query: str | None
    imdb_id: str | None
    season: int | None
    episode: int | str | None
    year: int | None
    categories: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class CategoryConstraint:
    media_type: str | None
    unsupported_only: bool


@dataclass(frozen=True, slots=True)
class SearchTarget:
    media_type: str
    media_id: str


def _clean_xml(value: object) -> str:
    return _ILLEGAL_XML_CHARACTER.sub(
        "", value if type(value) is str else str(value)
    )


def _escape(value: object) -> str:
    """Escape a value for use as XML text or as an attribute value."""
    text = _clean_xml(value)
    for character, entity in _XML_ESCAPES:
        text = text.replace(character, entity)
    return text


def _xml_document(*fragments: str) -> bytes:
    return "".join((_XML_DECLARATION, *fragments)).encode()


def _normalize_imdb_id(value: str, *, prefix_optional: bool) -> str:
    normalized = value.strip()
    match = _IMDB_ID.fullmatch(normalized)
    if match is not None:
        return f"tt{match.group(1)}"
    if prefix_optional and _IMDB_ID_WITHOUT_PREFIX.fullmatch(normalized):
        return f"tt{normalized}"
    raise TorznabProtocolError(201, "Invalid parameter")


def _parse_category(value: str) -> int:
    if _NONNEGATIVE_INTEGER.fullmatch(value.strip()) is None:
        raise TorznabProtocolError(201, "Invalid parameter")
    return int(value)


def parse_torznab_query(request: Request) -> TorznabQuery:
    values: dict[str, str] = {}
    category_values = []
    for raw_name, raw_value in request.query_params.multi_items():
        name = raw_name.casefold()
        if name == "cat":
            category_values.append(raw_value)
        else:
            values[name] = raw_value

    function = values.get("t", "caps").strip().casefold() or "caps"
    if function == "get":
        raise TorznabProtocolError(203, "Function not available")
    if function not in {"caps", "search", "movie", "tvsearch"}:
        raise TorznabProtocolError(202, "Unknown function")

    output = values.get("o", "").strip().casefold()
    if output not in {"", "xml"}:
        raise TorznabProtocolError(201, "Invalid parameter")
    if function == "caps":
        return TorznabQuery(function, None, None, None, None, None, ())

    categories = []
    for category_group in category_values:
        for category in category_group.split(","):
            if not category.strip():
                raise TorznabProtocolError(201, "Invalid parameter")
            categories.append(_parse_category(category))

    raw_season = values.get("season")
    season = None
    if raw_season is not None:
        match = _SEASON.fullmatch(raw_season.strip())
        if match is None:
            raise TorznabProtocolError(201, "Invalid parameter")
        season = int(match.group(1))

    raw_episode = values.get("ep")
    episode: int | str | None = None
    if raw_episode is not None:
        episode_value = raw_episode.strip()
        daily_match = _DAILY_EPISODE.fullmatch(episode_value)
        if daily_match is not None:
            episode = f"{int(daily_match.group(1)):02d}/{int(daily_match.group(2)):02d}"
        else:
            episode_match = _EPISODE.fullmatch(episode_value)
            if episode_match is None:
                raise TorznabProtocolError(201, "Invalid parameter")
            episode = int(episode_match.group(1))

    raw_year = values.get("year")
    year = None
    if raw_year is not None:
        if _YEAR.fullmatch(raw_year.strip()) is None:
            raise TorznabProtocolError(201, "Invalid parameter")
        year = int(raw_year)

    if isinstance(episode, str) and season is not None:
        if raw_season is None or _YEAR.fullmatch(raw_season.strip()) is None:
            raise TorznabProtocolError(201, "Invalid parameter")
        month, day = (int(value) for value in episode.split("/", 1))
        try:
            date(season, month, day)
        except ValueError as exc:
            raise TorznabProtocolError(201, "Invalid parameter") from exc

    imdb_id = values.get("imdbid")
    if imdb_id is not None:
        imdb_id = _normalize_imdb_id(imdb_id, prefix_optional=True)

    query = values.get("q")
    if query is not None:
        query = query.strip()

    return TorznabQuery(
        function,
        query,
        imdb_id,
        season,
        episode,
        year,
        tuple(categories),
    )


def category_constraint(categories: tuple[int, ...]) -> CategoryConstraint:
    has_movies = any(2000 <= category <= 2999 for category in categories)
    has_tv = any(5000 <= category <= 5999 for category in categories)
    if categories and not has_movies and not has_tv:
        return CategoryConstraint(None, True)
    if has_movies == has_tv:
        return CategoryConstraint(None, False)
    return CategoryConstraint("movie" if has_movies else "series", False)


def _function_media_type(function: str) -> str | None:
    if function == "movie":
        return "movie"
    if function == "tvsearch":
        return "series"
    return None


def _extract_title_scope(
    query: str,
    season: int | None,
    episode: int | str | None,
) -> tuple[str, int | None, int | str | None]:
    match = _TITLE_SCOPE.search(query)
    if match is None:
        return query, season, episode
    title = query[: match.start()].strip(" ._-")
    if season is None:
        season = int(match.group(1))
    if episode is None and match.group(2) is not None:
        episode = int(match.group(2))
    return title, season, episode


async def _resolve_daily_episode(
    imdb_id: str,
    year: int,
    episode: str,
    session,
) -> tuple[int, int] | None:
    month, day = (int(value) for value in episode.split("/", 1))
    try:
        air_date = date(year, month, day).isoformat()
    except ValueError as exc:
        raise TorznabProtocolError(201, "Invalid parameter") from exc
    return await EpisodeIndexService(session).get_episode_by_air_date(
        imdb_id, air_date
    )


async def resolve_search_target(
    query: TorznabQuery,
    constraint: CategoryConstraint,
    session,
) -> SearchTarget | None:
    function_type = _function_media_type(query.function)
    if (
        function_type is not None
        and constraint.media_type is not None
        and function_type != constraint.media_type
    ):
        return None

    season = query.season
    episode = query.episode
    if function_type == "movie" and (season is not None or episode is not None):
        raise TorznabProtocolError(201, "Invalid parameter")
    if episode is not None and season is None:
        raise TorznabProtocolError(201, "Invalid parameter")

    imdb_id = query.imdb_id
    title_query = query.query or ""
    if title_query:
        imdb_match = _IMDB_ID.fullmatch(title_query)
        if imdb_match is not None and imdb_id is None:
            imdb_id = f"tt{imdb_match.group(1)}"
        elif imdb_match is None:
            title_query, season, episode = _extract_title_scope(
                title_query, season, episode
            )

    forced_type = function_type or constraint.media_type
    if forced_type is None and (season is not None or episode is not None):
        forced_type = "series"

    resolved_type = forced_type
    if imdb_id is None:
        if not title_query:
            raise TorznabProtocolError(200, "Missing parameter")
        match = await resolve_imdb_title(
            session,
            title_query,
            media_type=forced_type,
            year=query.year,
        )
        if match is None:
            return None
        imdb_id = match.imdb_id
        if resolved_type is None:
            resolved_type = match.media_type
    elif resolved_type is None:
        resolved_type = await TMDBApi(session).get_media_type_from_imdb(imdb_id)
        if resolved_type is None:
            return None

    if constraint.media_type is not None and resolved_type != constraint.media_type:
        return None

    if resolved_type == "movie":
        if season is not None or episode is not None:
            raise TorznabProtocolError(201, "Invalid parameter")
        return SearchTarget("movie", imdb_id)
    if resolved_type != "series":
        return None

    if isinstance(episode, str):
        if season is None:
            raise TorznabProtocolError(201, "Invalid parameter")
        daily_scope = await _resolve_daily_episode(imdb_id, season, episode, session)
        if daily_scope is None:
            return None
        season, episode = daily_scope

    media_id = imdb_id
    if season is not None:
        media_id += f":{int(season)}"
        if episode is not None:
            media_id += f":{int(episode)}"
    return SearchTarget("series", media_id)


async def _candidate_torrent_row(media_id: str):
    try:
        _, season, episode = parse_media_id("series", media_id)
    except ValueError:
        return await database.fetch_one(
            """
            SELECT info_hash, season, episode
            FROM torrents
            WHERE media_id = :media_id
            LIMIT 1
            """,
            {"media_id": media_id},
        )

    scope = (
        MediaScope.EPISODE
        if episode is not None
        else MediaScope.SEASON
        if season is not None
        else MediaScope.SERIES
    )
    where_clause, params = build_torrent_cache_where(
        media_id.split(":", 1)[0], scope, season, episode
    )
    return await database.fetch_one(
        "SELECT info_hash, season, episode " + where_clause + " LIMIT 1",
        params,
    )


async def find_recent_target(
    constraint: CategoryConstraint,
    session,
) -> SearchTarget | None:
    rows = await database.fetch_all(
        """
        SELECT media_id
        FROM media_demand
        WHERE last_scraped_at IS NOT NULL
        ORDER BY last_scraped_at DESC
        LIMIT :limit
        """,
        {"limit": RECENT_CANDIDATE_LIMIT},
    )

    type_lookup_used = False
    for row in rows:
        media_id = row["media_id"]
        if not isinstance(media_id, str) or media_id.startswith("kitsu:"):
            continue
        try:
            imdb_id, season, episode = parse_media_id("series", media_id)
        except ValueError:
            continue

        torrent_row = await _candidate_torrent_row(media_id)
        if torrent_row is None:
            continue

        scoped_series = season is not None or episode is not None
        locally_series = scoped_series or torrent_row["season"] is not None or torrent_row[
            "episode"
        ] is not None
        if locally_series:
            if constraint.media_type == "movie":
                continue
            return SearchTarget("series", media_id)

        if type_lookup_used:
            continue
        type_lookup_used = True
        media_type = await TMDBApi(session).get_media_type_from_imdb(imdb_id)
        if constraint.media_type is not None and media_type != constraint.media_type:
            continue
        if media_type == "movie":
            return SearchTarget("movie", imdb_id)
        if media_type == "series":
            return SearchTarget("series", media_id)

    return None


def _valid_tracker(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = _clean_xml(value.strip())
    if normalized.casefold().startswith("tracker:"):
        normalized = normalized[8:].strip()
    parsed = urlsplit(normalized)
    if parsed.scheme.casefold() not in {"http", "https", "udp"} or not parsed.hostname:
        return None
    return normalized


@lru_cache(maxsize=1024)
def _encoded_trackers(candidates: tuple[str, ...]) -> str:
    """Encode a tracker list as magnet `&tr=` parameters.

    Cached because a feed reuses the same list across every one of its items.
    """
    trackers = dict.fromkeys(
        tracker
        for tracker in map(_valid_tracker, candidates)
        if tracker is not None
    )
    return "".join(f"&tr={quote(tracker, safe='')}" for tracker in trackers)


def build_magnet(info_hash: str, title: str, torrent: dict) -> str:
    sources = torrent.get("sources")
    trackers = _encoded_trackers(
        tuple(sources) if sources else tuple(global_trackers)
    )
    return (
        f"magnet:?xt={quote(f'urn:btih:{info_hash}', safe=':')}"
        f"&dn={quote(_clean_xml(title), safe='')}{trackers}"
    )


def _pub_date(value: object, fallback_timestamp: float) -> str:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        timestamp = fallback_timestamp
    if not math.isfinite(timestamp) or timestamp <= 0:
        timestamp = fallback_timestamp
    try:
        return format_datetime(datetime.fromtimestamp(timestamp, UTC), usegmt=True)
    except (OverflowError, OSError, ValueError):
        return format_datetime(
            datetime.fromtimestamp(fallback_timestamp, UTC), usegmt=True
        )


def _torznab_attribute(name: str, value: object) -> str:
    return f'<torznab:attr name="{name}" value="{_escape(value)}"/>'


def _serialize_items(
    result: MediaSearchResult,
    media_type: str,
    request_timestamp: float,
) -> list[str]:
    items = []
    imdb_digits = result.media_only_id.removeprefix("tt")
    category_id = "2000" if media_type == "movie" else "5000"
    category_name = "Movies" if media_type == "movie" else "TV"

    for raw_info_hash in result.ranked_info_hashes:
        if (
            not isinstance(raw_info_hash, str)
            or _INFO_HASH.fullmatch(raw_info_hash) is None
        ):
            continue
        torrent = result.torrents.get(raw_info_hash)
        if not isinstance(torrent, dict):
            continue
        raw_title = torrent.get("title")
        if not isinstance(raw_title, str):
            continue
        title = _clean_xml(raw_title)
        if not title:
            continue

        info_hash = raw_info_hash.lower()
        raw_size = torrent.get("size")
        size = (
            raw_size
            if type(raw_size) is int and raw_size >= 0
            else 0
        )
        # Magnets are percent-encoded ASCII, so "&" is their only XML-significant
        # character and no illegal-character sweep is needed.
        magnet = build_magnet(info_hash, title, torrent).replace("&", "&amp;")
        fragments = [
            f"<item><title>{_escape(title)}</title>"
            f'<guid isPermaLink="false">{info_hash}</guid>'
            f"<pubDate>{_pub_date(torrent.get('updatedAt'), request_timestamp)}</pubDate>"
            f"<link>{magnet}</link>"
            f"<category>{category_name}</category>"
            f"<size>{size}</size>"
            f'<enclosure url="{magnet}" length="{size}"'
            ' type="application/x-bittorrent;x-scheme-handler/magnet"/>',
            _torznab_attribute("category", category_id),
            _torznab_attribute("size", size),
            _torznab_attribute("infohash", info_hash),
            f'<torznab:attr name="magneturl" value="{magnet}"/>',
            _torznab_attribute("imdb", imdb_digits),
        ]

        seeders = torrent.get("seeders")
        if type(seeders) is int and seeders >= 0:
            fragments.append(_torznab_attribute("seeders", seeders))
        if media_type == "series":
            if result.search_season is not None:
                fragments.append(
                    _torznab_attribute("season", result.search_season)
                )
            if result.search_episode is not None:
                fragments.append(
                    _torznab_attribute("episode", result.search_episode)
                )

        parsed = torrent.get("parsed")
        if parsed is not None:
            parsed_year = getattr(parsed, "year", None)
            if type(parsed_year) is int and parsed_year > 0:
                fragments.append(_torznab_attribute("year", parsed_year))
            languages = getattr(parsed, "languages", None)
            if isinstance(languages, list):
                language = ",".join(
                    value for value in languages if isinstance(value, str) and value
                )
                if language:
                    fragments.append(_torznab_attribute("language", language))
            resolution = str(getattr(parsed, "resolution", "") or "")
            if resolution and resolution.casefold() != "unknown":
                fragments.append(_torznab_attribute("resolution", resolution))

        fragments.append("</item>")
        items.append("".join(fragments))

    return items


def serialize_feed(
    result: MediaSearchResult | None,
    media_type: str | None,
    link: str,
    *,
    request_timestamp: float | None = None,
) -> tuple[bytes, int]:
    items = (
        _serialize_items(
            result,
            media_type,
            request_timestamp if request_timestamp is not None else time.time(),
        )
        if result is not None and media_type is not None
        else []
    )
    total = len(items)

    return (
        _xml_document(
            f'<rss version="2.0" xmlns:torznab="{TORZNAB_NAMESPACE}"'
            f' xmlns:newznab="{NEWZNAB_NAMESPACE}"><channel>'
            "<title>Comet</title>"
            "<description>Comet torrent results</description>"
            f"<link>{_escape(link)}</link>"
            "<language>en</language>"
            f'<newznab:response offset="0" total="{total}"/>',
            *items,
            "</channel></rss>",
        ),
        total,
    )


def serialize_caps() -> bytes:
    available = "no" if settings.DISABLE_TORRENT_STREAMS else "yes"
    return _xml_document(
        "<caps>"
        '<server version="1.3" title="Comet"/>'
        f'<limits max="{FEED_LIMIT}" default="{FEED_LIMIT}"/>'
        "<searching>"
        f'<search available="{available}" supportedParams="q"/>'
        f'<tv-search available="{available}" supportedParams="q,imdbid,season,ep"/>'
        f'<movie-search available="{available}" supportedParams="q,imdbid"/>'
        "</searching>"
        "<categories>"
        '<category id="2000" name="Movies"/>'
        '<category id="5000" name="TV"/>'
        "</categories>"
        "</caps>"
    )


def serialize_error(code: int, description: str) -> bytes:
    return _xml_document(
        f'<error code="{code}" description="{_escape(description)}"/>'
    )


def _xml_response(
    request: Request,
    content: bytes,
    *,
    feed: bool,
    cache_policy=None,
):
    return cached_response(
        request,
        content,
        media_type=(
            "application/rss+xml; charset=utf-8"
            if feed
            else "application/xml; charset=utf-8"
        ),
        cache_policy=cache_policy,
    )


def _protocol_error_response(
    request: Request,
    error: TorznabProtocolError,
    *,
    no_store: bool = False,
):
    policy = CachePolicies.no_cache() if no_store else CachePolicies.empty_results()
    return _xml_response(
        request,
        serialize_error(error.code, error.description),
        feed=False,
        cache_policy=policy,
    )


def _request_link(request: Request) -> str:
    return f"{str(request.base_url).rstrip('/')}{request.url.path}"


def _feed_response(
    request: Request,
    result: MediaSearchResult | None = None,
    media_type: str | None = None,
) -> tuple[Response, int]:
    content, total = serialize_feed(result, media_type, _request_link(request))
    response = _xml_response(
        request,
        content,
        feed=True,
        cache_policy=(
            CachePolicies.streams() if total else CachePolicies.empty_results()
        ),
    )
    return response, total


@router.get(
    "/torznab/api",
    tags=["Stremio"],
    summary="Torznab API",
    description="Returns capabilities and torrent search feeds.",
)
async def torznab_api(
    request: Request,
    background_tasks: BackgroundTasks,
):
    try:
        query = parse_torznab_query(request)
        if query.function == "caps":
            return _xml_response(
                request,
                serialize_caps(),
                feed=False,
                cache_policy=CachePolicies.manifest(),
            )

        if settings.DISABLE_TORRENT_STREAMS:
            raise TorznabProtocolError(203, "Function not available")

        constraint = category_constraint(query.categories)
        function_type = _function_media_type(query.function)
        if constraint.unsupported_only or (
            function_type is not None
            and constraint.media_type is not None
            and function_type != constraint.media_type
        ):
            return _feed_response(request)[0]

        session = await http_client_manager.get_session()
        if (
            query.function == "search"
            and query.imdb_id is None
            and not query.query
        ):
            if query.season is not None or query.episode is not None:
                raise TorznabProtocolError(200, "Missing parameter")
            target = await find_recent_target(constraint, session)
        else:
            target = await resolve_search_target(query, constraint, session)

        if target is None:
            return _feed_response(request)[0]

        config = config_check(None, strict_b64config=True)
        if config is None:
            raise RuntimeError("Default configuration is unavailable")
        result = await search_media(
            target.media_type,
            target.media_id,
            config,
            get_client_ip(request),
            background_tasks.add_task,
        )

        if result.status is MediaSearchStatus.INVALID:
            raise TorznabProtocolError(201, "Invalid parameter")
        if result.status is MediaSearchStatus.DISABLED:
            raise TorznabProtocolError(203, "Function not available")
        if result.status is MediaSearchStatus.BUSY:
            raise TorznabProtocolError(900, "Search busy; retry shortly")

        empty = result.status in {
            MediaSearchStatus.UNRELEASED,
            MediaSearchStatus.METADATA_UNAVAILABLE,
        }
        response, total = _feed_response(
            request,
            None if empty else result,
            None if empty else target.media_type,
        )

        metrics.observe_stream(
            target.media_type,
            "torznab",
            result.cache_state,
            "empty" if total == 0 else "success",
            total,
        )
        return response
    except TorznabProtocolError as error:
        return _protocol_error_response(
            request,
            error,
            no_store=error.code == 900,
        )
    except Exception:
        logger.exception("Unexpected Torznab request failure")
        return _protocol_error_response(
            request,
            TorznabProtocolError(900, "Backend error"),
            no_store=True,
        )
