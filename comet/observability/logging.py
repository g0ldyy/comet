"""Comet's single structured logging contract.

Loguru is deliberately private to this module.  Callers submit literal event
names/messages and allowlisted primitive fields; this module owns validation,
profile filtering, rendering and the single stderr write.
"""

from __future__ import annotations

import asyncio
import inspect
import logging as stdlib_logging
import math
import os
import re
import sys
import threading
import time
import traceback
import unicodedata
import warnings
from collections import deque
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from enum import Enum
from types import TracebackType
from typing import Any, Literal

import orjson
from loguru import logger as _backend
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from comet.core.build_metadata import normalize_commit
from comet.observability.context import (
    TerminalFlag,
    current_connection_id,
    current_request_id,
    current_run_id,
    current_terminal_flags,
)
from comet.observability.events import capture_event

_EVENT_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){1,2}$", re.ASCII)
_TOKEN_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$", re.ASCII)
_IDENTIFIER_PATTERN = re.compile(r"^[0-9a-f]{32}$", re.ASCII)
_ERROR_TYPE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,95}$", re.ASCII)
_TASK_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$", re.ASCII)
_SETTING_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,95}$", re.ASCII)
_UVICORN_CHILD_EXIT_PATTERN = re.compile(
    r"^Child process \[([1-9][0-9]{0,9})\] died$", re.ASCII
)

_MAX_LINE_BYTES = 4096
_MAX_MESSAGE_BYTES = 96
_MAX_TEXT_BYTES = 128
_MAX_BUSINESS_FIELDS = 16
_MAX_BUSINESS_BYTES = 3072
_MAX_ERROR_MESSAGE_BYTES = 512
_MAX_DEBUG_STACK_BYTES = 768
_MAX_COUNTER = 2**63 - 1

_PROCESS_ROLES = frozenset(
    {
        "supervisor",
        "web_master",
        "web_worker",
        "executor_worker",
        "cometnet",
        "cli",
        "usenet_engine",
    }
)
_OUTCOMES = frozenset(
    {"ok", "partial", "failed", "cancelled", "timeout", "rejected", "skipped"}
)
_PROFILE_RANK = {"quiet": 0, "normal": 1, "verbose": 2, "debug": 3}
_TERMINAL_POLICY = {
    "ok": ("INFO", "normal"),
    "skipped": ("INFO", "verbose"),
    "rejected": ("INFO", "verbose"),
    "cancelled": ("INFO", "verbose"),
    "partial": ("WARNING", "quiet"),
    "timeout": ("WARNING", "quiet"),
    "failed": ("ERROR", "quiet"),
}
_LEVEL_COLORS = {
    "DEBUG": "\x1b[36m",
    "INFO": "\x1b[32m",
    "WARNING": "\x1b[33m",
    "ERROR": "\x1b[31m",
    "CRITICAL": "\x1b[1;31m",
}
_RESET_COLOR = "\x1b[0m"
_CATEGORY_STYLES = {
    "API": ("👾", "\x1b[38;5;31m", "#22d3ee"),
    "BACKGROUND": ("🏭", "\x1b[38;5;71m", "#5fba64"),
    "COMET": ("🌠", "\x1b[38;5;99m", "#a78bfa"),
    "COMETNET": ("🌐", "\x1b[38;5;39m", "#38bdf8"),
    "DATABASE": ("💾", "\x1b[38;5;75m", "#60a5fa"),
    "DEBRID": ("⚡", "\x1b[38;5;45m", "#4dd4ff"),
    "FILTER": ("🛡️", "\x1b[38;5;220m", "#facc15"),
    "PLAYBACK": ("▶️", "\x1b[38;5;49m", "#34d399"),
    "SCRAPER": ("👻", "\x1b[38;5;179m", "#d6bb71"),
    "STREAM": ("🎬", "\x1b[38;5;176m", "#d171d6"),
    "SYSTEM": ("⚙️", "\x1b[38;5;245m", "#9ca3af"),
    "USENET": ("📦", "\x1b[38;5;208m", "#fb923c"),
}
_FIELD_LABELS = {
    "attempt_count": "attempts",
    "backoff_ms": "retry_in",
    "candidate_count": "candidates",
    "cache_state": "cache",
    "client_type": "client",
    "content_id": "content",
    "database_backend": "backend",
    "debrid_service": "service",
    "downtime_ms": "downtime",
    "duration_ms": "duration",
    "engine_generation": "generation",
    "error_code": "error",
    "error_message": "exception_message",
    "error_type": "exception",
    "exit_code": "exit",
    "expected_title": "expected",
    "expected_year_max": "year_max",
    "expected_year_min": "year_min",
    "failure_count": "failed",
    "failure_reason": "reason",
    "generated_secret": "secret",
    "http_method": "method",
    "http_status": "status",
    "item_count": "items",
    "log_profile": "profile",
    "media_type": "type",
    "migration_count": "migrations",
    "operation": "operation",
    "peer_id": "peer",
    "pool_id": "pool",
    "parsed_title": "parsed",
    "parsed_year": "year",
    "playback_mode": "mode",
    "preparation_state": "state",
    "provider_name": "provider",
    "release_title": "release",
    "request_id": "request",
    "requested_count": "requested",
    "replica_slot": "replica",
    "response_bytes": "bytes",
    "result_count": "results",
    "route_name": "route",
    "run_id": "run",
    "success_count": "succeeded",
    "suppressed_count": "suppressed",
    "setting_name": "setting",
    "source_type": "source",
    "torrent_count": "torrents",
    "transfer_mode": "transfer",
    "transferred_bytes": "bytes",
    "uptime_ms": "uptime",
    "worker_count": "workers",
}
_FIELD_PRIORITIES = {
    "content_id": 0,
    "release_title": 1,
    "parsed_title": 2,
    "expected_title": 3,
    "parsed_year": 4,
    "expected_year_min": 5,
    "expected_year_max": 6,
    "detected_language": 7,
    "provider_name": 8,
    "debrid_service": 9,
    "operation": 10,
    "cache_state": 11,
    "requested_count": 12,
    "candidate_count": 13,
    "duration_ms": 100,
}
_DASHBOARD_LOG_LIMIT = 1_000


