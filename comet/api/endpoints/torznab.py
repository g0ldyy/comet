import math
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email.utils import format_datetime
from urllib.parse import quote, urlencode, urlsplit

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
from comet.utils.cache import (
    CachePolicies,
    check_etag_match,
    generate_etag,
    not_modified_response,
)
from comet.utils.http_client import http_client_manager
from comet.utils.network import get_client_ip
from comet.utils.parsing import MediaScope, parse_media_id
from comet.utils.torrent_cache import build_torrent_cache_where

router = APIRouter()

TORZNAB_NAMESPACE = "http://torznab.com/schemas/2015/feed"
NEWZNAB_NAMESPACE = "http://www.newznab.com/DTD/2010/feeds/attributes/"
MAX_RESULTS = 100
RECENT_CANDIDATE_LIMIT = 20

ET.register_namespace("torznab", TORZNAB_NAMESPACE)
ET.register_namespace("newznab", NEWZNAB_NAMESPACE)

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
    offset: int
    limit: int


@dataclass(frozen=True, slots=True)
class CategoryConstraint:
    media_type: str | None
    unsupported_only: bool


@dataclass(frozen=True, slots=True)
class SearchTarget:
    media_type: str
    media_id: str


def _clean_xml(value: object) -> str:
    text = str(value)
    return "".join(
        character
        for character in text
        if (
            ord(character) in (0x09, 0x0A, 0x0D)
            or 0x20 <= ord(character) <= 0xD7FF
            or 0xE000 <= ord(character) <= 0xFFFD
            or 0x10000 <= ord(character) <= 0x10FFFF
        )
    )


def _xml_bytes(root: ET.Element) -> bytes:
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _normalize_imdb_id(value: str, *, prefix_optional: bool) -> str:
    normalized = value.strip()
    match = _IMDB_ID.fullmatch(normalized)
    if match is not None:
        return f"tt{match.group(1)}"
    if prefix_optional and _IMDB_ID_WITHOUT_PREFIX.fullmatch(normalized):
        return f"tt{normalized}"
    raise TorznabProtocolError(201, "Invalid parameter")


def _parse_nonnegative(value: str, *, limit: bool = False) -> int:
    if _NONNEGATIVE_INTEGER.fullmatch(value.strip()) is None:
        raise TorznabProtocolError(201, "Invalid parameter")
    parsed = int(value)
    return min(parsed, MAX_RESULTS) if limit else parsed


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
        return TorznabQuery(function, None, None, None, None, None, (), 0, MAX_RESULTS)

    categories = []
    for category_group in category_values:
        for category in category_group.split(","):
            if not category.strip():
                raise TorznabProtocolError(201, "Invalid parameter")
            categories.append(_parse_nonnegative(category))

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

    offset = _parse_nonnegative(values.get("offset", "0"))
    result_limit = _parse_nonnegative(values.get("limit", str(MAX_RESULTS)), limit=True)
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
        offset,
        result_limit,
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


def _torrent_trackers(torrent: dict) -> list[str]:
    candidates = torrent.get("sources") or global_trackers
    normalized = []
    seen = set()
    for candidate in candidates:
        tracker = _valid_tracker(candidate)
        if tracker is None or tracker in seen:
            continue
        seen.add(tracker)
        normalized.append(tracker)
    return normalized


def build_magnet(info_hash: str, title: str, torrent: dict) -> str:
    parameters = [("xt", f"urn:btih:{info_hash}"), ("dn", _clean_xml(title))]
    parameters.extend(("tr", tracker) for tracker in _torrent_trackers(torrent))
    return "magnet:?" + urlencode(parameters, doseq=True, quote_via=quote)


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


def _add_torznab_attribute(item: ET.Element, name: str, value: object) -> None:
    ET.SubElement(
        item,
        "torznab:attr",
        {"name": name, "value": _clean_xml(value)},
    )


def _serializable_items(
    result: MediaSearchResult,
    media_type: str,
    request_timestamp: float,
) -> list[ET.Element]:
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
        info_hash = raw_info_hash.lower()
        torrent = result.torrents.get(raw_info_hash)
        if not isinstance(torrent, dict):
            continue
        raw_title = torrent.get("title")
        if not isinstance(raw_title, str) or not raw_title:
            continue
        title = _clean_xml(raw_title)
        if not title:
            continue

        raw_size = torrent.get("size")
        size = (
            raw_size
            if type(raw_size) is int and raw_size >= 0
            else 0
        )
        magnet = _clean_xml(build_magnet(info_hash, title, torrent))
        item = ET.Element("item")
        ET.SubElement(item, "title").text = title
        ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = info_hash
        ET.SubElement(item, "pubDate").text = _pub_date(
            torrent.get("updatedAt"), request_timestamp
        )
        ET.SubElement(item, "link").text = magnet
        ET.SubElement(item, "category").text = category_name
        ET.SubElement(item, "size").text = str(size)
        ET.SubElement(
            item,
            "enclosure",
            {
                "url": magnet,
                "length": str(size),
                "type": "application/x-bittorrent;x-scheme-handler/magnet",
            },
        )

        _add_torznab_attribute(item, "category", category_id)
        _add_torznab_attribute(item, "size", size)
        _add_torznab_attribute(item, "infohash", info_hash)
        _add_torznab_attribute(item, "magneturl", magnet)
        _add_torznab_attribute(item, "imdb", imdb_digits)

        seeders = torrent.get("seeders")
        if type(seeders) is int and seeders >= 0:
            _add_torznab_attribute(item, "seeders", seeders)
        if media_type == "series":
            if result.search_season is not None:
                _add_torznab_attribute(item, "season", result.search_season)
            if result.search_episode is not None:
                _add_torznab_attribute(item, "episode", result.search_episode)

        parsed = torrent.get("parsed")
        if parsed is not None:
            parsed_year = getattr(parsed, "year", None)
            if type(parsed_year) is int and parsed_year > 0:
                _add_torznab_attribute(item, "year", parsed_year)
            languages = getattr(parsed, "languages", None)
            if isinstance(languages, list):
                language = ",".join(
                    _clean_xml(value)
                    for value in languages
                    if isinstance(value, str) and value
                )
                if language:
                    _add_torznab_attribute(item, "language", language)
            resolution = str(getattr(parsed, "resolution", "") or "")
            if resolution and resolution.casefold() != "unknown":
                _add_torznab_attribute(item, "resolution", resolution)

        items.append(item)

    return items


