import ipaddress
from typing import Mapping, Optional, Tuple, Union

from fastapi import Request, WebSocket

IP_PROXY_HEADERS = [
    "cf-connecting-ip",  # Cloudflare
    "true-client-ip",  # Cloudflare Enterprise / Akamai
    "x-real-ip",  # Nginx default
    "x-client-ip",  # Generic
    "do-connecting-ip",  # DigitalOcean
    "fastly-client-ip",  # Fastly CDN
    "x-cluster-client-ip",  # Rackspace
    "x-forwarded-for",  # Standard (can be spoofed, check last)
    "x-forwarded",  # Non-standard variant
    "forwarded-for",  # Non-standard variant
    "forwarded",  # RFC 7239
    "x-appengine-user-ip",  # Google App Engine
    "cf-pseudo-ipv4",  # Cloudflare IPv6->IPv4
]


def is_public_ip(ip: str) -> bool:
    """Check if an IP address is public (not private, loopback, or reserved)."""
    try:
        parsed_ip = ipaddress.ip_address(ip)
        return not parsed_ip.is_private and not parsed_ip.is_loopback
    except ValueError:
        return False


def is_valid_ip(ip: str) -> bool:
    """Check if a string is a valid IP address."""
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return False


def extract_ip_from_headers(
    headers: Mapping[str, str], require_public: bool = True
) -> Optional[str]:
    """
    Extract the real client IP from proxy headers.

    Args:
        headers: HTTP headers (case-insensitive mapping)
        require_public: If True, only return public IPs. If False, return any valid IP.

    Returns:
        The extracted IP or None if not found.
    """
    normalized = {k.lower(): v for k, v in headers.items()}

    for header_name in IP_PROXY_HEADERS:
        header_value = normalized.get(header_name)
        if not header_value:
            continue

        if header_name in ("x-forwarded-for", "x-forwarded", "forwarded-for"):
            for ip_part in header_value.split(","):
                ip_part = ip_part.strip()
                if is_valid_ip(ip_part):
                    if not require_public or is_public_ip(ip_part):
                        return ip_part
        elif header_name == "forwarded":
            for part in header_value.split(","):
                for directive in part.split(";"):
                    directive = directive.strip()
                    if directive.lower().startswith("for="):
                        ip_part = directive[4:].strip().strip('"')
                        if ip_part.startswith("[") and "]" in ip_part:
                            ip_part = ip_part[1 : ip_part.index("]")]
                        if is_valid_ip(ip_part):
                            if not require_public or is_public_ip(ip_part):
                                return ip_part
        else:
            ip_part = header_value.strip()
            if is_valid_ip(ip_part):
                if not require_public or is_public_ip(ip_part):
                    return ip_part

    return None


def get_client_ip(request: Union[Request, WebSocket]) -> str:
    """
    Get the real client IP from a FastAPI Request or WebSocket.

    Priority:
    1. Proxy headers (CF-Connecting-IP, X-Real-IP, etc.)
    2. Direct client connection

    Returns empty string if no public IP found.
    """
    real_ip = extract_ip_from_headers(dict(request.headers), require_public=True)
    if real_ip:
        return real_ip

    if request.client and request.client.host and is_public_ip(request.client.host):
        return request.client.host

    return ""


def get_client_ip_any(request: Union[Request, WebSocket]) -> Tuple[str, bool]:
    """
    Get the client IP, including private IPs as fallback.

    Returns:
        Tuple of (ip_address, is_from_proxy_header)
        Returns ("unknown", False) if nothing found.
    """
    real_ip = extract_ip_from_headers(dict(request.headers), require_public=True)
    if real_ip:
        return (real_ip, True)

    real_ip = extract_ip_from_headers(dict(request.headers), require_public=False)
    if real_ip:
        return (real_ip, True)

    if request.client and request.client.host:
        ip = request.client.host
        if is_valid_ip(ip):
            return (ip, False)

    return ("unknown", False)
