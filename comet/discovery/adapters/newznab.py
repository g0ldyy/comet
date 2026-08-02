"""Caps-aware Newznab discovery shared by indexers, Hydra and Prowlarr."""

import asyncio
import hashlib
import time
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

import aiohttp
from curl_cffi.requests import RequestsError

from comet.core.provider_json import is_success_status
from comet.core.sources import (
    MAX_REMOTE_GUID_LENGTH,
    REAL_NZB_PROVIDER_KINDS,
    LocatorKind,
    LocatorPolicy,
    RealNzbRef,
    ReleaseCandidate,
    TransportKind,
)
from comet.discovery.models import DiscoveryBatch, DiscoveryContext, MediaQuery
from comet.playback.base import Actionability, ProviderStatus, Readiness
from comet.usenet.easynews import bounded_retry_after
from comet.usenet.outbound import (
    OutboundUrlError,
    configured_http_origin,
    validate_http_url,
)
from comet.utils.network_manager import (
    DiscoveryResponseTooLarge,
    network_manager,
)

_MAX_SIGNED_64 = (1 << 63) - 1
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_USER_AGENT_MODES = frozenset({"stealth", "browser", "custom"})


class NewznabError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        retry_after: int | None = None,
        auth_failed: bool = False,
    ):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after
        self.auth_failed = auth_failed


@dataclass(frozen=True, slots=True)
class NewznabAccount:
    endpoint: str
    api_key: str
    configuration_id: str
    label: str = "Newznab"
    user_agent_mode: str = "custom"
    query_user_agent: str = "Comet"
    grab_user_agent: str = "Comet"
    max_results: int = 100
    page_size: int = 100
    requests_per_second: int = 2
    daily_query_limit: int = 1_000
    daily_grab_limit: int = 100

    def __post_init__(self):
        _endpoint(self.endpoint)
        _text(self.api_key, "Newznab API key", 1_024)
        _text(self.configuration_id, "Newznab configuration ID", 64)
        _text(self.label, "Newznab label", 64)
        if self.user_agent_mode not in _USER_AGENT_MODES:
            raise ValueError("Newznab User-Agent mode is invalid")
        if self.user_agent_mode != "stealth":
            _text(self.query_user_agent, "Newznab query user agent", 512)
            _text(self.grab_user_agent, "Newznab grab user agent", 512)
        _integer(self.max_results, "Newznab result limit", 1, 1_000)
        _integer(self.page_size, "Newznab page size", 1, 100)
        _integer(self.requests_per_second, "Newznab RPS", 1, 100)
        _integer(self.daily_query_limit, "Newznab daily query limit", 1, 1_000_000)
        _integer(self.daily_grab_limit, "Newznab daily grab limit", 1, 1_000_000)


@dataclass(frozen=True, slots=True)
class NewznabCaps:
    operations: dict[str, frozenset[str]]
    categories: frozenset[int]


@dataclass(frozen=True, slots=True)
class NewznabEnclosure:
    url: str
    media_type: str
    length: str | None


@dataclass(frozen=True, slots=True)
class NewznabFeedItem:
    fields: dict[str, str]
    attributes: dict[str, str]
    enclosures: tuple[NewznabEnclosure, ...]


