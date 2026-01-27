"""
CometNet Utilities Module

Common utility functions for P2P networking, data normalization,
and asynchronous execution.
"""

import asyncio
import ipaddress
import re
import socket
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, Callable, Optional, Tuple, TypeVar
from urllib.parse import urlparse

import orjson
import websockets
from websockets.exceptions import InvalidHandshake, InvalidStatusCode

from comet.core.models import settings

T = TypeVar("T")

_crypto_executor: Optional[ThreadPoolExecutor] = None


def _get_crypto_executor() -> ThreadPoolExecutor:
    """Get or create the dedicated crypto thread pool."""
    global _crypto_executor
    if _crypto_executor is None:
        pool_size = max(4, settings.EXECUTOR_MAX_WORKERS)
        _crypto_executor = ThreadPoolExecutor(
            max_workers=pool_size, thread_name_prefix="cometnet-crypto-"
        )
    return _crypto_executor


def shutdown_crypto_executor() -> None:
    """Shutdown the crypto executor (call on application shutdown)."""
    global _crypto_executor
    if _crypto_executor is not None:
        _crypto_executor.shutdown(wait=False)
        _crypto_executor = None


# --- Data Normalization ---


def canonicalize_data(data: Any) -> Any:
    """
    Recursively sort dict keys for deterministic serialization.
    Used for creating stable signatures.
    """
    return orjson.loads(orjson.dumps(data, option=orjson.OPT_SORT_KEYS))


# --- Network Utilities ---

# Internal/suspicious domain patterns that should be blocked
INTERNAL_DOMAIN_PATTERNS = [
    r"\.local$",  # mDNS local domains
    r"\.internal$",  # Common internal suffix
    r"\.lan$",  # LAN suffix
    r"\.localdomain$",  # Linux default
    r"\.home$",  # Home networks
    r"\.corp$",  # Corporate networks
    r"\.intranet$",  # Intranet suffix
    r"\.private$",  # Private suffix
    r"^localhost\.",  # localhost.* variants
    r"\.localhost$",  # *.localhost variants
]

_INTERNAL_DOMAIN_RE = [re.compile(p, re.IGNORECASE) for p in INTERNAL_DOMAIN_PATTERNS]

# IP-in-domain services that could be used for DNS rebinding attacks
IP_IN_DOMAIN_PATTERNS = [
    r"\.nip\.io$",  # nip.io (10-0-0-1.nip.io)
    r"\.sslip\.io$",  # sslip.io
    r"\.xip\.io$",  # xip.io (deprecated but still works sometimes)
    r"^(?:\d{1,3}[-.]){3}\d{1,3}\.",  # IP at start: 192-168-1-1.example.com
]

_IP_IN_DOMAIN_RE = [re.compile(p, re.IGNORECASE) for p in IP_IN_DOMAIN_PATTERNS]


def is_internal_domain(hostname: str) -> bool:
    """
    Check if a hostname looks like an internal/private domain.
    This catches domains that resolve to internal IPs even if
    the domain itself isn't an IP address.
    """
    hostname = hostname.lower().strip(".")

    # Check internal domain patterns
    for pattern in _INTERNAL_DOMAIN_RE:
        if pattern.search(hostname):
            return True

    # Check IP-in-domain patterns (potential DNS rebinding)
    for pattern in _IP_IN_DOMAIN_RE:
        if pattern.search(hostname):
            return True

    return False


async def resolve_hostname_to_ip(hostname: str) -> Optional[str]:
    """
    Resolve a hostname to its IP address.
    Returns None if resolution fails.
    """
    try:
        loop = asyncio.get_running_loop()
        result = await loop.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        if result:
            return result[0][4][0]
        return None
    except (socket.gaierror, socket.herror, OSError, IndexError):
        return None


