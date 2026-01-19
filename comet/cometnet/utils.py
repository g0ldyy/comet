"""
CometNet Utilities Module

Common utility functions for P2P networking, data normalization,
and asynchronous execution.
"""

import asyncio
import ipaddress
import re
import socket
from functools import partial
from typing import Any, Callable, Optional, Tuple, TypeVar
from urllib.parse import quote, urlparse

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


def resolve_hostname_to_ip(hostname: str) -> Optional[str]:
    """
    Resolve a hostname to its IP address.
    Returns None if resolution fails.
    """
    try:
        return socket.gethostbyname(hostname)
    except (socket.gaierror, socket.herror, OSError):
        return None


def is_private_or_internal_ip(host: str) -> bool:
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
    resolved_ip = resolve_hostname_to_ip(host)
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


async def _check_local_reachability(
    http_url: str, timeout: float
) -> Tuple[bool, Optional[str]]:
    """
    Local reachability check - verifies the server is running and responding.
    """
    parsed = urlparse(http_url)

    paths_to_try = ["/", "/health"]

    # If the URL has a specific path, also try that path's parent
    if parsed.path and parsed.path not in ("/", "/health", ""):
        # e.g., /cometnet/ws -> try /cometnet/ as well
        parent = "/".join(parsed.path.rstrip("/").split("/")[:-1])
        if parent and parent != "/":
            paths_to_try.insert(0, parent + "/")

    last_error = None

    for path in paths_to_try:
        check_url = f"{parsed.scheme}://{parsed.netloc}{path}"

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as session:
                async with session.get(check_url) as response:
                    if response.status == 200:
                        body = await response.text()
                        if COMETNET_SERVER_IDENTIFIER in body:
                            return True, None
                        else:
                            last_error = (
                                f"Response at {path} does not contain '{COMETNET_SERVER_IDENTIFIER}'. "
                                "If using a reverse proxy, ensure it forwards to the CometNet WebSocket port."
                            )
                    elif response.status == 426:
                        # 426 Upgrade Required means CometNet is responding!
                        # This happens on non-root paths
                        return True, None
                    else:
                        last_error = f"HTTP {response.status} at {path}"
        except aiohttp.ClientConnectorError as e:
            last_error = f"Connection failed to {check_url}: {e}"
        except asyncio.TimeoutError:
            last_error = f"Connection timed out to {check_url}"
        except aiohttp.ClientSSLError as e:
            last_error = f"SSL/TLS error: {e} - Check your certificate configuration"
        except Exception as e:
            last_error = f"Unexpected error checking {check_url}: {e}"

    return False, last_error


def _is_public_ip(hostname: str) -> bool:
    """Check if a hostname is a public IP address (not private/internal)."""
    try:
        ip = ipaddress.ip_address(hostname)
        is_public = not (
            ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        )
        return is_public
    except ValueError:
        return False


