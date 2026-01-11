import hashlib
from typing import Any, Optional

import orjson
from fastapi import Request, Response

from comet.core.models import settings

NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


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
        self._max_age = seconds
        return self

    def s_maxage(self, seconds: int):
        """Maximum time response is fresh for shared caches (CDN/proxy)."""
        self._s_maxage = seconds
        return self

    def stale_while_revalidate(self, seconds: int):
        """Serve stale while revalidating in background."""
        self._stale_while_revalidate = seconds
        return self

    def stale_if_error(self, seconds: int):
        """Serve stale if origin returns error."""
        self._stale_if_error = seconds
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


def generate_etag(data: Any):
    if isinstance(data, bytes):
        content = data
    elif isinstance(data, str):
        content = data.encode("utf-8")
    else:
        content = orjson.dumps(data, option=orjson.OPT_SORT_KEYS)

    hash_digest = hashlib.md5(content, usedforsecurity=False).hexdigest()[:16]
    return f'W/"{hash_digest}"'


def check_etag_match(request: Request, etag: str):
    if_none_match = request.headers.get("If-None-Match")
    if not if_none_match:
        return False

    client_etags = [e.strip() for e in if_none_match.split(",")]

    normalized_etag = etag.replace('W/"', '"')
    for client_etag in client_etags:
        normalized_client = client_etag.replace('W/"', '"')
        if normalized_client == normalized_etag or client_etag == "*":
            return True

    return False


class CachedJSONResponse(Response):
    def __init__(
        self,
        content: Any,
        status_code: int = 200,
        cache_control: Optional[CacheControl] = None,
        etag: Optional[str] = None,
        vary: Optional[list[str]] = None,
        **kwargs,
    ):
        body = orjson.dumps(content)
        super().__init__(
            content=body,
            status_code=status_code,
            media_type="application/json",
            **kwargs,
        )

        if cache_control:
            self.headers["Cache-Control"] = cache_control.build()

        self.headers["ETag"] = etag or generate_etag(body)

        if vary:
            self.headers["Vary"] = ", ".join(vary)


def not_modified_response(etag: str):
    return Response(
        status_code=304,
        headers={
            "ETag": etag,
            "Cache-Control": "must-revalidate",
        },
    )


class CachePolicies:
    @staticmethod
    def public_torrents():
        """
        For public torrent lists (without user config).
        Cache for a short time at CDN, revalidate often.
        """

        ttl = settings.HTTP_CACHE_PUBLIC_STREAMS_TTL
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
    def private_streams():
        """
        For user-specific stream results (with b64config).
        Private cache only, short TTL.
        """

        ttl = settings.HTTP_CACHE_PRIVATE_STREAMS_TTL

        return CacheControl().private().max_age(ttl).must_revalidate()

    @staticmethod
    def manifest():
        """
        For manifest.json responses.
        Very short cache as it can change based on config.
        """
        return CacheControl().private().max_age(60).must_revalidate()

    @staticmethod
    def configure_page():
        """
        For the /configure page.
        Cacheable if no custom HTML, otherwise private.
        """

        if settings.CUSTOM_HEADER_HTML:
            return CacheControl().private().max_age(300)

        return CacheControl().public().max_age(300).s_maxage(3600)

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
