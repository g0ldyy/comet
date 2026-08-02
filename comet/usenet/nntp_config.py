"""Strict request-local configuration for native NNTP server sources."""

import ipaddress
import re
from dataclasses import dataclass

import idna

_SERVER_FIELDS = {
    "name",
    "host",
    "port",
    "tls_mode",
    "username",
    "password",
    "connections",
    "priority",
    "backup",
    "pipeline",
}
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_SERVER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_MAX_NATIVE_SERVERS = 16
_MAX_NNTP_CREDENTIAL_BYTES = 512


@dataclass(frozen=True, slots=True)
class NntpServerConfig:
    name: str
    host: str
    port: int
    tls_mode: str
    username: str | None
    password: str | None
    connections: int
    priority: int
    backup: bool
    pipeline: int


def valid_nntp_credential(value: object) -> bool:
    """Match the native engine's bounded, command-safe credential contract."""
    if not isinstance(value, str) or not value:
        return False
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return len(encoded) <= _MAX_NNTP_CREDENTIAL_BYTES and not any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in value
    )


def canonical_nntp_host(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 253
        or any(character.isspace() or ord(character) < 33 for character in value)
        or "://" in value
        or "/" in value
        or "%" in value
    ):
        raise ValueError(f"{label} has an invalid name or host")
    try:
        parsed_ip = ipaddress.ip_address(value)
    except ValueError:
        try:
            canonical = (
                idna.encode(
                    value,
                    uts46=True,
                    std3_rules=True,
                )
                .decode("ascii")
                .lower()
            )
        except idna.IDNAError as exc:
            raise ValueError(f"{label} has an invalid name or host") from exc
        if (
            len(canonical) > 253
            or canonical.endswith(".")
            or any(
                not _DNS_LABEL.fullmatch(dns_label)
                for dns_label in canonical.split(".")
            )
        ):
            raise ValueError(f"{label} has an invalid name or host")
        return canonical
    return parsed_ip.compressed


def _parse_servers(
    value: object, *, maximum: int, label: str
) -> tuple[NntpServerConfig, ...]:
    if not isinstance(value, list) or not value or len(value) > maximum:
        raise ValueError(f"{label} must contain one to {maximum} entries")
    servers = []
    names = set()
    for entry in value:
        if not isinstance(entry, dict) or set(entry) - _SERVER_FIELDS:
            raise ValueError(f"{label} has an invalid shape")
        required = {"name", "host", "port", "tls_mode", "connections", "priority"}
        if not required.issubset(entry):
            raise ValueError(f"{label} is incomplete")
        name = entry["name"]
        host = entry["host"]
        normalized_name = name.casefold() if isinstance(name, str) else ""
        if (
            not isinstance(name, str)
            or _SERVER_NAME.fullmatch(name) is None
            or normalized_name in names
        ):
            raise ValueError(f"{label} has an invalid name or host")
        host = canonical_nntp_host(host, label)
        names.add(normalized_name)
        port = entry["port"]
        connections = entry["connections"]
        priority = entry["priority"]
        pipeline = entry.get("pipeline", 16)
        tls_mode = entry["tls_mode"]
        if (
            isinstance(port, bool)
            or not isinstance(port, int)
            or not 1 <= port <= 65535
            or tls_mode not in {"implicit", "starttls", "plaintext"}
            or isinstance(connections, bool)
            or not isinstance(connections, int)
            or not 1 <= connections <= 100
            or isinstance(priority, bool)
            or not isinstance(priority, int)
            or not 0 <= priority <= 1000
            or isinstance(pipeline, bool)
            or not isinstance(pipeline, int)
            or not 1 <= pipeline <= 16
        ):
            raise ValueError(f"{label} has invalid connection settings")
        username = entry.get("username")
        password = entry.get("password")
        if (username is None) != (password is None):
            raise ValueError(f"{label} credentials must be provided together")
        if any(
            not valid_nntp_credential(secret)
            for secret in (username, password)
            if secret is not None
        ):
            raise ValueError(f"{label} credentials are invalid")
        backup = entry.get("backup", False)
        if not isinstance(backup, bool):
            raise ValueError(f"{label} backup must be boolean")
        servers.append(
            NntpServerConfig(
                name,
                host,
                port,
                tls_mode,
                username,
                password,
                connections,
                priority,
                backup,
                pipeline,
            )
        )
    return tuple(servers)


def parse_personal_servers(value: object) -> tuple[NntpServerConfig, ...]:
    """Validate the closed Base64-carried personal NNTP server list."""
    return _parse_servers(
        value,
        maximum=_MAX_NATIVE_SERVERS,
        label="personal NNTP servers",
    )


def parse_instance_servers(value: object) -> tuple[NntpServerConfig, ...]:
    """Normalize the already validated operator-owned NNTP pool."""
    return _parse_servers(
        value,
        maximum=_MAX_NATIVE_SERVERS,
        label="instance NNTP servers",
    )