class NewznabAdapter:
    def __init__(
        self,
        session,
        account: NewznabAccount,
        *,
        governor=None,
        governor_scope: bytes | None = None,
    ):
        self._account = account
        self._stealth = account.user_agent_mode == "stealth"
        self._request_session = (
            network_manager.get_client(
                "newznab",
                impersonate="chrome",
                discard_cookies=True,
                proxy_ethos="always",
                proxy_setting="USER_PROVIDED_PROXY_URL",
            )
            if self._stealth
            else session
        )
        self._governor = governor
        self._governor_scope = governor_scope
        self._caps: tuple[float, NewznabCaps] | None = None
        self._caps_lock = asyncio.Lock()

    async def validate_config(self) -> ProviderStatus:
        try:
            await self._caps_for_search(force=True)
        except NewznabError as exc:
            return ProviderStatus(
                (
                    Readiness.TERMINAL_FAILURE
                    if exc.auth_failed
                    else Readiness.RETRYABLE_FAILURE
                ),
                Actionability.NONE,
                code=exc.code,
                auth_failed=exc.auth_failed,
            )
        except (aiohttp.ClientError, RequestsError, TimeoutError):
            return ProviderStatus(
                Readiness.RETRYABLE_FAILURE,
                Actionability.NONE,
                code="provider_unavailable",
            )
        return ProviderStatus(Readiness.READY, Actionability.NONE)

    async def search(
        self,
        query: MediaQuery,
        context: DiscoveryContext,
    ) -> DiscoveryBatch:
        if "usenet" not in context.branches:
            return DiscoveryBatch()
        loop = asyncio.get_running_loop()
        if context.cancelled() or (
            context.hard_deadline is not None and loop.time() >= context.hard_deadline
        ):
            return DiscoveryBatch()
        caps = await self._caps_for_search()
        base_params = _query_params(query, caps)
        candidates = []
        offset = 0
        total = None
        complete = True
        while offset < self._account.max_results:
            if context.cancelled() or (
                context.hard_deadline is not None
                and loop.time() >= context.hard_deadline
            ):
                complete = False
                break
            limit = min(
                self._account.page_size,
                self._account.max_results - offset,
            )
            payload = await self._request(
                {
                    **base_params,
                    "offset": str(offset),
                    "limit": str(limit),
                },
                maximum=2 * 1024 * 1024,
            )
            page, page_total, consumed = _parse_results(
                payload,
                query,
                self._account,
                context,
                maximum_items=limit,
            )
            candidates.extend(page[: self._account.max_results - len(candidates)])
            total = page_total if page_total is not None else total
            if consumed == 0:
                break
            offset += consumed
            if total is not None and offset >= total:
                break
            if total is None and consumed < limit:
                break
        return DiscoveryBatch(
            tuple(candidates),
            coverage=frozenset({"usenet"}) if complete else frozenset(),
        )

    async def grab(self, remote_guid: str) -> bytes:
        if not _opaque_identifier(remote_guid):
            raise ValueError("Newznab replay identifier is invalid")
        payload = await self._request(
            {"t": "get", "id": remote_guid},
            maximum=150 * 1024 * 1024,
            operation="grab",
            user_agent=self._account.grab_user_agent,
        )
        stripped = payload.lstrip()
        if stripped.startswith(b"<error"):
            _xml_root(payload)
        return payload

    async def _caps_for_search(self, *, force: bool = False) -> NewznabCaps:
        now = time.monotonic()
        if not force and self._caps is not None and now < self._caps[0]:
            return self._caps[1]
        async with self._caps_lock:
            now = time.monotonic()
            if not force and self._caps is not None and now < self._caps[0]:
                return self._caps[1]
            caps = _parse_caps(await self._request({"t": "caps"}, maximum=256 * 1024))
            self._caps = (now + 6 * 60 * 60, caps)
            return caps

    async def _request(
        self,
        params: dict[str, str],
        *,
        maximum: int,
        operation: str = "query",
        user_agent: str | None = None,
    ) -> bytes:
        if self._governor is not None:
            while (
                await self._governor.acquire_window(
                    self._governor_scope,
                    "newznab_rps",
                    limit=self._account.requests_per_second,
                    window_seconds=1,
                )
                is None
            ):
                await asyncio.sleep(1 - time.time() % 1)
            if (
                await self._governor.acquire_window(
                    self._governor_scope,
                    f"newznab_{operation}_daily",
                    limit=(
                        self._account.daily_grab_limit
                        if operation == "grab"
                        else self._account.daily_query_limit
                    ),
                    window_seconds=24 * 60 * 60,
                )
                is None
            ):
                raise NewznabError("provider_limit_exhausted", retryable=True)
        headers = {
            "Accept": "application/xml, text/xml, application/rss+xml",
            **_origin_headers(self._account.endpoint),
        }
        if not self._stealth:
            headers["User-Agent"] = user_agent or self._account.query_user_agent
        url = self._account.endpoint
        request_params: dict[str, str] | None = {
            **params,
            "apikey": self._account.api_key,
        }
        allowed_private_origins = frozenset({configured_http_origin(url)})
        for redirect in range(4):
            request_options: dict[str, object] = {
                "headers": headers,
                "allow_redirects": False,
            }
            if request_params is not None:
                request_options["params"] = request_params
            if self._stealth:
                request_options["maximum_body_bytes"] = maximum
            try:
                async with self._request_session.get(
                    url,
                    **request_options,
                ) as response:
                    if response.status in _REDIRECT_STATUSES:
                        if operation != "grab" or redirect == 3:
                            raise NewznabError("provider_redirect_invalid")
                        location = response.headers.get("Location")
                        if not location:
                            raise NewznabError("provider_redirect_invalid")
                        try:
                            target = await validate_http_url(
                                urljoin(url, location),
                                allowed_private_origins=allowed_private_origins,
                            )
                        except (OutboundUrlError, TypeError, ValueError) as exc:
                            raise NewznabError("provider_redirect_invalid") from exc
                        url = target.url
                        request_params = None
                        headers = {
                            name: value
                            for name, value in headers.items()
                            if name in {"Accept", "User-Agent"}
                        }
                        continue
                    if not is_success_status(response.status):
                        raise _status_error(
                            response.status,
                            retry_after=response.headers.get("Retry-After"),
                        )
                    return (
                        await response.read()
                        if self._stealth
                        else await _read_bounded(response, maximum)
                    )
            except DiscoveryResponseTooLarge as exc:
                raise NewznabError("provider_response_too_large") from exc
        raise NewznabError("provider_redirect_invalid")