class LogValidationError(ValueError):
    pass


class LogProfile(str, Enum):
    QUIET = "quiet"
    NORMAL = "normal"
    VERBOSE = "verbose"
    DEBUG = "debug"


class LogFormat(str, Enum):
    PRETTY = "pretty"
    JSON = "json"


class LoggingSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
        frozen=True,
    )

    LOG_PROFILE: LogProfile = LogProfile.NORMAL
    LOG_FORMAT: LogFormat = LogFormat.PRETTY
    NO_COLOR: str | None = None

    @field_validator("LOG_PROFILE", mode="before")
    @classmethod
    def default_empty_log_profile(cls, value):
        return LogProfile.NORMAL if value == "" else value

    @field_validator("LOG_FORMAT", mode="before")
    @classmethod
    def default_empty_log_format(cls, value):
        return LogFormat.PRETTY if value == "" else value

    @model_validator(mode="after")
    def reject_unsafe_no_color(self) -> LoggingSettings:
        if self.NO_COLOR is not None:
            _validate_text(self.NO_COLOR, maximum_bytes=_MAX_TEXT_BYTES)
        return self

    @property
    def no_color(self) -> bool:
        return "NO_COLOR" in self.model_fields_set


def _validate_text(value: object, *, maximum_bytes: int = _MAX_TEXT_BYTES) -> str:
    if type(value) is not str:
        raise LogValidationError("text_type")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        raise LogValidationError("text_encoding") from None
    if len(encoded) > maximum_bytes:
        raise LogValidationError("text_length")
    if any(unicodedata.category(character) in {"Cc", "Cf"} for character in value):
        raise LogValidationError("text_control")
    return value


def _validate_token(value: object) -> str:
    value = _validate_text(value, maximum_bytes=64)
    if _TOKEN_PATTERN.fullmatch(value) is None:
        raise LogValidationError("token")
    return value


def _enum(*values: str) -> Callable[[object], str]:
    allowed = frozenset(values)

    def validate(value: object) -> str:
        value = _validate_token(value)
        if value not in allowed:
            raise LogValidationError("enum")
        return value

    return validate


def _validate_bool(value: object) -> bool:
    if type(value) is not bool:
        raise LogValidationError("bool")
    return value


def _validate_non_negative(value: object) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_COUNTER:
        raise LogValidationError("non_negative_integer")
    return value


