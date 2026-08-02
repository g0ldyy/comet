"""Shared playback-provider state and output contracts."""

from dataclasses import dataclass
from enum import StrEnum

REMOTE_PREPARATION_TIMEOUT_SECONDS = 110


class ProviderRuntimeError(RuntimeError):
    """Provider failure carrying the state transitions chosen at its boundary."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        retry_after: int | None = None,
        auth_failed: bool = False,
        terminal: bool = False,
        terminal_status: str | None = None,
        mutation_rejected: bool = False,
        remote_missing: bool = False,
    ):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after
        self.auth_failed = auth_failed
        self.terminal = terminal or terminal_status is not None
        self.terminal_status = terminal_status
        self.mutation_rejected = mutation_rejected
        self.remote_missing = remote_missing


class Readiness(StrEnum):
    UNKNOWN = "unknown"
    REQUIRES_PREPARE = "requires_prepare"
    PREPARING = "preparing"
    READY = "ready"
    RETRYABLE_FAILURE = "retryable_failure"
    TERMINAL_FAILURE = "terminal_failure"


class Actionability(StrEnum):
    NONE = "none"
    CLIENT_ON_DEMAND = "client_on_demand"
    SERVER_ON_DEMAND = "server_on_demand"
    REMOTE_PREPARE = "remote_prepare"


class BytePath(StrEnum):
    CLOUD_REDIRECT = "cloud_redirect"
    SERVER_RELAY = "server_relay"
    NATIVE_ENGINE = "native_engine"
    CLIENT_DELEGATED = "client_delegated"


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    readiness: Readiness
    actionability: Actionability
    code: str | None = None
    auth_failed: bool = False

    def __post_init__(self):
        if (
            self.readiness is Readiness.TERMINAL_FAILURE
            and self.actionability is not Actionability.NONE
        ):
            raise ValueError("terminal provider failures cannot be actionable")
        if (
            self.readiness is Readiness.REQUIRES_PREPARE
            and self.actionability is not Actionability.REMOTE_PREPARE
        ):
            raise ValueError("requires_prepare must use remote_prepare actionability")
        if self.auth_failed and self.readiness is not Readiness.TERMINAL_FAILURE:
            raise ValueError("authentication failures must be terminal")


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    kind: str
    label: str
    accepted_locator_kinds: frozenset[str]
    byte_paths: frozenset[BytePath]
    mutates_upstream: bool