async def _check_external_reachability(
    advertise_url: str, timeout: float, logger=None
) -> Tuple[Optional[bool], Optional[str]]:
    """
    Verify external reachability using third-party services.

    Returns:
        (True, message) - Externally verified as reachable (message has details)
        (False, error) - Externally verified as NOT reachable
        (None, message) - Could not verify externally (services unavailable)
    """
    http_url = websocket_url_to_http(advertise_url)

    def log(msg):
        if logger:
            logger.debug(f"[ExternalCheck] {msg}")

    # Step 1: Initiate the check
    try:
        encoded_url = quote(http_url, safe="")
        init_url = f"https://check-host.net/check-http?host={encoded_url}"

        log(f"Initiating check-host.net request for {http_url}")

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout),
            headers={"Accept": "application/json"},
        ) as session:
            async with session.get(init_url) as response:
                if response.status != 200:
                    log(f"check-host.net returned status {response.status}")
                    return None, f"check-host.net returned status {response.status}"

                data = await response.json()
                request_id = data.get("request_id")
                nodes = data.get("nodes", {})

                if not request_id or not nodes:
                    log("check-host.net did not return a valid request")
                    return None, "check-host.net did not return a valid request"

                log(f"Check initiated: request_id={request_id}, nodes={len(nodes)}")

            # Step 2: Wait for results
            result_url = f"https://check-host.net/check-result/{request_id}"

            # Poll for results (check-host.net needs a few seconds)
            for attempt in range(5):  # Max ~10 seconds of waiting
                await asyncio.sleep(2.0)  # Wait before checking results

                log(f"Polling results (attempt {attempt + 1}/5)...")

                async with session.get(result_url) as result_response:
                    if result_response.status != 200:
                        log(f"Result poll returned status {result_response.status}")
                        continue

                    results = await result_response.json()

                    # Count successful checks
                    success_count = 0
                    failure_count = 0
                    pending_count = 0
                    success_nodes = []
                    failure_nodes = []

                    for node_id, node_result in results.items():
                        if node_result is None:
                            pending_count += 1
                            continue

                        if isinstance(node_result, list) and len(node_result) > 0:
                            check_data = node_result[0]
                            if isinstance(check_data, list) and len(check_data) > 0:
                                is_ok = check_data[0]
                                status = check_data[2] if len(check_data) > 2 else None

                                if is_ok == 1:
                                    success_count += 1
                                    success_nodes.append(f"{node_id}(HTTP OK)")
                                elif isinstance(status, int) and 100 <= status < 600:
                                    success_count += 1
                                    success_nodes.append(f"{node_id}(HTTP {status})")
                                else:
                                    failure_count += 1
                                    failure_nodes.append(f"{node_id}({status})")

                    total_completed = success_count + failure_count
                    log(
                        f"Results: {success_count} success, {failure_count} failed, "
                        f"{pending_count} pending"
                    )

                    # If we have enough results, make a decision
                    if total_completed >= 2:
                        if success_count > 0:
                            log(f"SUCCESS! Nodes that reached you: {success_nodes}")
                            return (
                                True,
                                f"Externally verified by {success_count}/{total_completed} check-host.net nodes",
                            )
                        else:
                            log(f"FAILED! No nodes could reach you: {failure_nodes}")
                            return (
                                False,
                                f"External check failed: 0/{total_completed} nodes could reach your URL",
                            )

                    # If all completed and none succeeded
                    if pending_count == 0 and success_count == 0:
                        log(f"All nodes failed: {failure_nodes}")
                        return (
                            False,
                            f"External check failed: No nodes could reach your URL ({failure_count} failed)",
                        )

            # Timeout waiting for results
            log("Timed out waiting for enough results")
            return None, "External check timed out waiting for results"

    except asyncio.TimeoutError:
        log("Request timed out")
        return None, "External check timed out"
    except aiohttp.ClientError as e:
        log(f"Network error: {e}")
        return None, f"External check network error: {e}"
    except Exception as e:
        log(f"Unexpected error: {e}")
        return None, f"External check error: {e}"