def newznab_account_from_options(
    options: object,
    configuration_id: str,
    *,
    label: str = "Newznab",
) -> NewznabAccount:
    if not isinstance(options, dict):
        raise ValueError("Newznab options must be an object")
    return NewznabAccount(
        endpoint=options.get("endpoint"),
        api_key=options.get("apiKey"),
        configuration_id=configuration_id,
        label=label,
        user_agent_mode=options.get("userAgentMode", "stealth"),
        query_user_agent=options.get("queryUserAgent", "Comet"),
        grab_user_agent=options.get("grabUserAgent", "Comet"),
        max_results=options.get("maxResults", 100),
        page_size=options.get("pageSize", 100),
        requests_per_second=options.get("requestsPerSecond", 2),
        daily_query_limit=options.get("dailyQueryLimit", 1_000),
        daily_grab_limit=options.get("dailyGrabLimit", 100),
    )


async def _read_bounded(response, maximum: int) -> bytes:
    body = bytearray()
    async for chunk in response.content.iter_chunked(64 * 1024):
        body.extend(chunk)
        if len(body) > maximum:
            raise NewznabError("provider_response_too_large")
    return bytes(body)


def _xml_root(payload: bytes):
    lowered = payload.lower()
    if not payload or b"<!entity" in lowered:
        raise NewznabError("provider_response_invalid")
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise NewznabError("provider_response_invalid") from exc
    for element in root.iter():
        if _local_name(element.tag) == "error":
            raise _newznab_protocol_error(element.attrib.get("code"))
    return root


def _newznab_protocol_error(code: str | None) -> NewznabError:
    if code == "100":
        return NewznabError("api_key_missing", auth_failed=True)
    if code == "101":
        return NewznabError("api_key_invalid", auth_failed=True)
    if code == "102":
        return NewznabError("account_suspended", auth_failed=True)
    if code == "103":
        return NewznabError("provider_unavailable", retryable=True)
    if code == "104":
        return NewznabError("provider_limit_exhausted", retryable=True)
    return NewznabError("provider_error")


def _status_error(
    status: int | None,
    *,
    fallback: str = "provider_response_invalid",
    retry_after: str | None = None,
) -> NewznabError:
    if status in {401, 403}:
        return NewznabError("api_key_invalid", auth_failed=True)
    if status == 429:
        return NewznabError(
            "provider_limit_exhausted",
            retryable=True,
            retry_after=bounded_retry_after(retry_after),
        )
    if status is not None and status >= 500:
        return NewznabError("provider_unavailable", retryable=True)
    return NewznabError(fallback)


def _parse_caps(payload: bytes) -> NewznabCaps:
    root = _xml_root(payload)
    operations = {}
    searching = next(
        (item for item in root.iter() if _local_name(item.tag) == "searching"),
        None,
    )
    if searching is not None:
        for element in searching:
            name = _local_name(element.tag).replace("-", "").replace("_", "")
            if name not in {"search", "tvsearch", "moviesearch"}:
                continue
            if element.attrib.get("available", "yes").strip().lower() in {
                "no",
                "false",
                "0",
            }:
                continue
            operations[name] = frozenset(
                parameter.strip().lower().replace("_", "")
                for parameter in element.attrib.get("supportedParams", "").split(",")
                if parameter.strip()
            )
    if not operations:
        raise NewznabError("provider_caps_incompatible")
    categories = frozenset(
        identifier
        for element in root.iter()
        if _local_name(element.tag) == "category"
        and (
            identifier := _bounded_decimal(
                element.attrib.get("id"),
                maximum=_MAX_SIGNED_64,
            )
        )
        is not None
    )
    return NewznabCaps(operations, categories)


