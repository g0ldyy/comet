"""Public AnimeTosho Torznab discovery with dual torrent/NZB mapping."""

import asyncio
import base64
import hashlib
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import aiohttp

from comet.core.provider_json import is_success_status
from comet.core.sources import (
    MAX_REMOTE_GUID_LENGTH,
    TORRENT_PROVIDER_KINDS,
    LocatorKind,
    LocatorPolicy,
    ReleaseCandidate,
    TorrentLocator,
    TransportKind,
)
from comet.discovery.adapters.newznab import (
    NewznabError,
    NewznabFeedItem,
    _bounded_decimal,
    _parse_caps,
    _read_bounded,
    map_newznab_nzb_item,
    newznab_item_published_at_ms,
    newznab_item_size,
    parse_newznab_feed,
)
from comet.discovery.models import DiscoveryBatch, DiscoveryContext, MediaQuery
from comet.playback.base import Actionability, ProviderStatus, Readiness
from comet.usenet.outbound import OutboundUrlError, fetch_http_bytes

_FEED_URL = "https://feed.animetosho.org/api"
_MAX_FEED_BYTES = 2 * 1024 * 1024
_MAX_NZB_BYTES = 150 * 1024 * 1024
_INFO_HASH = re.compile(r"[0-9a-fA-F]{40}$")
_MAX_SEEDERS = (1 << 31) - 1
PUBLIC_GOVERNOR_SCOPE = hashlib.sha256(b"comet-public-provider-v1\0animetosho").digest()


@dataclass(frozen=True, slots=True)
class AnimeToshoConfiguration:
    configuration_id: str
    max_results: int = 150
    page_size: int = 75

    def __post_init__(self):
        if (
            not isinstance(self.configuration_id, str)
            or not 1 <= len(self.configuration_id) <= 64
            or any(ord(character) < 32 for character in self.configuration_id)
        ):
            raise ValueError("AnimeTosho configuration ID is invalid")
        if (
            isinstance(self.max_results, bool)
            or not isinstance(self.max_results, int)
            or not 1 <= self.max_results <= 200
        ):
            raise ValueError("AnimeTosho result limit is invalid")
        if (
            isinstance(self.page_size, bool)
            or not isinstance(self.page_size, int)
            or not 1 <= self.page_size <= min(200, self.max_results)
        ):
            raise ValueError("AnimeTosho page size is invalid")


def animetosho_configuration(
    configuration_id: str,
    options: object,
) -> AnimeToshoConfiguration:
    if not isinstance(options, dict):
        raise ValueError("AnimeTosho options must be an object")
    return AnimeToshoConfiguration(
        configuration_id,
        max_results=options.get("maxResults", 150),
        page_size=options.get("pageSize", 75),
    )


