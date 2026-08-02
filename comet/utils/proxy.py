from urllib.parse import unquote, urlparse

from aiohttp_socks import ProxyConnector, ProxyType

SOCKS4_PROXY_SCHEMES = frozenset({"socks4", "socks4a"})
SOCKS_PROXY_SCHEMES = SOCKS4_PROXY_SCHEMES | {"socks5", "socks5h"}
REMOTE_DNS_PROXY_SCHEMES = frozenset({"socks4a", "socks5h"})
SUPPORTED_PROXY_SCHEMES = SOCKS_PROXY_SCHEMES | {"http", "https"}


def is_socks_proxy(proxy_url: str) -> bool:
    return urlparse(proxy_url).scheme in SOCKS_PROXY_SCHEMES


def socks_proxy_connector(proxy_url: str, **connector_options) -> ProxyConnector:
    parsed = urlparse(proxy_url)
    return ProxyConnector(
        proxy_type=(
            ProxyType.SOCKS4
            if parsed.scheme in SOCKS4_PROXY_SCHEMES
            else ProxyType.SOCKS5
        ),
        host=parsed.hostname,
        port=parsed.port or 1080,
        username=unquote(parsed.username) if parsed.username else None,
        password=unquote(parsed.password) if parsed.password else None,
        rdns=parsed.scheme in REMOTE_DNS_PROXY_SCHEMES,
        **connector_options,
    )