def _query_params(query: MediaQuery, caps: NewznabCaps) -> dict[str, str]:
    if query.media_type == "movie" and "moviesearch" in caps.operations:
        operation = "moviesearch"
    elif query.media_type == "series" and "tvsearch" in caps.operations:
        operation = "tvsearch"
    elif "search" in caps.operations:
        operation = "search"
    else:
        raise NewznabError("provider_caps_incompatible")
    supported = caps.operations[operation]
    params = {"t": "movie" if operation == "moviesearch" else operation}
    identifier = _identifier_parameter(query.media_id, supported)
    search_text = next(
        (alias.strip() for alias in query.title_aliases if alias.strip()),
        None,
    )
    if identifier is not None:
        params[identifier[0]] = identifier[1]
    elif search_text is not None and ("q" in supported or operation == "search"):
        params["q"] = search_text
    else:
        raise NewznabError("provider_query_unsupported")
    if query.year is not None and "year" in supported:
        params["year"] = str(query.year)
    if query.air_date is not None and {"season", "ep"} <= supported:
        year, month, day = query.air_date.split("-")
        params.update({"season": year, "ep": f"{month}/{day}"})
    else:
        if query.season is not None and "season" in supported:
            params["season"] = str(query.season)
        if (
            query.episode is not None
            and query.search_scope not in {"season_pack", "series_pack"}
            and "ep" in supported
        ):
            params["ep"] = str(query.episode)
    category = 2000 if query.media_type == "movie" else 5000
    params["cat"] = str(category)
    return params


def _identifier_parameter(
    media_id: str,
    supported: frozenset[str],
) -> tuple[str, str] | None:
    candidates = []
    if media_id.startswith("kitsu:"):
        candidates.extend((("kitsu", media_id[6:]), ("kitsuid", media_id[6:])))
    elif media_id.startswith("tt"):
        candidates.append(("imdbid", media_id[2:]))
    elif ":" in media_id:
        scheme, value = media_id.split(":", 1)
        if scheme in {"tmdb", "tvdb"}:
            candidates.append((f"{scheme}id", value))
    return next(
        ((name, value) for name, value in candidates if name in supported and value),
        None,
    )


def _parse_results(
    payload: bytes,
    query: MediaQuery,
    account: NewznabAccount,
    context: DiscoveryContext,
    *,
    maximum_items: int,
) -> tuple[list[ReleaseCandidate], int | None, int]:
    items, total = parse_newznab_feed(payload)
    items = items[:maximum_items]
    candidates = [
        candidate
        for item in items
        if (
            candidate := map_newznab_nzb_item(
                item,
                query,
                configuration_id=account.configuration_id,
                label=account.label,
                context=context,
            )
        )
        is not None
    ]
    return candidates, total, len(items)


def parse_newznab_feed(
    payload: bytes,
) -> tuple[tuple[NewznabFeedItem, ...], int | None]:
    """Parse one bounded Newznab/Torznab feed for all transport mappers."""
    root = _xml_root(payload)
    total = next(
        (
            parsed_total
            for element in root.iter()
            if _local_name(element.tag) == "response"
            and (
                parsed_total := _bounded_decimal(
                    element.attrib.get("total"),
                    maximum=_MAX_SIGNED_64,
                )
            )
            is not None
        ),
        None,
    )
    items = []
    for item in root.iter():
        if _local_name(item.tag) != "item":
            continue
        fields = {}
        attributes = {}
        enclosures = []
        for child in item:
            name = _local_name(child.tag)
            if name == "attr":
                attributes[child.attrib.get("name", "").lower()] = child.attrib.get(
                    "value",
                    "",
                )
            elif name == "enclosure":
                enclosures.append(
                    NewznabEnclosure(
                        child.attrib.get("url", ""),
                        child.attrib.get("type", "").lower(),
                        child.attrib.get("length"),
                    )
                )
            else:
                fields[name] = child.text or ""
        items.append(NewznabFeedItem(fields, attributes, tuple(enclosures)))
    return tuple(items), total


