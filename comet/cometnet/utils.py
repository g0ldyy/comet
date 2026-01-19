"""
CometNet Utilities Module

Common utility functions for P2P networking, data normalization,
and asynchronous execution.
"""

import asyncio
import ipaddress
from functools import partial
from typing import Any, Callable, Optional, Tuple, TypeVar
from urllib.parse import urlparse

import aiohttp

T = TypeVar("T")


# --- Data Normalization ---


def canonicalize_data(data: Any) -> Any:
    """
    Recursively sort dict keys for deterministic serialization.
    Used for creating stable signatures.
    """
    if isinstance(data, dict):
        return {k: canonicalize_data(v) for k, v in sorted(data.items())}
    elif isinstance(data, list):
        return [canonicalize_data(i) for i in data]
    return data


# --- Network Utilities ---


def is_private_or_internal_ip(host: str) -> bool:
    """
    Check if a host is a private/internal IP address.
    Checks for: private, loopback, link-local, and reserved addresses.
    """
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
    except ValueError:
        # Not an IP address (could be a hostname)
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


def is_valid_peer_address(address: str, allow_private: bool = False) -> bool:
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
        if not allow_private and is_private_or_internal_ip(host):
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
    Run a blocking function in the default loop executor.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(func, *args))


# --- Reachability Check ---

COMETNET_SERVER_IDENTIFIER = "CometNet WebSocket Server"


def websocket_url_to_http(ws_url: str) -> str:
    """
    Convert a WebSocket URL to its HTTP equivalent.

    ws://host:port/path -> http://host:port/path
    wss://host:port/path -> https://host:port/path
    """
    if ws_url.startswith("wss://"):
        return "https://" + ws_url[6:]
    elif ws_url.startswith("ws://"):
        return "http://" + ws_url[5:]
    return ws_url


async def check_advertise_url_reachability(
    advertise_url: str, timeout: float = 10.0
) -> Tuple[bool, Optional[str]]:
    """
    Check if the advertise URL is externally reachable by making an HTTP request.

    The CometNet WebSocket server responds with "CometNet WebSocket Server" on
    HTTP health check requests. This function verifies that the URL is accessible
    from the outside by checking for this response.

    Args:
        advertise_url: The WebSocket URL to check (ws:// or wss://)
        timeout: Request timeout in seconds

    Returns:
        Tuple of (is_reachable, error_message)
        - (True, None) if the URL is reachable and responds correctly
        - (False, error_message) if the URL is not reachable or invalid
    """
    if not advertise_url:
        return False, "No advertise URL configured"

    # Convert ws:// to http:// or wss:// to https://
    http_url = websocket_url_to_http(advertise_url)

    # Extract base URL (remove /ws or other paths, check root)
    parsed = urlparse(http_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}/"

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as session:
            async with session.get(base_url) as response:
                if response.status != 200:
                    return False, (
                        f"HTTP {response.status} - Server may not be accessible from outside"
                    )

                body = await response.text()
                if COMETNET_SERVER_IDENTIFIER not in body:
                    return False, (
                        f"Response does not contain '{COMETNET_SERVER_IDENTIFIER}' - "
                        "the URL may point to a different service or a reverse proxy "
                        "is not correctly configured"
                    )

                return True, None
    except aiohttp.ClientConnectorError as e:
        return (
            False,
            f"Connection failed: {e} - The address may not be accessible from outside",
        )
    except asyncio.TimeoutError:
        return (
            False,
            f"Connection timed out after {timeout}s - The address may not be accessible",
        )
    except aiohttp.ClientSSLError as e:
        return False, f"SSL/TLS error: {e} - Check your certificate configuration"
    except Exception as e:
        return False, f"Unexpected error: {e}"
