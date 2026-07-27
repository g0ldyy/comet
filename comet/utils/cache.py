import hashlib
import re
from collections.abc import Sequence
from typing import Any

import orjson
from fastapi import Request, Response

from comet.core.models import settings

NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

_ENTITY_TAG = re.compile(r'(?:W/)?"[\x21\x23-\x7e\x80-\xff]*"')


def _parse_entity_tag_list(value: str) -> list[str] | None:
    tags: list[str] = []
    position = 0
    length = len(value)

    while position < length:
        while position < length and value[position] in " \t":
            position += 1
        match = _ENTITY_TAG.match(value, position)
        if match is None:
            return None
        tags.append(match.group(0))
        position = match.end()
        while position < length and value[position] in " \t":
            position += 1
        if position == length:
            return tags
        if value[position] != ",":
            return None
        position += 1
        if position == length:
            return None

    return tags or None


def _validate_cache_seconds(seconds: int) -> int:
    if type(seconds) is not int or seconds < 0:
        raise ValueError("cache durations must be non-negative integers")
    return seconds


class CacheControl:
    def __init__(self):
        self._directives = []
        self._max_age = None
        self._s_maxage = None
        self._stale_while_revalidate = None
        self._stale_if_error = None

    def public(self):
        """Response can be cached by any cache."""
        self._directives.append("public")
        return self

    def private(self):
        """Response is intended for a single user."""
        self._directives.append("private")
        return self

    def no_cache(self):
        """Cache must revalidate with origin before using cached copy."""
        self._directives.append("no-cache")
        return self

    def no_store(self):
        """Response must not be stored in any cache."""
        self._directives.append("no-store")
        return self

    def must_revalidate(self):
        """Cache must revalidate stale responses."""
        self._directives.append("must-revalidate")
        return self

    def immutable(self):
        """Response will not change during its freshness lifetime."""
        self._directives.append("immutable")
        return self

    def max_age(self, seconds: int):
        """Maximum time response is considered fresh (browser cache)."""
        self._max_age = _validate_cache_seconds(seconds)
        return self

    def s_maxage(self, seconds: int):
        """Maximum time response is fresh for shared caches (CDN/proxy)."""
        self._s_maxage = _validate_cache_seconds(seconds)
        return self

    def stale_while_revalidate(self, seconds: int):
        """Serve stale while revalidating in background."""
        self._stale_while_revalidate = _validate_cache_seconds(seconds)
        return self

    def stale_if_error(self, seconds: int):
        """Serve stale if origin returns error."""
        self._stale_if_error = _validate_cache_seconds(seconds)
        return self

    def build(self):
        """Build the Cache-Control header value."""
        parts = list(self._directives)

        if self._max_age is not None:
            parts.append(f"max-age={self._max_age}")
        if self._s_maxage is not None:
            parts.append(f"s-maxage={self._s_maxage}")
        if self._stale_while_revalidate is not None:
            parts.append(f"stale-while-revalidate={self._stale_while_revalidate}")
        if self._stale_if_error is not None:
            parts.append(f"stale-if-error={self._stale_if_error}")

        return ", ".join(parts)


def _generate_etag(body: bytes):
    hash_digest = hashlib.md5(body, usedforsecurity=False).hexdigest()[:16]
    return f'W/"{hash_digest}"'


def check_etag_match(request: Request, etag: str):
    if_none_match = request.headers.get("If-None-Match")
    if not if_none_match:
        return False

    if if_none_match.strip() == "*":
        return True

    client_etags = _parse_entity_tag_list(if_none_match)
    if client_etags is None or _ENTITY_TAG.fullmatch(etag) is None:
        return False

    normalized_etag = etag.removeprefix("W/")
    return any(
        (client_etag.removeprefix("W/")) == normalized_etag
        for client_etag in client_etags
    )


class CachePolicies:
    @staticmethod
    def streams():
        """
        For all stream results.
        Cache for a short time at CDN, revalidate often.
        """

        ttl = settings.HTTP_CACHE_STREAMS_TTL
        swr = settings.HTTP_CACHE_STALE_WHILE_REVALIDATE

        return (
            CacheControl()
            .public()
            .max_age(ttl // 2)  # Browser cache shorter
            .s_maxage(ttl)  # CDN/proxy cache longer
            .stale_while_revalidate(swr)
            .stale_if_error(300)
        )

    @staticmethod
    def manifest():
        """
        For manifest.json responses.
        Long cache as manifest rarely changes.
        """
        ttl = settings.HTTP_CACHE_MANIFEST_TTL
        swr = settings.HTTP_CACHE_STALE_WHILE_REVALIDATE

        return CacheControl().public().max_age(ttl).stale_while_revalidate(swr)

    @staticmethod
    def configure_page():
        """
        For the /configure page.
        Long cache as the page is mostly static.
        """
        ttl = settings.HTTP_CACHE_CONFIGURE_TTL
        swr = settings.HTTP_CACHE_STALE_WHILE_REVALIDATE

        return CacheControl().public().max_age(ttl).stale_while_revalidate(swr)

    @staticmethod
    def empty_results():
        """
        For empty/temporary responses (no torrents found, processing, errors).
        Short public cache to prevent spam while allowing quick retries.
        """
        return (
            CacheControl()
            .public()
            .max_age(15)  # Browser cache 15 seconds
            .s_maxage(30)  # CDN cache 30 seconds
            .stale_if_error(60)  # Serve stale on error for 1 minute
        )

    @staticmethod
    def no_cache():
        """
        For responses that should never be cached.
        Used for playback redirects, errors, etc.
        """
        return CacheControl().private().no_store().no_cache().max_age(0)


def cached_response(
    request: Request,
    body: bytes,
    *,
    media_type: str,
    cache_policy: CacheControl | None = None,
    vary: Sequence[str] | None = None,
) -> Response:
    """Serve a body as a revalidatable response.

    Returns 304 when the client's `If-None-Match` still matches. With HTTP
    caching disabled the body is served without any validator, so callers never
    have to branch on the setting themselves.
    """
    if not settings.HTTP_CACHE_ENABLED:
        return Response(content=body, media_type=media_type)

    cache_control = (cache_policy or CachePolicies.empty_results()).build()
    etag = _generate_etag(body)
    if check_etag_match(request, etag):
        return Response(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": cache_control},
        )

    headers = {"Cache-Control": cache_control, "ETag": etag}
    if vary:
        headers["Vary"] = ", ".join(vary)
    return Response(content=body, media_type=media_type, headers=headers)


def cached_json_response(
    request: Request,
    content: Any,
    *,
    cache_policy: CacheControl | None = None,
    vary: Sequence[str] | None = None,
) -> Response:
    return cached_response(
        request,
        orjson.dumps(content),
        media_type="application/json",
        cache_policy=cache_policy,
        vary=vary,
    )