def map_newznab_nzb_item(
    item: NewznabFeedItem,
    query: MediaQuery,
    *,
    configuration_id: str,
    label: str,
    context: DiscoveryContext,
    remote_id: str | None = None,
    identity_namespace: str = "newznab",
) -> ReleaseCandidate | None:
    """Map one parsed feed item to a replayable real-NZB candidate."""
    fields = item.fields
    attributes = item.attributes
    if remote_id is None:
        remote_id = _remote_identifier(
            attributes,
            *(enclosure.url for enclosure in item.enclosures),
            fields.get("link"),
            permalink=fields.get("guid"),
        )
    elif not _opaque_identifier(remote_id):
        raise ValueError("Newznab replay identifier is invalid")
    title = fields.get("title")
    if remote_id is None or not title or len(title) > 1_024:
        return None
    size = newznab_item_size(item)
    published_at_ms = newznab_item_published_at_ms(item)
    digest = hashlib.sha256(
        f"{identity_namespace}:{configuration_id}:{remote_id}".encode()
    ).hexdigest()
    locator = RealNzbRef(
        locator_id=f"nzb1:locator:{digest}",
        kind=LocatorKind.REAL_NZB,
        policy=LocatorPolicy(
            REAL_NZB_PROVIDER_KINDS,
            owner_configuration_partition=context.account_partition,
        ),
        adapter_configuration_id=configuration_id,
        remote_guid=remote_id,
    )
    return ReleaseCandidate(
        candidate_id=f"nzb1:{digest}",
        media_id=query.media_id,
        scope=query.scope,
        transport=TransportKind.USENET,
        title=title,
        locators=(locator,),
        size=size,
        published_at_ms=published_at_ms,
        source=label,
    )


def newznab_item_size(item: NewznabFeedItem) -> int | None:
    size = _bounded_decimal(
        item.attributes.get("size"),
        maximum=_MAX_SIGNED_64,
    )
    if size is not None and size > 0:
        return size
    for value in item.enclosures:
        size = _bounded_decimal(value.length, maximum=_MAX_SIGNED_64)
        if size is not None and size > 0:
            return size
    return None


def newznab_item_published_at_ms(item: NewznabFeedItem) -> int | None:
    return _published_at_ms(item.fields.get("pubdate"))


def _remote_identifier(
    attributes: dict[str, str],
    *retrieval_values: str | None,
    permalink: str | None,
) -> str | None:
    permalink_url = None
    retrieval_urls = []
    values = (
        ("attribute", attributes.get("guid")),
        ("attribute", attributes.get("id")),
        *(("retrieval", value) for value in retrieval_values),
        ("permalink", permalink),
    )
    for role, value in values:
        if _opaque_identifier(value):
            return value
        parsed = _http_url(value)
        if parsed is None:
            continue
        if role == "retrieval":
            retrieval_urls.append(parsed)
        elif role == "permalink":
            permalink_url = parsed
        query = parse_qs(parsed.query, keep_blank_values=False)
        for key in ("id", "guid"):
            for candidate in query.get(key, ()):
                if _opaque_identifier(candidate):
                    return candidate
    if permalink_url is None:
        return None
    candidate = permalink_url.path.rstrip("/").rsplit("/", 1)[-1]
    if not _opaque_identifier(candidate):
        return None
    for parsed in retrieval_urls:
        token = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        if token != candidate and token.startswith(candidate):
            return candidate
    return None


def _http_url(value: object):
    if not isinstance(value, str) or len(value) > MAX_REMOTE_GUID_LENGTH:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    return parsed if parsed.scheme in {"http", "https"} else None


def _opaque_identifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= MAX_REMOTE_GUID_LENGTH
        and not value.lstrip().lower().startswith(("http://", "https://"))
        and not any(ord(character) < 32 for character in value)
    )


def _bounded_decimal(value: object, *, maximum: int) -> int | None:
    if (
        not isinstance(value, str)
        or not value
        or not value.isascii()
        or not value.isdigit()
        or len(value) > len(str(maximum))
    ):
        return None
    parsed = int(value)
    return parsed if parsed <= maximum else None


def _published_at_ms(value: str | None) -> int | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    timestamp = parsed.timestamp()
    return int(timestamp * 1_000) if timestamp >= 0 else None


def _endpoint(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 2_048:
        raise ValueError("Newznab endpoint is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Newznab endpoint is invalid") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port == 0
        or parsed.fragment
        or any(character.isspace() for character in value)
    ):
        raise ValueError("Newznab endpoint is invalid")
    return value


def _origin_headers(endpoint: str) -> dict[str, str]:
    parsed = urlsplit(endpoint)
    origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    return {"Origin": origin, "Referer": f"{origin}/"}


def _text(value: object, field: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{field} is invalid")
    return value


def _integer(value: object, field: str, minimum: int, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"{field} is invalid")
    return value


def _local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1].lower()
