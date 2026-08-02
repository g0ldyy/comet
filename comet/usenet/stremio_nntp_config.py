"""Strict client-native NNTP configuration."""

from urllib.parse import quote, unquote, urlsplit

from comet.usenet.nntp_config import canonical_nntp_host, valid_nntp_credential

_HANDOFF_OPTION_FIELDS = frozenset({"servers"})


def serialize_server(config: dict) -> str:
    """Serialize one explicit client-owned NNTP server without URL ambiguity."""
    if not isinstance(config, dict):
        raise ValueError("NNTP server must be an object")
    allowed = {
        "host",
        "port",
        "tls_mode",
        "username",
        "password",
        "connections",
    }
    if not allowed <= config.keys():
        raise ValueError("NNTP server has an invalid shape")
    host = config["host"]
    username = config["username"]
    password = config["password"]
    port = config["port"]
    connections = config["connections"]
    tls_mode = config["tls_mode"]
    if (
        not isinstance(host, str)
        or not host
        or any(character.isspace() or ord(character) < 33 for character in host)
        or not valid_nntp_credential(username)
        or not valid_nntp_credential(password)
        or isinstance(port, bool)
        or not isinstance(port, int)
        or not 1 <= port <= 65535
        or isinstance(connections, bool)
        or not isinstance(connections, int)
        or not 1 <= connections <= 100
        or tls_mode not in {"implicit_tls", "plaintext"}
    ):
        raise ValueError("NNTP server has invalid values")
    try:
        canonical_host = canonical_nntp_host(host, "NNTP server")
    except ValueError as exc:
        raise ValueError("NNTP server host is invalid") from exc
    if ":" in canonical_host:
        canonical_host = f"[{canonical_host}]"
    scheme = "nntps" if tls_mode == "implicit_tls" else "nntp"
    return (
        f"{scheme}://{quote(username, safe='')}:{quote(password, safe='')}"
        f"@{canonical_host}:{port}/{connections}"
    )


def serialize_servers(configs: list[dict]) -> tuple[str, ...]:
    if not isinstance(configs, list) or not 1 <= len(configs) <= 8:
        raise ValueError("Stremio NNTP requires one to eight servers")
    return tuple(dict.fromkeys(serialize_server(config) for config in configs))


def validate_serialized_servers(value: object) -> tuple[str, ...]:
    """Accept only the exact canonical URI form emitted for supported clients."""
    if not isinstance(value, tuple) or not 1 <= len(value) <= 8:
        raise ValueError("Stremio NNTP requires one to eight servers")
    if any(not isinstance(server, str) for server in value):
        raise ValueError("Stremio NNTP server URI is invalid")
    servers = tuple(dict.fromkeys(value))
    for server in servers:
        try:
            encoded = server.encode("ascii")
            parsed = urlsplit(server)
            port = parsed.port
            username = unquote(parsed.username or "", errors="strict")
            password = unquote(parsed.password or "", errors="strict")
            connections_text = parsed.path.removeprefix("/")
        except (UnicodeDecodeError, UnicodeEncodeError, ValueError):
            raise ValueError("Stremio NNTP server URI is invalid") from None
        if (
            not 1 <= len(encoded) <= 4_096
            or parsed.scheme not in {"nntp", "nntps"}
            or not parsed.hostname
            or parsed.username is None
            or parsed.password is None
            or port is None
            or not parsed.path.startswith("/")
            or not connections_text.isascii()
            or not connections_text.isdigit()
            or not 1 <= len(connections_text) <= 3
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Stremio NNTP server URI is invalid")
        canonical = serialize_server(
            {
                "host": parsed.hostname,
                "port": port,
                "tls_mode": (
                    "implicit_tls" if parsed.scheme == "nntps" else "plaintext"
                ),
                "username": username,
                "password": password,
                "connections": int(connections_text),
            }
        )
        if canonical != server:
            raise ValueError("Stremio NNTP server URI is invalid")
    return servers


def validate_handoff_config(config: object) -> tuple[str, ...]:
    """Validate the client-owned NNTP servers handed to Stremio."""
    if not isinstance(config, dict) or config.keys() != _HANDOFF_OPTION_FIELDS:
        raise ValueError("Stremio NNTP options have an invalid shape")
    return serialize_servers(config["servers"])