async def check_advertise_url_reachability(
    advertise_url: str, timeout: float = 10.0, logger=None
) -> Tuple[bool, Optional[str]]:
    """
    Check if the advertise URL is reachable and correctly configured.

    Behavior depends on URL type:
    - PUBLIC IP (e.g., ws://1.2.3.4:8765): External check is REQUIRED
      Local checks are meaningless for direct IPs (NAT hairpinning bypasses firewall)
    - DOMAIN NAME (e.g., wss://example.com): Local check + optional external
      For reverse proxy setups, local check verifies proxy config

    Args:
        advertise_url: The WebSocket URL to check (ws:// or wss://)
        timeout: Request timeout in seconds
        logger: Optional logger for debug output

    Returns:
        Tuple of (is_reachable, message)
        - (True, success_message) if the URL is verified reachable
        - (True, warning_message) if local check passed but couldn't verify externally (domain only)
        - (False, error_message) if the URL fails checks
    """
    if not advertise_url:
        return False, "No advertise URL configured"

    # Validate URL format first
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

    hostname = parsed.hostname
    is_public_ip_url = _is_public_ip(hostname)

    # Convert to HTTP for checking
    http_url = websocket_url_to_http(advertise_url)

    if is_public_ip_url:
        external_result, external_msg = await _check_external_reachability(
            advertise_url,
            timeout=15.0,
            logger=logger,  # More time for external checks
        )

        if external_result is True:
            return True, f"EXTERNAL_VERIFIED: {external_msg}"
        elif external_result is False:
            return (
                False,
                f"External reachability check FAILED.\n"
                f"{external_msg}\n\n"
                "Your advertise URL is a public IP, so external verification is required.\n"
                "Local checks are bypassed because they would go through NAT hairpinning\n"
                "and always succeed even if your port is not externally accessible.\n\n"
                "Possible causes:\n"
                "1. Firewall blocking incoming connections on this port\n"
                "2. UPnP port mapping not working correctly\n"
                "3. ISP blocking incoming connections (CGNAT)\n\n"
                "To verify manually, ask someone on another network to run:\n"
                f"  curl -v {http_url}",
            )
        else:
            return (
                False,
                f"CANNOT VERIFY external reachability for public IP.\n"
                f"Reason: {external_msg}\n\n"
                "Your advertise URL ({hostname}) is a public IP address.\n"
                "For public IPs, external verification is REQUIRED because:\n"
                "- Local checks go through NAT hairpinning and ALWAYS pass\n"
                "- This doesn't prove your port is externally accessible\n\n"
                "Options:\n"
                "1. Check your firewall/router allows incoming connections\n"
                "2. Verify UPnP mapping is working (check router admin page)\n"
                "3. Test manually: ask someone outside your network to run:\n"
                f"   curl -v {http_url}\n"
                "4. If you're SURE it works, set COMETNET_SKIP_REACHABILITY_CHECK=true\n\n"
                "Note: If external services (check-host.net) are blocked, you may need\n"
                "to temporarily allow outbound HTTPS to verify, or use option 4.",
            )

    external_result, external_msg = await _check_external_reachability(
        advertise_url, timeout=12.0, logger=logger
    )

    is_local_ok, local_error = await _check_local_reachability(http_url, timeout)

    # Case 1: External OK + Local OK
    if external_result is True and is_local_ok:
        return True, f"EXTERNAL_VERIFIED: {external_msg}"

    # Case 2: External OK + Local FAIL
    if external_result is True and not is_local_ok:
        if "Connection failed" in (local_error or "") or "timed out" in (
            local_error or ""
        ):
            # Likely hairpin NAT - accept with warning
            return (
                True,
                f"EXTERNAL_VERIFIED_NO_LOCAL: {external_msg} (local check failed: hairpin NAT?)",
            )
        else:
            # Local check got a response but it's NOT CometNet
            return (
                False,
                f"URL is reachable but does NOT appear to be CometNet!\n\n"
                f"External check: {external_msg}\n"
                f"Local check: {local_error}\n\n"
                "The URL responds but the response doesn't contain 'CometNet WebSocket Server'.\n"
                "This usually means you've configured the WRONG URL.\n\n"
                "Please verify COMETNET_ADVERTISE_URL points to YOUR CometNet instance,\n"
                "not some other website or service.",
            )

    # Case 3: External FAIL + Local OK
    if external_result is False and is_local_ok:
        # Local works but external doesn't - maybe check-host.net is blocked?
        return (
            True,
            f"LOCAL_ONLY: External check failed ({external_msg}) but local check passed",
        )

    # Case 4: External FAIL + Local FAIL
    if external_result is False:
        return (
            False,
            f"External verification failed: {external_msg}\n\n"
            "check-host.net servers could not reach your URL.\n"
            "This means your reverse proxy is not publicly accessible.\n\n"
            "Check:\n"
            "1. DNS is correctly configured for your domain\n"
            "2. Reverse proxy (Traefik/nginx/Caddy) is routing to CometNet port\n"
            "3. Firewall allows incoming HTTPS traffic\n"
            "4. SSL certificate is valid",
        )

    # Case 5: External unavailable (None) + Local OK
    if external_result is None and is_local_ok:
        return True, f"LOCAL_ONLY: {external_msg}"

    # Case 6: External unavailable + Local FAIL
    error_msg = local_error or "Local check failed"

    if "Connection failed" in error_msg or "timed out" in error_msg:
        return (
            False,
            f"Could not verify reachability.\n"
            f"External check: {external_msg}\n"
            f"Local check: {error_msg}\n\n"
            "This might be a hairpin NAT issue (your server can't reach itself\n"
            "via its public domain from inside the network).\n\n"
            "Options:\n"
            "1. Test manually from OUTSIDE your network:\n"
            f"   curl -v {http_url}\n"
            "2. If you're SURE it works externally, set COMETNET_SKIP_REACHABILITY_CHECK=true",
        )
    else:
        return (
            False,
            f"Could not verify reachability.\n"
            f"External check: {external_msg}\n"
            f"Local check: {error_msg}\n\n"
            "The URL responds but doesn't appear to be CometNet.\n"
            "Verify COMETNET_ADVERTISE_URL points to your CometNet instance.\n\n"
            "Options:\n"
            "1. Test manually from OUTSIDE your network:\n"
            f"   curl -v {http_url}\n"
            "2. If you're SURE it works externally, set COMETNET_SKIP_REACHABILITY_CHECK=true",
        )