async def is_private_or_internal_ip(host: str) -> bool:
    """
    Check if a host is a private/internal IP address.
    Checks for: private, loopback, link-local, and reserved addresses.
    Also resolves hostnames to check their actual IP.
    """
    # First, try direct IP check
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
    except ValueError:
        pass

    # Not a direct IP - check if it's an internal domain pattern
    if is_internal_domain(host):
        return True

    # Try to resolve the hostname and check the resulting IP
    # This catches DNS rebinding attempts where a public-looking domain
    # resolves to a private IP
    resolved_ip = await resolve_hostname_to_ip(host)
    if resolved_ip:
        try:
            ip = ipaddress.ip_address(resolved_ip)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return True
        except ValueError:
            pass

    return False


def extract_ip_from_address(address: str) -> str:
    """
    Extract the IP/hostname from a WebSocket address.
    Handles ws://, wss://, and raw IP:port formats.
    """
    try:
        address = address.strip()
        # Handle ws:// or wss:// URLs
        if address.startswith(("ws://", "wss://")):
            parsed = urlparse(address)
            return parsed.hostname or "unknown"
        # Handle raw IP:port or just IP
        return address.split(":")[0]
    except Exception:
        return "unknown"


async def is_valid_peer_address(address: str, allow_private: bool = False) -> bool:
    """
    Validate a peer address for security.

    Args:
        address: WebSocket URL to validate
        allow_private: If True, allow private/internal IPs

    Returns:
        True if the address is valid and safe to connect to
    """
    try:
        parsed = urlparse(address)

        # Must be ws:// or wss:// scheme
        if parsed.scheme not in ("ws", "wss"):
            return False

        # Must have a hostname
        if not parsed.hostname:
            return False

        host = parsed.hostname.lower()

        # Block localhost variants if not allowed
        if host in ("localhost", "localhost.localdomain"):
            if not allow_private:
                return False

        # Check for private/internal IP addresses
        if not allow_private and await is_private_or_internal_ip(host):
            return False

        # Port must be valid if specified
        if parsed.port is not None:
            if not (1 <= parsed.port <= 65535):
                return False

        # Block suspicious patterns
        if "@" in address:  # Credential injection
            return False

        return True
    except Exception:
        return False


# --- Async Utilities ---


async def run_in_executor(func: Callable[..., T], *args: Any) -> T:
    """
    Run a blocking function in the dedicated crypto executor.
    """
    loop = asyncio.get_running_loop()
    executor = _get_crypto_executor()
    return await loop.run_in_executor(executor, partial(func, *args))


# --- Reachability Check ---


async def check_advertise_url_reachability(
    advertise_url: str, timeout: float = 10.0, logger=None
) -> Tuple[bool, Optional[str]]:
    """
    Check if the advertise URL is reachable by attempting a WebSocket connection.
    """
    if not advertise_url:
        return False, "No advertise URL configured"

    # Validate URL format
    try:
        parsed = urlparse(advertise_url)
        if parsed.scheme not in ("ws", "wss"):
            return (
                False,
                f"Invalid URL scheme '{parsed.scheme}'. Must be 'ws://' or 'wss://'",
            )
        if not parsed.hostname:
            return False, "Invalid URL: no hostname specified"
        if parsed.port is not None and not (1 <= parsed.port <= 65535):
            return False, f"Invalid port number: {parsed.port}"
    except Exception as e:
        return False, f"Invalid URL format: {e}"

    try:
        async with asyncio.timeout(timeout):
            async with websockets.connect(
                advertise_url,
                close_timeout=2,
                open_timeout=timeout,
            ) as ws:
                await ws.close()
                return True, "WebSocket connection successful"
    except InvalidStatusCode as e:
        return (
            False,
            f"Server returned HTTP {e.status_code} instead of WebSocket upgrade",
        )
    except InvalidHandshake as e:
        return False, f"WebSocket handshake failed: {e}"
    except asyncio.TimeoutError:
        return False, f"Connection timed out after {timeout}s"
    except OSError as e:
        return False, f"Connection failed: {e}"
    except Exception as e:
        return False, f"WebSocket error: {e}"