class AnimeToshoAdapter:
    def __init__(
        self,
        session,
        configuration: AnimeToshoConfiguration,
        *,
        governor=None,
    ):
        self._session = session
        self._configuration = configuration
        self._governor = governor
        self._feed_cache: tuple[str, float, tuple[NewznabFeedItem, ...]] | None = None
        self._feed_tasks: dict[
            tuple[str, float | None, asyncio.Event | None],
            asyncio.Task[tuple[tuple[NewznabFeedItem, ...], bool]],
        ] = {}

    async def validate_config(self) -> ProviderStatus:
        try:
            payload = await self._request(
                {"t": "caps", "apikey": "0"},
                maximum=256 * 1024,
            )
            caps = _parse_caps(payload)
            if "search" not in caps.operations:
                raise NewznabError("provider_caps_incompatible")
        except NewznabError as exc:
            return ProviderStatus(
                Readiness.RETRYABLE_FAILURE,
                Actionability.NONE,
                code=exc.code,
            )
        except (aiohttp.ClientError, TimeoutError):
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
        branches = context.branches & {"bittorrent", "usenet"}
        if not branches:
            return DiscoveryBatch()
        search_text = next(
            (
                normalized
                for value in query.title_aliases
                if (normalized := value.strip())
            ),
            None,
        )
        if search_text is None:
            return DiscoveryBatch(coverage=frozenset(branches))

        items, complete = await self._feed_items(search_text, context)
        candidates = []
        for item in items:
            if "bittorrent" in branches:
                torrent = self._torrent_candidate(item, query)
                if torrent is not None:
                    candidates.append(torrent)
            if "usenet" in branches:
                nzb = self._nzb_candidate(item, query, context)
                if nzb is not None:
                    candidates.append(nzb)
        return DiscoveryBatch(
            tuple(candidates),
            coverage=frozenset(branches) if complete else frozenset(),
        )

    async def grab(self, remote_guid: str) -> bytes:
        url = _nzb_url(remote_guid)
        document = await self._request_url(
            url,
            maximum=_MAX_NZB_BYTES,
            accept="application/x-nzb, application/xml, text/xml",
            operation="animetosho_grab",
            request_limit=8,
        )
        return document

    async def _feed_items(
        self,
        search_text: str,
        context: DiscoveryContext,
    ) -> tuple[tuple[NewznabFeedItem, ...], bool]:
        loop = asyncio.get_running_loop()
        if context.cancelled() or (
            context.hard_deadline is not None and loop.time() >= context.hard_deadline
        ):
            return (), False
        cached = self._feed_cache
        if cached is not None and cached[0] == search_text and loop.time() < cached[1]:
            return cached[2], True
        task_key = (search_text, context.hard_deadline, context.cancellation)
        task = self._feed_tasks.get(task_key)
        if task is None:
            task = asyncio.create_task(self._load_feed_items(search_text, context))
            self._feed_tasks[task_key] = task

            def discard(completed):
                self._feed_tasks.pop(task_key, None)
                if not completed.cancelled():
                    completed.exception()

            task.add_done_callback(discard)
        return await asyncio.shield(task)

    async def _load_feed_items(
        self,
        search_text: str,
        context: DiscoveryContext,
    ) -> tuple[tuple[NewznabFeedItem, ...], bool]:
        loop = asyncio.get_running_loop()
        results = []
        offset = 0
        complete = True
        while offset < self._configuration.max_results:
            if context.cancelled() or (
                context.hard_deadline is not None
                and loop.time() >= context.hard_deadline
            ):
                complete = False
                break
            limit = min(
                self._configuration.page_size,
                self._configuration.max_results - offset,
            )
            payload = await self._request(
                {
                    "t": "search",
                    "q": search_text,
                    "cat": "5070",
                    "offset": str(offset),
                    "limit": str(limit),
                    "apikey": "0",
                },
                maximum=_MAX_FEED_BYTES,
            )
            items, total = parse_newznab_feed(payload)
            items = items[:limit]
            results.extend(items)
            consumed = len(items)
            if consumed == 0:
                if total is not None and offset < total:
                    complete = False
                break
            if total is not None and offset + consumed >= total:
                break
            if consumed < limit:
                if total is not None:
                    complete = False
                break
            offset += limit
        result = tuple(results)
        if complete:
            self._feed_cache = (search_text, loop.time() + 1.0, result)
        return result, complete

    def _torrent_candidate(
        self,
        item: NewznabFeedItem,
        query: MediaQuery,
    ) -> ReleaseCandidate | None:
        info_hash = item.attributes.get("infohash")
        title = item.fields.get("title")
        if (
            info_hash is None
            or not _INFO_HASH.fullmatch(info_hash)
            or not title
            or len(title) > 1_024
        ):
            return None
        normalized_hash = info_hash.lower()
        digest = hashlib.sha256(
            f"animetosho:torrent:{normalized_hash}".encode()
        ).hexdigest()
        seeders_text = item.attributes.get("seeders")
        seeders = _bounded_decimal(seeders_text, maximum=_MAX_SEEDERS)
        return ReleaseCandidate(
            candidate_id=f"at1:torrent:{digest}",
            media_id=query.media_id,
            scope=query.scope,
            transport=TransportKind.BITTORRENT,
            title=title,
            locators=(
                TorrentLocator(
                    locator_id=f"at1:torrent-locator:{digest}",
                    kind=LocatorKind.TORRENT,
                    policy=LocatorPolicy(TORRENT_PROVIDER_KINDS),
                    info_hash=normalized_hash,
                ),
            ),
            size=newznab_item_size(item),
            published_at_ms=newznab_item_published_at_ms(item),
            source="AnimeTosho",
            transport_stats={"seeders": seeders},
        )

    def _nzb_candidate(
        self,
        item: NewznabFeedItem,
        query: MediaQuery,
        context: DiscoveryContext,
    ) -> ReleaseCandidate | None:
        token = next(
            (
                token
                for value in item.enclosures
                if "bittorrent" not in value.media_type
                if (token := _nzb_token(value.url)) is not None
            ),
            None,
        )
        if token is None:
            return None
        return map_newznab_nzb_item(
            item,
            query,
            configuration_id=self._configuration.configuration_id,
            label="AnimeTosho",
            context=context,
            remote_id=token,
            identity_namespace="animetosho",
        )

    async def _request(
        self,
        params: dict[str, str],
        *,
        maximum: int,
    ) -> bytes:
        return await self._request_url(
            _FEED_URL,
            params=params,
            maximum=maximum,
            accept="application/xml, text/xml, application/rss+xml",
            operation="animetosho_search",
            request_limit=4,
        )

    async def _request_url(
        self,
        url: str,
        *,
        maximum: int,
        accept: str,
        operation: str,
        request_limit: int,
        params: dict[str, str] | None = None,
    ) -> bytes:
        if self._governor is not None:
            permit = await self._governor.acquire_window(
                PUBLIC_GOVERNOR_SCOPE,
                operation,
                limit=request_limit,
                window_seconds=1,
            )
            if permit is None:
                raise NewznabError("provider_limit_exhausted", retryable=True)
        if params is None:
            try:
                return await fetch_http_bytes(
                    url,
                    max_bytes=maximum,
                    headers={"Accept": accept, "User-Agent": "Comet"},
                )
            except OutboundUrlError as exc:
                raise _outbound_error(exc.http_status) from exc
        async with self._session.get(
            url,
            params=params,
            headers={
                "Accept": accept,
                "Accept-Encoding": "identity",
                "User-Agent": "Comet",
            },
            allow_redirects=False,
        ) as response:
            if response.status == 429:
                raise NewznabError("provider_limit_exhausted", retryable=True)
            if response.status >= 500:
                raise NewznabError("provider_unavailable", retryable=True)
            if not is_success_status(response.status):
                raise NewznabError("provider_response_invalid")
            return await _read_bounded(response, maximum)


def _nzb_token(value: object) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 4_096:
        return None
    try:
        parsed = urlsplit(value)
        _ = parsed.port
        encoded = base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")
    except (UnicodeError, ValueError):
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in value
        )
    ):
        return None
    token = f"atn1:{encoded}"
    return token if len(token) <= MAX_REMOTE_GUID_LENGTH else None


def _nzb_url(token: object) -> str:
    if not isinstance(token, str) or not token.startswith("atn1:") or not token[5:]:
        raise ValueError("AnimeTosho NZB reference is invalid")
    encoded = token[5:]
    if any(
        character
        not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        for character in encoded
    ):
        raise ValueError("AnimeTosho NZB reference is invalid")
    try:
        decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("AnimeTosho NZB reference is invalid") from exc
    if _nzb_token(decoded) != token:
        raise ValueError("AnimeTosho NZB reference is invalid")
    return decoded


def _outbound_error(status: int | None) -> NewznabError:
    if status == 429:
        return NewznabError("provider_limit_exhausted", retryable=True)
    if status is not None and status >= 500:
        return NewznabError("provider_unavailable", retryable=True)
    return NewznabError("provider_response_invalid")