def serialize_feed(
    result: MediaSearchResult | None,
    media_type: str | None,
    offset: int,
    limit: int,
    link: str,
    *,
    request_timestamp: float | None = None,
) -> tuple[bytes, int, int]:
    timestamp = request_timestamp if request_timestamp is not None else time.time()
    items = (
        _serializable_items(result, media_type, timestamp)
        if result is not None and media_type is not None
        else []
    )
    total = len(items)
    page = items[offset : offset + limit]

    rss = ET.Element(
        "rss",
        {
            "version": "2.0",
            "xmlns:torznab": TORZNAB_NAMESPACE,
            "xmlns:newznab": NEWZNAB_NAMESPACE,
        },
    )
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "Comet"
    ET.SubElement(channel, "description").text = "Comet torrent results"
    ET.SubElement(channel, "link").text = _clean_xml(link)
    ET.SubElement(channel, "language").text = "en"
    ET.SubElement(
        channel,
        "newznab:response",
        {"offset": str(offset), "total": str(total)},
    )
    channel.extend(page)
    return _xml_bytes(rss), total, len(page)


def serialize_caps() -> bytes:
    available = "no" if settings.DISABLE_TORRENT_STREAMS else "yes"
    caps = ET.Element("caps")
    ET.SubElement(caps, "server", {"version": "1.3", "title": "Comet"})
    ET.SubElement(
        caps,
        "limits",
        {"max": str(MAX_RESULTS), "default": str(MAX_RESULTS)},
    )
    searching = ET.SubElement(caps, "searching")
    ET.SubElement(
        searching,
        "search",
        {"available": available, "supportedParams": "q"},
    )
    ET.SubElement(
        searching,
        "tv-search",
        {
            "available": available,
            "supportedParams": "q,imdbid,season,ep",
        },
    )
    ET.SubElement(
        searching,
        "movie-search",
        {"available": available, "supportedParams": "q,imdbid"},
    )
    categories = ET.SubElement(caps, "categories")
    ET.SubElement(categories, "category", {"id": "2000", "name": "Movies"})
    ET.SubElement(categories, "category", {"id": "5000", "name": "TV"})
    return _xml_bytes(caps)


def serialize_error(code: int, description: str) -> bytes:
    return _xml_bytes(
        ET.Element(
            "error",
            {"code": str(code), "description": _clean_xml(description)},
        )
    )


def _xml_response(
    request: Request,
    content: bytes,
    *,
    feed: bool,
    cache_policy=None,
):
    content_type = (
        "application/rss+xml; charset=utf-8"
        if feed
        else "application/xml; charset=utf-8"
    )
    if not settings.HTTP_CACHE_ENABLED:
        return Response(content=content, headers={"Content-Type": content_type})

    policy = cache_policy or CachePolicies.empty_results()
    cache_control = policy.build()
    etag = generate_etag(content)
    if check_etag_match(request, etag):
        return not_modified_response(etag, cache_control=cache_control)
    return Response(
        content=content,
        headers={
            "Content-Type": content_type,
            "Cache-Control": cache_control,
            "ETag": etag,
            "Vary": "Accept, Accept-Encoding",
        },
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
        if constraint.unsupported_only:
            content, _, _ = serialize_feed(
                None, None, query.offset, query.limit, _request_link(request)
            )
            return _xml_response(
                request,
                content,
                feed=True,
                cache_policy=CachePolicies.empty_results(),
            )

        function_type = _function_media_type(query.function)
        if (
            function_type is not None
            and constraint.media_type is not None
            and function_type != constraint.media_type
        ):
            content, _, _ = serialize_feed(
                None, None, query.offset, query.limit, _request_link(request)
            )
            return _xml_response(
                request,
                content,
                feed=True,
                cache_policy=CachePolicies.empty_results(),
            )

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
            content, _, _ = serialize_feed(
                None, None, query.offset, query.limit, _request_link(request)
            )
            return _xml_response(
                request,
                content,
                feed=True,
                cache_policy=CachePolicies.empty_results(),
            )

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

        empty_statuses = {
            MediaSearchStatus.UNRELEASED,
            MediaSearchStatus.METADATA_UNAVAILABLE,
        }
        if result.status in empty_statuses:
            content, total, page_count = serialize_feed(
                None, None, query.offset, query.limit, _request_link(request)
            )
        else:
            content, total, page_count = serialize_feed(
                result,
                target.media_type,
                query.offset,
                query.limit,
                _request_link(request),
            )

        metrics.observe_stream(
            target.media_type,
            "torznab",
            result.cache_state,
            "empty" if total == 0 else "success",
            page_count,
        )
        return _xml_response(
            request,
            content,
            feed=True,
            cache_policy=(
                CachePolicies.streams()
                if total
                else CachePolicies.empty_results()
            ),
        )
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