def _validate_positive(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_COUNTER:
        raise LogValidationError("positive_integer")
    return value


def _validate_duration(value: object) -> int | float:
    if type(value) not in {int, float} or value < 0:
        raise LogValidationError("duration")
    if isinstance(value, float) and not math.isfinite(value):
        raise LogValidationError("duration")
    return value


def _validate_ratio(value: object) -> int | float:
    if type(value) not in {int, float} or not 0 <= value <= 1:
        raise LogValidationError("ratio")
    if isinstance(value, float) and not math.isfinite(value):
        raise LogValidationError("ratio")
    return value


def _validate_identifier(value: object) -> str:
    value = _validate_text(value, maximum_bytes=32)
    if _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise LogValidationError("identifier")
    return value


def _validate_build_revision(value: object) -> str:
    value = _validate_text(value, maximum_bytes=40)
    if (normalized := normalize_commit(value)) is None:
        raise LogValidationError("build_revision")
    return normalized


def _validate_task_name(value: object) -> str:
    value = _validate_text(value, maximum_bytes=64)
    if _TASK_NAME_PATTERN.fullmatch(value) is None:
        raise LogValidationError("task_name")
    return value


def _validate_setting_name(value: object) -> str:
    value = _validate_text(value, maximum_bytes=96)
    if value == "configuration" or _SETTING_NAME_PATTERN.fullmatch(value):
        return value
    raise LogValidationError("setting_name")


def _bounded_field_text(maximum_bytes: int) -> Callable[[object], str]:
    def validate(value: object) -> str:
        return _validate_text(value, maximum_bytes=maximum_bytes)

    return validate


_COUNT_FIELDS = (
    "article_count",
    "attempt_count",
    "authentication_failure_count",
    "candidate_count",
    "failure_count",
    "invalid_count",
    "item_count",
    "migration_count",
    "missing_count",
    "expected_year_max",
    "expected_year_min",
    "parsed_year",
    "result_count",
    "requested_count",
    "success_count",
    "suppressed_count",
    "torrent_count",
    "worker_count",
    "worker_pid",
)
_BYTE_FIELDS = (
    "response_bytes",
    "transferred_bytes",
)
_DURATION_FIELDS = (
    "backoff_ms",
    "downtime_ms",
    "duration_ms",
    "timeout_ms",
    "uptime_ms",
)
_BOOL_FIELDS = ("retryable",)
_SLOT_FIELDS = ("replica_slot",)

# This is the only runtime registry for caller-supplied fields.  Validators are
# deliberately narrow; adding a new field is a reviewed contract change.
FIELD_SPECS: dict[str, Callable[[object], object]] = {
    **{name: _validate_non_negative for name in _COUNT_FIELDS},
    **{name: _validate_non_negative for name in _BYTE_FIELDS},
    **{name: _validate_duration for name in _DURATION_FIELDS},
    **{name: _validate_bool for name in _BOOL_FIELDS},
    **{name: _validate_positive for name in _SLOT_FIELDS},
    "build_revision": _validate_build_revision,
    "cache_state": _validate_token,
    "client_type": _validate_token,
    "content_id": _bounded_field_text(256),
    "database_backend": _enum("sqlite", "postgresql"),
    "debrid_service": _validate_token,
    "detected_language": _bounded_field_text(32),
    "details": _bounded_field_text(1024),
    "error_code": _validate_token,
    "expected_title": _bounded_field_text(512),
    "exit_code": _validate_non_negative,
    "failure_reason": _validate_token,
    "http_method": _enum("delete", "get", "head", "options", "patch", "post", "put"),
    "http_status": _validate_non_negative,
    "log_format": _enum("pretty", "json"),
    "log_profile": _enum("quiet", "normal", "verbose", "debug"),
    "media_type": _validate_token,
    "operation": _validate_token,
    "peer_id": _bounded_field_text(128),
    "outcome": _enum(*sorted(_OUTCOMES)),
    "parsed_title": _bounded_field_text(512),
    "playback_mode": _validate_token,
    "preparation_state": _validate_token,
    "process_role": _enum(*sorted(_PROCESS_ROLES)),
    "provider_name": _bounded_field_text(128),
    "provider_host": _bounded_field_text(256),
    "pool_id": _bounded_field_text(64),
    "release_title": _bounded_field_text(512),
    "filter_reason": _enum(
        "adult_content",
        "alias_language",
        "empty_release",
        "missing_title",
        "parse_error",
        "title_mismatch",
        "year_mismatch",
    ),
    "route_name": _validate_token,
    "generated_secret": _bounded_field_text(256),
    "source_type": _validate_token,
    "setting_name": _validate_setting_name,
    "signal": _validate_positive,
    "task_name": _validate_task_name,
    "transfer_mode": _enum("full", "range"),
    "worker_pid": _validate_positive,
}

_RESERVED_FIELDS = frozenset(
    {
        "timestamp",
        "level",
        "event",
        "message",
        "category",
        "module",
        "function",
        "line",
        "error_message",
        "error_type",
        "debug_stack",
        "request_id",
        "run_id",
        "connection_id",
        "process_role",
        "pid",
        "engine_generation",
    }
)
_EMERGENCY_EVENTS = {
    "logging.record.rejected": ("ERROR", "Logging record rejected"),
    "logging.renderer.failed": ("ERROR", "Logging renderer failed"),
    "logging.sink.failed": ("ERROR", "Logging sink failed"),
    "runtime.bootstrap.failed": ("CRITICAL", "Runtime bootstrap failed"),
}

_configuration_lock = threading.RLock()
_configured = False
_strict = False
_settings = LoggingSettings.model_construct(
    LOG_PROFILE=LogProfile.NORMAL,
    LOG_FORMAT=LogFormat.PRETTY,
    NO_COLOR=None,
    _fields_set=set(),
)
_process_role = "web_worker"
_engine_generation: int | None = None
_emergency_active = False
_emergency_seen: set[str] = set()
_dashboard_logs: deque[dict[str, object]] = deque(maxlen=_DASHBOARD_LOG_LIMIT)
_dashboard_logs_lock = threading.Lock()
_dashboard_log_sequence = 0


def _utc_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _event_category(event: str) -> str:
    prefix = event.split(".", 1)[0]
    if prefix in {"discovery", "scrape", "search"}:
        return "SCRAPER"
    if prefix == "filter":
        return "FILTER"
    if prefix == "debrid":
        return "DEBRID"
    if prefix == "playback":
        return "PLAYBACK"
    if prefix == "stream":
        return "STREAM"
    if prefix in {"database", "db", "migration", "replica"}:
        return "DATABASE"
    if prefix in {"native", "native_engine", "usenet"}:
        return "USENET"
    if prefix in {"http", "request"}:
        return "API"
    if prefix == "cometnet":
        return "COMETNET"
    if prefix in {"background", "dmm"}:
        return "BACKGROUND"
    if prefix in {"config", "runtime", "web_master", "web_worker"}:
        return "COMET"
    return "SYSTEM"


def _clear_dashboard_logs() -> None:
    global _dashboard_log_sequence
    with _dashboard_logs_lock:
        _dashboard_logs.clear()
        _dashboard_log_sequence = 0


def _capture_dashboard_record(record: Mapping[str, object], details: str) -> None:
    global _dashboard_log_sequence
    category = str(record["category"])
    icon, _ansi, color = _CATEGORY_STYLES[category]
    with _dashboard_logs_lock:
        _dashboard_log_sequence += 1
        _dashboard_logs.append(
            {
                "timestamp": record["timestamp"],
                "level": record["level"],
                "message": record["message"],
                "category": category,
                "details": details,
                "sequence": _dashboard_log_sequence,
                "created": time.time(),
                "icon": icon,
                "color": color,
            }
        )


def recent_logs(*, since: int = 0, process_id: int | None = None) -> dict[str, object]:
    """Return a detached, bounded snapshot for the authenticated admin UI."""

    if type(since) is not int or since < 0:
        raise ValueError("since must be a non-negative integer")
    if process_id is not None and (type(process_id) is not int or process_id < 1):
        raise ValueError("process_id must be a positive integer")
    with _dashboard_logs_lock:
        records = list(_dashboard_logs)
    newest = records[-1]["sequence"] if records else 0
    # A request may land on another Gunicorn worker. In that case the local
    # sequence can be behind the browser cursor, so return its complete ring.
    if process_id not in {None, os.getpid()} or since > newest:
        since = 0
    return {
        "logs": [dict(record) for record in records if record["sequence"] > since],
        "total_logs": len(records),
        "latest_sequence": newest,
        "process_id": os.getpid(),
    }


def _emergency_write(event: str, *, once_key: str | None = None) -> None:
    global _emergency_active
    if event not in _EMERGENCY_EVENTS:
        event = "runtime.bootstrap.failed"
    with _configuration_lock:
        if _emergency_active:
            return
        if once_key is not None:
            if once_key in _emergency_seen:
                return
            if len(_emergency_seen) >= 128:
                return
            _emergency_seen.add(once_key)
        _emergency_active = True
    try:
        level, message = _EMERGENCY_EVENTS[event]
        timestamp = _utc_timestamp()
        if _settings.LOG_FORMAT == LogFormat.JSON:
            # All interpolated values originate from the closed tables above.
            payload = (
                f'{{"timestamp":"{timestamp}","level":"{level}",'
                f'"event":"{event}","message":"{message}"}}\n'
            ).encode("ascii")
        else:
            payload = f"{timestamp} {level} {event} - {message}\n".encode("ascii")
        if len(payload) <= _MAX_LINE_BYTES:
            os.write(2, payload)
    except Exception:
        pass
    finally:
        with _configuration_lock:
            _emergency_active = False


def bootstrap_failure() -> None:
    _emergency_write("runtime.bootstrap.failed")


def configure_entrypoint(*, process_role: str) -> LoggingSettings:
    """Configure logging or terminate without exposing a settings failure."""

    try:
        return configure(process_role=process_role)
    except Exception:
        bootstrap_failure()
        raise SystemExit(78) from None


def configuration_invalid(
    *,
    exception: BaseException | None = None,
    setting_name: str = "configuration",
    invalid_count: int = 1,
) -> None:
    """Emit the sole safe terminal for invalid application configuration."""

    try:
        if exception is not None:
            candidate_names: list[str] = []
            validation_reasons: list[str] = []
            errors_method = getattr(exception, "errors", None)
            if callable(errors_method):
                errors = errors_method(
                    include_url=False,
                    include_context=False,
                    include_input=False,
                )
                if isinstance(errors, list):
                    invalid_count = max(1, min(len(errors), _MAX_COUNTER))
                    for error in errors:
                        location = error.get("loc") if isinstance(error, dict) else None
                        reason = error.get("msg") if isinstance(error, dict) else None
                        if isinstance(reason, str):
                            reason = reason.removeprefix("Value error, ")
                            try:
                                validation_reasons.append(
                                    _validate_text(reason, maximum_bytes=512)
                                )
                            except LogValidationError:
                                pass
                        candidate = (
                            location[0]
                            if isinstance(location, (tuple, list)) and location
                            else None
                        )
                        if (
                            isinstance(candidate, str)
                            and _SETTING_NAME_PATTERN.fullmatch(candidate) is not None
                        ):
                            candidate_names.append(candidate)
            if candidate_names:
                setting_name = min(candidate_names)
            details = (
                validation_reasons[0]
                if validation_reasons
                else f"configuration loader failed ({type(exception).__name__})"
            )
        else:
            details = "configuration validation failed"
        log.critical(
            "config.invalid",
            message="Application configuration is invalid",
            setting_name=setting_name,
            invalid_count=invalid_count,
            error_code="invalid_configuration",
            details=details,
        )
    except Exception:
        bootstrap_failure()


def install_bootstrap_safety() -> None:
    root = stdlib_logging.getLogger()
    root.handlers[:] = [stdlib_logging.NullHandler()]
    root.setLevel(stdlib_logging.CRITICAL + 1)
    stdlib_logging.lastResort = None
    stdlib_logging.raiseExceptions = False
    warnings.showwarning = lambda *_args, **_kwargs: None


def _resolve_settings() -> LoggingSettings:
    from comet.core.operator_settings import effective_model_inputs

    return LoggingSettings(
        **effective_model_inputs(frozenset(LoggingSettings.model_fields))
    )


def configure(
    settings: LoggingSettings | None = None,
    *,
    process_role: str,
    strict: bool = False,
) -> LoggingSettings:
    global _configured, _settings, _strict
    install_bootstrap_safety()
    resolved = settings or _resolve_settings()
    set_process_role(process_role)
    with _configuration_lock:
        _settings = resolved
        _strict = strict
        _backend.remove()
        _backend.configure(
            handlers=[
                {
                    "sink": _loguru_sink,
                    "level": "DEBUG",
                    "format": "{message}",
                    "colorize": False,
                    "backtrace": False,
                    "diagnose": False,
                    "enqueue": False,
                    "catch": False,
                }
            ]
        )
        _clear_dashboard_logs()
        _configured = True
    install_safe_hooks()
    return resolved


def ensure_configured(*, process_role: str = "web_worker") -> LoggingSettings:
    if _configured:
        set_process_role(process_role)
        return _settings
    return configure(process_role=process_role)


def current_settings() -> LoggingSettings:
    return _settings


def set_process_role(role: str) -> None:
    global _process_role
    if role not in _PROCESS_ROLES:
        raise LogValidationError("process_role")
    _process_role = role


def current_process_role() -> str:
    return _process_role


def set_engine_generation(generation: int | None) -> None:
    global _engine_generation
    if generation is not None and (
        type(generation) is not int or generation <= 0 or generation > _MAX_COUNTER
    ):
        raise LogValidationError("engine_generation")
    _engine_generation = generation


def _profile_enabled(minimum: str) -> bool:
    return _PROFILE_RANK[_settings.LOG_PROFILE.value] >= _PROFILE_RANK[minimum]


def _caller() -> tuple[str, str, int]:
    frame = inspect.currentframe()
    try:
        # _caller -> façade method -> business call site.
        caller = frame.f_back.f_back if frame and frame.f_back else None
        if caller is None:
            return ("unknown", "unknown", 0)
        module = caller.f_globals.get("__name__", "unknown").rsplit(".", 1)[-1]
        function = caller.f_code.co_name
        line = caller.f_lineno
        return (
            _safe_source_token(module),
            _safe_source_token(function),
            max(0, line),
        )
    finally:
        del frame


def _safe_source_token(value: object) -> str:
    if not isinstance(value, str):
        return "unknown"
    cleaned = re.sub(r"[^A-Za-z0-9_<>]", "_", value)[:96]
    return cleaned or "unknown"


def _diagnostic_text(value: object) -> str | None:
    try:
        message = str(value)
    except Exception:
        return None
    if not message:
        return None

    parts: list[tuple[str, int]] = []
    size = 0
    truncated = False
    for character in message:
        rendered = (
            character
            if unicodedata.category(character) not in {"Cc", "Cf"}
            else character.encode("unicode_escape").decode("ascii")
        )
        rendered_size = len(rendered.encode("utf-8"))
        if size + rendered_size > _MAX_ERROR_MESSAGE_BYTES:
            truncated = True
            break
        parts.append((rendered, rendered_size))
        size += rendered_size

    if truncated:
        while parts and size + 3 > _MAX_ERROR_MESSAGE_BYTES:
            _, rendered_size = parts.pop()
            size -= rendered_size
        parts.append(("...", 3))
    return "".join(part for part, _ in parts)


def _safe_exception(
    exc: BaseException,
) -> tuple[str, str | None, str | None]:
    error_type = type(exc).__name__
    if _ERROR_TYPE_PATTERN.fullmatch(error_type) is None:
        error_type = "Exception"
    message = _diagnostic_text(exc)
    if _settings.LOG_PROFILE != LogProfile.DEBUG:
        return error_type, message, None
    frames: list[str] = []
    trace: TracebackType | None = exc.__traceback__
    for frame in traceback.extract_tb(trace, limit=8):
        module = _safe_source_token(
            os.path.splitext(os.path.basename(frame.filename))[0]
        )
        function = _safe_source_token(frame.name)
        frames.append(f"{module}:{function}:{max(0, frame.lineno)}")
    stack = " > ".join(frames)
    while len(stack.encode("utf-8")) > _MAX_DEBUG_STACK_BYTES and frames:
        frames.pop(0)
        stack = " > ".join(frames)
    return error_type, message, stack or None


def _validate_event_message(event: object, message: object) -> tuple[str, str]:
    event = _validate_text(event, maximum_bytes=64)
    if _EVENT_PATTERN.fullmatch(event) is None:
        raise LogValidationError("event")
    message = _validate_text(message, maximum_bytes=_MAX_MESSAGE_BYTES)
    if not message or message != message.strip():
        raise LogValidationError("message")
    return event, message


def _validate_fields(
    fields: Mapping[str, object],
) -> dict[str, str | bool | int | float | None]:
    if len(fields) > _MAX_BUSINESS_FIELDS:
        raise LogValidationError("field_count")
    validated: dict[str, str | bool | int | float | None] = {}
    for name, value in fields.items():
        if name in _RESERVED_FIELDS:
            raise LogValidationError("reserved_field")
        validator = FIELD_SPECS.get(name)
        if validator is None:
            raise LogValidationError("unknown_field")
        if value is None:
            continue
        normalized = validator(value)
        if type(normalized) not in {str, bool, int, float}:
            raise LogValidationError("field_type")
        validated[name] = normalized
    if len(orjson.dumps(validated)) > _MAX_BUSINESS_BYTES:
        raise LogValidationError("field_budget")
    return validated


def _field_sort_key(name: str) -> tuple[int, str]:
    return (_FIELD_PRIORITIES.get(name, len(_FIELD_PRIORITIES)), name)


def _make_record(
    *,
    level: str,
    event: str,
    message: str,
    fields: Mapping[str, object],
    exc: BaseException | None,
    source: tuple[str, str, int],
) -> dict[str, object]:
    event, message = _validate_event_message(event, message)
    validated = _validate_fields(fields)
    record: dict[str, object] = {
        "timestamp": _utc_timestamp(),
        "level": level,
        "event": event,
        "message": message,
        "category": _event_category(event),
        "process_role": _process_role,
        "pid": os.getpid(),
    }
    if _engine_generation is not None:
        record["engine_generation"] = _engine_generation
    request_id = current_request_id()
    run_id = current_run_id()
    connection_id = current_connection_id()
    if request_id is not None:
        record["request_id"] = _validate_identifier(request_id)
    if run_id is not None:
        record["run_id"] = _validate_identifier(run_id)
    if connection_id is not None:
        record["connection_id"] = _validate_identifier(connection_id)
    if "outcome" in validated:
        record["outcome"] = validated.pop("outcome")
    if exc is not None:
        error_type, error_message, debug_stack = _safe_exception(exc)
        record["error_type"] = error_type
        if error_message is not None:
            record["error_message"] = error_message
        if debug_stack is not None:
            record["debug_stack"] = debug_stack
    if _settings.LOG_PROFILE == LogProfile.DEBUG:
        record["module"], record["function"], record["line"] = source
    for name in sorted(validated, key=_field_sort_key):
        record[name] = validated[name]
    return record


def _pretty_value(value: object) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def _pretty_field(name: str, value: object) -> str:
    label = _FIELD_LABELS.get(name, name)
    if name == "details":
        return _pretty_value(value)
    if name in _DURATION_FIELDS and type(value) in {int, float}:
        return f"{label}={value:.1f}ms"
    if name in {"connection_id", "request_id", "run_id"} and isinstance(value, str):
        return f"{label}={value[:8]}"
    return f"{label}={_pretty_value(value)}"


_DISPLAY_BASE_FIELDS = frozenset(
    {
        "timestamp",
        "level",
        "event",
        "message",
        "category",
        "filter_reason",
        "process_role",
        "pid",
    }
)


def _is_display_field(name: str, value: object) -> bool:
    """Keep human-facing logs concise without weakening structured JSON."""

    if name in _DISPLAY_BASE_FIELDS:
        return False
    if name == "outcome" and value in {"failed", "ok"}:
        return False
    return not (
        name in {"failure_count", "success_count", "suppressed_count", "torrent_count"}
        and value == 0
    )


def _display_details(record: Mapping[str, object]) -> str:
    return " ".join(
        _pretty_field(key, value)
        for key, value in record.items()
        if _is_display_field(key, value)
    )


def _render(record: Mapping[str, object], *, details: str) -> bytes:
    if _settings.LOG_FORMAT == LogFormat.JSON:
        payload = orjson.dumps(record) + b"\n"
    else:
        level = str(record["level"])
        category = str(record["category"])
        icon, category_color, _dashboard_color = _CATEGORY_STYLES[category]
        color = not _settings.no_color
        rendered_level = (
            f"{_LEVEL_COLORS[level]}{level}{_RESET_COLOR}" if color else level
        )
        rendered_category = (
            f"{category_color}{icon} {category}{_RESET_COLOR}"
            if color
            else f"{icon} {category}"
        )
        suffix = f" | {details}" if details else ""
        payload = (
            f"{record['timestamp']} | {rendered_category} | {rendered_level} | "
            f"{record['message']}{suffix}\n"
        ).encode()
    if len(payload) > _MAX_LINE_BYTES:
        raise LogValidationError("line_budget")
    return payload


def _loguru_sink(message: Any) -> None:
    record = message.record["extra"]["comet_record"]
    try:
        details = _display_details(record)
        payload = _render(record, details=details)
    except Exception:
        _emergency_write("logging.renderer.failed", once_key="renderer")
        return
    _capture_dashboard_record(record, details)
    capture_event(record)
    try:
        written = os.write(2, payload)
        if written != len(payload):
            _emergency_write("logging.sink.failed", once_key="short_write")
    except Exception:
        _emergency_write("logging.sink.failed", once_key="sink")


def ingest_native_event(document: bytes) -> None:
    """Normalize one bounded native JSON event at the supervisor boundary."""

    try:
        if len(document) > _MAX_LINE_BYTES:
            raise LogValidationError("line_budget")
        value = orjson.loads(document)
        if not isinstance(value, dict):
            raise LogValidationError("record_type")
        required = {
            "timestamp",
            "level",
            "event",
            "message",
            "category",
            "process_role",
            "pid",
            "engine_generation",
        }
        if not required.issubset(value):
            raise LogValidationError("record_fields")
        timestamp = _validate_text(value.pop("timestamp"), maximum_bytes=19)
        time.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
        level = value.pop("level")
        if level not in _LEVEL_COLORS:
            raise LogValidationError("level")
        event, message = _validate_event_message(
            value.pop("event"),
            value.pop("message"),
        )
        if value.pop("category") != "USENET":
            raise LogValidationError("category")
        if value.pop("process_role") != "usenet_engine":
            raise LogValidationError("process_role")
        pid = _validate_positive(value.pop("pid"))
        generation = _validate_positive(value.pop("engine_generation"))
        request_id = value.pop("request_id", None)
        if request_id is not None:
            request_id = _validate_identifier(request_id)
        fields = _validate_fields(value)
        record: dict[str, object] = {
            "timestamp": timestamp,
            "level": level,
            "event": event,
            "message": message,
            "category": "USENET",
            "process_role": "usenet_engine",
            "pid": pid,
            "engine_generation": generation,
            **fields,
        }
        if request_id is not None:
            record["request_id"] = request_id
        _backend.bind(comet_record=record).log(level, "")
    except (LogValidationError, ValueError, orjson.JSONDecodeError) as error:
        _reject(f"native_{error}")


def _reject(reason: str) -> None:
    if _strict:
        raise LogValidationError(reason)
    _emergency_write("logging.record.rejected", once_key=reason)


class LogFacade:
    def enabled(self, detail: Literal["normal", "verbose", "debug"]) -> bool:
        if detail not in {"normal", "verbose", "debug"}:
            raise LogValidationError("profile")
        return _profile_enabled(detail)

    def _emit(
        self,
        *,
        minimum: str,
        level: str,
        event: str,
        message: str,
        exc: BaseException | None,
        source: tuple[str, str, int],
        fields: Mapping[str, object],
        prefiltered: bool = False,
    ) -> None:
        if not prefiltered and not _profile_enabled(minimum):
            return
        try:
            record = _make_record(
                level=level,
                event=event,
                message=message,
                fields=fields,
                exc=exc,
                source=source,
            )
            _backend.bind(comet_record=record).log(level, "")
        except LogValidationError as error:
            _reject(str(error))
        except Exception:
            _emergency_write("logging.renderer.failed", once_key="backend")

    def info(
        self,
        event: str,
        message: str,
        *,
        exc: BaseException | None = None,
        **fields: object,
    ) -> None:
        if not _profile_enabled("normal"):
            return
        self._emit(
            minimum="normal",
            level="INFO",
            event=event,
            message=message,
            exc=exc,
            source=(
                _caller() if _settings.LOG_PROFILE == LogProfile.DEBUG else ("", "", 0)
            ),
            fields=fields,
            prefiltered=True,
        )

    def verbose(
        self,
        event: str,
        message: str,
        *,
        exc: BaseException | None = None,
        **fields: object,
    ) -> None:
        if not _profile_enabled("verbose"):
            return
        self._emit(
            minimum="verbose",
            level="INFO",
            event=event,
            message=message,
            exc=exc,
            source=(
                _caller() if _settings.LOG_PROFILE == LogProfile.DEBUG else ("", "", 0)
            ),
            fields=fields,
            prefiltered=True,
        )

    def debug(
        self,
        event: str,
        message: str,
        *,
        exc: BaseException | None = None,
        **fields: object,
    ) -> None:
        if not _profile_enabled("debug"):
            return
        self._emit(
            minimum="debug",
            level="DEBUG",
            event=event,
            message=message,
            exc=exc,
            source=_caller(),
            fields=fields,
            prefiltered=True,
        )

    def warning(
        self,
        event: str,
        message: str,
        *,
        exc: BaseException | None = None,
        **fields: object,
    ) -> None:
        if not _profile_enabled("quiet"):
            return
        self._emit(
            minimum="quiet",
            level="WARNING",
            event=event,
            message=message,
            exc=exc,
            source=(
                _caller() if _settings.LOG_PROFILE == LogProfile.DEBUG else ("", "", 0)
            ),
            fields=fields,
            prefiltered=True,
        )

    def error(
        self,
        event: str,
        message: str,
        *,
        exc: BaseException | None = None,
        **fields: object,
    ) -> None:
        if not _profile_enabled("quiet"):
            return
        self._emit(
            minimum="quiet",
            level="ERROR",
            event=event,
            message=message,
            exc=exc,
            source=(
                _caller() if _settings.LOG_PROFILE == LogProfile.DEBUG else ("", "", 0)
            ),
            fields=fields,
            prefiltered=True,
        )

    def critical(
        self,
        event: str,
        message: str,
        *,
        exc: BaseException | None = None,
        **fields: object,
    ) -> None:
        if not _profile_enabled("quiet"):
            return
        self._emit(
            minimum="quiet",
            level="CRITICAL",
            event=event,
            message=message,
            exc=exc,
            source=(
                _caller() if _settings.LOG_PROFILE == LogProfile.DEBUG else ("", "", 0)
            ),
            fields=fields,
            prefiltered=True,
        )

    def terminal(
        self,
        event: str,
        message: str,
        *,
        outcome: str,
        exc: BaseException | None = None,
        transport_failure_explained: bool = False,
        **fields: object,
    ) -> None:
        try:
            event, message = _validate_event_message(event, message)
            if outcome not in _TERMINAL_POLICY:
                raise LogValidationError("outcome")
            if type(transport_failure_explained) is not bool:
                raise LogValidationError("transport_control")
            if transport_failure_explained and (
                event != "stream.completed"
                or outcome not in {"partial", "timeout", "failed", "cancelled"}
            ):
                raise LogValidationError("transport_control")
            validated = _validate_fields({"outcome": outcome, **fields})
            level, minimum = _TERMINAL_POLICY[outcome]
            # Build the complete record before mutating the HTTP flags.
            record = _make_record(
                level=level,
                event=event,
                message=message,
                fields=validated,
                exc=exc,
                source=(
                    _caller()
                    if _settings.LOG_PROFILE == LogProfile.DEBUG
                    else ("", "", 0)
                ),
            )
        except LogValidationError as error:
            _reject(str(error))
            return
        except Exception:
            _emergency_write("logging.renderer.failed", once_key="terminal")
            return

        flags = current_terminal_flags()
        if flags is not None:
            marks = TerminalFlag.BUSINESS_SEEN
            if outcome in {"partial", "timeout", "failed", "cancelled"}:
                marks |= TerminalFlag.BUSINESS_FAILURE_EXPLAINED
            if transport_failure_explained:
                marks |= TerminalFlag.TRANSPORT_FAILURE_EXPLAINED
            if not flags.admit_terminal(event, marks):
                _reject("duplicate_terminal")
                return
        if not _profile_enabled(minimum):
            return
        try:
            _backend.bind(comet_record=record).log(level, "")
        except Exception:
            _emergency_write("logging.renderer.failed", once_key="backend")


log = LogFacade()


def _dependency_failure(
    record: stdlib_logging.LogRecord,
) -> tuple[BaseException | None, str | None]:
    exception = record.msg if isinstance(record.msg, BaseException) else None
    if exception is None and record.exc_info is not None:
        candidate = record.exc_info[1]
        if isinstance(candidate, BaseException):
            exception = candidate
    if isinstance(record.msg, BaseException):
        return exception, None
    try:
        details = _diagnostic_text(record.getMessage())
    except Exception:
        details = None
    return exception, details


class _ClosedBridgeHandler(stdlib_logging.Handler):
    def emit(self, record: stdlib_logging.LogRecord) -> None:
        if record.name == "uvicorn.error":
            if record.msg == "Application startup failed. Exiting.":
                log.error(
                    "web_worker.exited",
                    message="Web worker exited unexpectedly",
                    error_code="startup_failure",
                )
                return
            if type(record.msg) is str:
                match = _UVICORN_CHILD_EXIT_PATTERN.fullmatch(record.msg)
                if match is not None:
                    log.error(
                        "web_worker.exited",
                        message="Web worker exited unexpectedly",
                        worker_pid=int(match.group(1)),
                        error_code="worker_exit",
                    )
                    return
            if record.levelno >= stdlib_logging.ERROR:
                exception, details = _dependency_failure(record)
                log.error(
                    "dependency.uvicorn.failed",
                    message="Uvicorn reported a failure",
                    error_code="dependency_failure",
                    details=details,
                    exc=exception,
                )
            return
        if record.name == "gunicorn.error":
            if record.levelno >= stdlib_logging.ERROR:
                exception, details = _dependency_failure(record)
                log.error(
                    "dependency.gunicorn.failed",
                    message="Gunicorn reported a failure",
                    error_code="dependency_failure",
                    details=details,
                    exc=exception,
                )
            return
        if record.levelno < stdlib_logging.WARNING:
            return
        failed = record.levelno >= stdlib_logging.ERROR
        if record.name == "asyncio":
            if failed:
                log.error(
                    "dependency.asyncio.failed",
                    message="Async runtime dependency failed",
                    error_code="dependency_failure",
                )
            else:
                log.warning(
                    "dependency.asyncio.degraded",
                    message="Async runtime dependency degraded",
                    error_code="dependency_warning",
                )
        elif record.name == "demagnetize":
            if failed:
                log.error(
                    "dependency.demagnetize.failed",
                    message="Demagnetize dependency failed",
                    error_code="dependency_failure",
                )
            else:
                log.warning(
                    "dependency.demagnetize.degraded",
                    message="Demagnetize dependency degraded",
                    error_code="dependency_warning",
                )
        elif record.name == "websockets.server":
            if failed:
                log.error(
                    "dependency.websockets.failed",
                    message="WebSocket dependency failed",
                    error_code="dependency_failure",
                )
            else:
                log.warning(
                    "dependency.websockets.degraded",
                    message="WebSocket dependency degraded",
                    error_code="dependency_warning",
                )

    def handleError(self, record: stdlib_logging.LogRecord) -> None:
        return


def configure_stdlib_bridge() -> None:
    for name in ("uvicorn.access", "gunicorn.access"):
        dependency_logger = stdlib_logging.getLogger(name)
        dependency_logger.handlers.clear()
        dependency_logger.propagate = False
        dependency_logger.disabled = True
    for name in (
        "uvicorn.error",
        "gunicorn.error",
        "asyncio",
        "websockets.server",
        "demagnetize",
    ):
        dependency_logger = stdlib_logging.getLogger(name)
        dependency_logger.handlers[:] = [_ClosedBridgeHandler()]
        dependency_logger.setLevel(stdlib_logging.WARNING)
        dependency_logger.propagate = False


def _hook_exception_admitted(exception: BaseException | None) -> bool:
    return exception is not None and not isinstance(
        exception,
        (
            asyncio.CancelledError,
            SystemExit,
            KeyboardInterrupt,
            GeneratorExit,
        ),
    )


def install_safe_hooks() -> None:
    def excepthook(
        _exception_type: type[BaseException],
        exception: BaseException,
        _traceback: TracebackType | None,
    ) -> None:
        if _hook_exception_admitted(exception):
            log.critical(
                "runtime.exception.unhandled",
                message="Unhandled runtime exception",
                error_code="unhandled_exception",
                exc=exception,
            )

    def threading_hook(args: threading.ExceptHookArgs) -> None:
        if _hook_exception_admitted(args.exc_value):
            log.critical(
                "runtime.thread.failed",
                message="Unhandled thread exception",
                error_code="thread_failure",
                exc=args.exc_value,
            )

    def unraisable_hook(args: sys.UnraisableHookArgs) -> None:
        if _hook_exception_admitted(args.exc_value):
            log.critical(
                "runtime.unraisable.detected",
                message="Unhandled unraisable exception",
                error_code="unraisable_exception",
                exc=args.exc_value,
            )

    sys.excepthook = excepthook
    threading.excepthook = threading_hook
    sys.unraisablehook = unraisable_hook
    configure_stdlib_bridge()


install_bootstrap_safety()
_backend.remove()

__all__ = (
    "FIELD_SPECS",
    "LogFacade",
    "LogFormat",
    "LogProfile",
    "LogValidationError",
    "LoggingSettings",
    "bootstrap_failure",
    "configuration_invalid",
    "configure",
    "configure_entrypoint",
    "current_process_role",
    "current_settings",
    "ensure_configured",
    "install_bootstrap_safety",
    "install_safe_hooks",
    "log",
    "recent_logs",
    "set_engine_generation",
    "set_process_role",
)
