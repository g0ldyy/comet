"""Bounded, public-only HTTP retrieval for user-supplied NZB URLs."""

import asyncio
import ipaddress
from contextlib import asynccontextmanager
from dataclasses import dataclass
from io import BytesIO
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp
from aiohttp.abc import AbstractResolver
from yarl import URL

from comet.core.models import settings
from comet.core.provider_json import is_success_status
from comet.usenet.limits import MAX_NZB_DOCUMENT_BYTES
from comet.utils.http_client import http_client_manager

_HEADER_NAME_CHARACTERS = frozenset(
    "!#$%&'*+-.^_`|~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
)
_ALLOWED_COMMON_HEADERS = frozenset({"accept", "user-agent"})
_FORBIDDEN_REQUEST_HEADERS = frozenset(
    {
        "accept-encoding",
        "connection",
        "content-length",
        "host",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


class OutboundUrlError(ValueError):
    """The supplied URL cannot be safely fetched."""

    def __init__(self, message: str, *, http_status: int | None = None):
        super().__init__(message)
        self.http_status = (
            http_status
            if type(http_status) is int and 100 <= http_status <= 599
            else None
        )


@dataclass(frozen=True, slots=True)
class ValidatedUrl:
    url: str
    scheme: str
    host: str
    port: int
    origin: str
    addresses: tuple[tuple[int, str], ...]


def http_url_with_basic_auth(url: str, username: str, password: str) -> str:
    """Encode HTTP Basic credentials into a client-consumable media URL."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ValueError("HTTP media URL is invalid") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port == 0
        or not isinstance(username, str)
        or not isinstance(password, str)
    ):
        raise ValueError("HTTP media URL is invalid")
    userinfo = f"{quote(username, safe='')}:{quote(password, safe='')}@"
    return urlunsplit(
        (
            parsed.scheme,
            f"{userinfo}{parsed.netloc}",
            parsed.path,
            parsed.query,
            parsed.fragment,
        )
    )


def configured_http_origin(value: str) -> str:
    """Return the canonical origin of one trusted operator-configured URL."""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("configured HTTP origin is invalid") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port == 0
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("configured HTTP origin is invalid")
    effective_port = (
        port if port is not None else (443 if parsed.scheme == "https" else 80)
    )
    host = parsed.hostname.rstrip(".").lower()
    if not host:
        raise ValueError("configured HTTP origin is invalid")
    origin_host = f"[{host}]" if ":" in host else host
    return f"{parsed.scheme}://{origin_host}:{effective_port}"


class PinnedResolver(AbstractResolver):
    def __init__(self, addresses: tuple[tuple[int, str], ...]):
        self._addresses = addresses

    async def resolve(self, host, port=0, family=0):
        return [
            {
                "hostname": host,
                "host": address,
                "port": port,
                "family": address_family,
                "proto": 0,
                "flags": 0,
            }
            for address_family, address in self._addresses
            if family in (0, address_family)
        ]

    async def close(self):
        return None


def _is_public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return address.is_global


def _validated_headers(
    value: dict[str, str] | None,
    *,
    allow_credentials: bool,
) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > 32:
        raise OutboundUrlError("Outbound HTTP headers are invalid")
    result = {}
    normalized_names = set()
    for name, header_value in value.items():
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= 128
            or any(character not in _HEADER_NAME_CHARACTERS for character in name)
            or not isinstance(header_value, str)
            or len(header_value) > 8 * 1024
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in header_value
            )
        ):
            raise OutboundUrlError("Outbound HTTP headers are invalid")
        normalized = name.lower()
        if (
            normalized in normalized_names
            or normalized in _FORBIDDEN_REQUEST_HEADERS
            or not allow_credentials
            and normalized not in _ALLOWED_COMMON_HEADERS
        ):
            raise OutboundUrlError("Outbound HTTP headers are invalid")
        normalized_names.add(normalized)
        result[name] = header_value
    return result


async def validate_http_url(
    value: object,
    *,
    allowed_private_origins: frozenset[str] = frozenset(),
) -> ValidatedUrl:
    """Validate and pin one HTTP(S) target under an exact-origin policy."""
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise OutboundUrlError("NZB URL is invalid")
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in value
    ):
        raise OutboundUrlError("NZB URL is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise OutboundUrlError("NZB URL is invalid") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port == 0
        or parsed.fragment
    ):
        raise OutboundUrlError("NZB URL is invalid")
    host = parsed.hostname.rstrip(".").lower()
    if not host or host in {"localhost", "localhost.localdomain"}:
        raise OutboundUrlError("NZB URL must use a public host")
    effective_port = (
        port if port is not None else (443 if parsed.scheme == "https" else 80)
    )
    try:
        records = await asyncio.get_running_loop().getaddrinfo(
            host, effective_port, type=0, proto=0
        )
    except OSError as exc:
        raise OutboundUrlError("NZB URL host could not be resolved") from exc
    addresses = tuple(dict.fromkeys((record[0], record[4][0]) for record in records))
    origin_host = f"[{host}]" if ":" in host else host
    origin = f"{parsed.scheme}://{origin_host}:{effective_port}"
    if not addresses or (
        any(not _is_public_address(address) for _, address in addresses)
        and origin not in allowed_private_origins
    ):
        raise OutboundUrlError("NZB URL must use a public host")
    return ValidatedUrl(
        value,
        parsed.scheme,
        host,
        effective_port,
        origin,
        addresses,
    )


async def validate_public_http_url(value: object) -> ValidatedUrl:
    """Validate and pin a public HTTP(S) target before one connection."""
    return await validate_http_url(value)


async def fetch_http_bytes(
    url: object,
    *,
    max_bytes: int,
    headers: dict[str, str] | None = None,
    origin_headers: dict[str, str] | None = None,
    credential_origin: str | None = None,
    allowed_private_origins: frozenset[str] = frozenset(),
    redirects: int = 3,
) -> bytes:
    """Fetch bounded bytes with DNS pinning and credential stripping per hop."""
    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or not 1 <= max_bytes <= MAX_NZB_DOCUMENT_BYTES
        or isinstance(redirects, bool)
        or not isinstance(redirects, int)
        or not 0 <= redirects <= 10
        or origin_headers is not None
        and (
            not isinstance(credential_origin, str)
            or not credential_origin
            or len(credential_origin) > 4096
        )
    ):
        raise OutboundUrlError("Outbound HTTP request policy is invalid")
    common_headers = _validated_headers(headers, allow_credentials=False)
    scoped_headers = _validated_headers(origin_headers, allow_credentials=True)
    try:
        async with asyncio.timeout(20):
            return await _fetch_http_bytes(
                url,
                max_bytes=max_bytes,
                headers=common_headers,
                origin_headers=scoped_headers,
                credential_origin=credential_origin,
                allowed_private_origins=allowed_private_origins,
                redirects=redirects,
            )
    except TimeoutError as exc:
        raise OutboundUrlError("NZB URL could not be fetched") from exc


async def _fetch_http_bytes(
    url: object,
    *,
    max_bytes: int,
    headers: dict[str, str] | None,
    origin_headers: dict[str, str] | None,
    credential_origin: str | None,
    allowed_private_origins: frozenset[str],
    redirects: int,
) -> bytes:
    current = url
    timeout = aiohttp.ClientTimeout(connect=5, sock_read=10)
    for _ in range(redirects + 1):
        target = await validate_http_url(
            current,
            allowed_private_origins=allowed_private_origins,
        )
        try:
            async with _outbound_session(target, timeout) as session:
                async with session.get(
                    target.url,
                    allow_redirects=False,
                    headers={
                        "Accept-Encoding": "identity",
                        **(headers or {}),
                        **(
                            origin_headers or {}
                            if credential_origin == target.origin
                            else {}
                        ),
                    },
                ) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location:
                            raise OutboundUrlError("NZB URL redirect is invalid")
                        current = str(response.url.join(URL(location)))
                        continue
                    if not is_success_status(response.status):
                        raise OutboundUrlError(
                            "NZB URL could not be fetched",
                            http_status=response.status,
                        )
                    body = BytesIO()
                    body_size = 0
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        body_size += len(chunk)
                        if body_size > max_bytes:
                            raise OutboundUrlError("NZB URL body is too large")
                        body.write(chunk)
                    if body_size == 0:
                        raise OutboundUrlError("NZB URL body is empty")
                    return body.getvalue()
        except aiohttp.ClientError as exc:
            raise OutboundUrlError("NZB URL could not be fetched") from exc
    raise OutboundUrlError("NZB URL redirected too many times")


@asynccontextmanager
async def _outbound_session(target: ValidatedUrl, timeout: aiohttp.ClientTimeout):
    if settings.USER_PROVIDED_PROXY_URL is not None:
        yield await http_client_manager.get_user_session()
        return
    connector = aiohttp.TCPConnector(
        resolver=PinnedResolver(target.addresses),
        use_dns_cache=False,
        limit=1,
    )
    async with aiohttp.ClientSession(
        connector=connector,
        timeout=timeout,
        auto_decompress=False,
    ) as session:
        yield session


async def fetch_public_nzb(
    url: object,
    *,
    max_bytes: int,
    redirects: int = 3,
) -> bytes:
    """Fetch one public NZB while validating and pinning every redirect hop."""
    return await fetch_http_bytes(
        url,
        max_bytes=max_bytes,
        headers={
            "Accept": (
                "application/x-nzb, application/xml, text/xml, application/gzip, */*"
            )
        },
        redirects=redirects,
    )
