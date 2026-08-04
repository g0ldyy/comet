"""Shared URL policy for operator-approved Usenet HTTP services."""

from collections.abc import Collection
from urllib.parse import urlsplit, urlunsplit

from comet.usenet.outbound import configured_http_origin


class UpstreamUrlError(ValueError):
    """A configuration-safe URL failure with a user-facing capability code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def normalize_upstream_base_url(
    value: object,
    *,
    allowed_http_origins: Collection[str] | None,
) -> str:
    """Normalize a base URL and enforce explicit operator approval for HTTP."""
    if not isinstance(value, str) or not value:
        raise UpstreamUrlError("configuration_invalid")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise UpstreamUrlError("configuration_invalid") from None
    if (
        len(encoded) > 1024
        or "\\" in value
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in value
        )
    ):
        raise UpstreamUrlError("configuration_invalid")
    try:
        parsed = urlsplit(value)
        origin = configured_http_origin(value)
    except ValueError:
        raise UpstreamUrlError("configuration_invalid") from None
    if (
        allowed_http_origins is not None
        and parsed.scheme == "http"
        and origin not in allowed_http_origins
    ):
        raise UpstreamUrlError("private_upstream_origin_required")
    return urlunsplit(
        (parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip("/"), "", "")
    )
