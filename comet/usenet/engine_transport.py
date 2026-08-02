"""Bounded HTTP transport for the replica-local Usenet engine."""

import asyncio
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path

import orjson

from comet.observability import current_request_id, log
from comet.usenet.identity import is_sha256_hex
from comet.usenet.limits import (
    MAX_NZB_METADATA_BYTES,
    MAX_UNIX_SOCKET_PATH_BYTES,
)

MAX_ENGINE_HEADER_BYTES = 16 * 1024
MAX_ENGINE_CONTROL_BYTES = 1024 * 1024
MAX_ENGINE_PROVIDER_SET_BYTES = 2 * 1024 * 1024
MAX_ENGINE_NATIVE_CATALOG_BYTES = 2 * 1024 * 1024
MAX_ENGINE_NZB_METADATA_BYTES = MAX_NZB_METADATA_BYTES
MAX_ENGINE_RANGE_BYTES = 8 * 1024 * 1024
MAX_ENGINE_CONTROL_RESPONSE_SECONDS = 5
MAX_ENGINE_SESSION_RESPONSE_SECONDS = 35
MAX_ENGINE_REQUEST_WRITE_SECONDS = 35
MAX_ENGINE_CLOSE_SECONDS = 1
MAX_ENGINE_DESCRIPTOR_BYTES = 4 * 1024

_ENGINE_FAILURE_KEYS = frozenset({"version", "code", "retryable"})
_ENGINE_OPERATIONS = {
    ("GET", "/v1/health"): "health",
    ("GET", "/v1/stats"): "stats",
    ("POST", "/v1/drain"): "drain",
    ("POST", "/v1/resume"): "resume",
    ("POST", "/v1/archive-plan"): "archive_plan",
    ("POST", "/v1/archive-direct/catalog"): "archive_catalog",
    ("POST", "/v1/archive-direct/open"): "archive_open",
    ("POST", "/v1/session-archives/catalog"): "session_archive_catalog",
    ("POST", "/v1/session-archives/open"): "session_archive_open",
    ("POST", "/v1/archive-nested/catalog"): "nested_catalog",
    ("POST", "/v1/archive-nested/extract"): "nested_extract",
    ("POST", "/v1/par2/discover"): "par2_discover",
    ("POST", "/v1/par2/map-sources"): "par2_map",
    ("POST", "/v1/par2/repair"): "par2_repair",
}
_POLLING_OPERATIONS = frozenset({"health", "stats"})
_QUIET_SUCCESS_OPERATIONS = _POLLING_OPERATIONS | {"session"}
_ADMISSION_FAILURE_REASONS = frozenset({"archive_busy", "native_busy", "repair_busy"})
_QUIET_FAILURE_REASONS = frozenset({"nntp_cancelled"}) | _ADMISSION_FAILURE_REASONS
_UNBOUNDED_WORK_OPERATIONS = frozenset(
    {
        "archive_plan",
        "archive_catalog",
        "archive_open",
        "session_archive_catalog",
        "session_archive_open",
        "nested_catalog",
        "nested_extract",
        "par2_discover",
        "par2_map",
        "par2_repair",
        "materialization",
        "native_inspect",
    }
)


class EngineUnavailable(RuntimeError):
    pass


def _has_fields(value: object, fields: set[str]) -> bool:
    return isinstance(value, dict) and fields <= value.keys()


def _decode_engine_failure(
    response: object,
    *,
    label: str,
) -> tuple[str, bool]:
    if (
        not _has_fields(response, _ENGINE_FAILURE_KEYS)
        or type(response.get("version")) is not int
        or response["version"] != 1
        or not isinstance(response.get("code"), str)
        or not response["code"]
        or len(response["code"]) > 128
        or not isinstance(response.get("retryable"), bool)
    ):
        raise EngineUnavailable(f"Usenet engine returned invalid {label} failure data")
    return response["code"], response["retryable"]


def _engine_operation(method: str, path: str) -> str:
    if operation := _ENGINE_OPERATIONS.get((method, path)):
        return operation
    if path.startswith("/v1/artifacts/"):
        if path.endswith("/parse"):
            return "nzb_parse"
        if path.endswith("/native-catalog"):
            return "native_catalog"
        if path.endswith("/native-inspect"):
            return "native_inspect"
    if path.startswith("/v1/materializations"):
        return "materialization"
    if path.startswith("/v1/raw-composites"):
        return "raw_composite"
    if path.startswith("/v1/sessions"):
        return "session"
    if path.startswith("/v1/provider-sets"):
        return "provider_set"
    return "request"


def _log_engine_request_failure(
    operation: str,
    started_at: int,
    exception: BaseException,
) -> None:
    if operation in _POLLING_OPERATIONS:
        return
    log.warning(
        "usenet.engine.failed",
        "Usenet engine request failed",
        operation=operation,
        duration_ms=(time.monotonic_ns() - started_at) / 1_000_000,
        error_code="engine_unavailable",
        exc=exception,
    )


def _maximum_response_bytes(path: str) -> int:
    if path.startswith("/v1/artifacts/") and path.endswith("/parse"):
        artifact_sha256 = path.removeprefix("/v1/artifacts/").removesuffix("/parse")
        if is_sha256_hex(artifact_sha256):
            return MAX_ENGINE_NZB_METADATA_BYTES
    if path.startswith("/v1/raw-composites/") and path.endswith("/read"):
        return MAX_ENGINE_RANGE_BYTES
    if path.startswith("/v1/sessions/") and path.endswith("/read"):
        return MAX_ENGINE_RANGE_BYTES
    if path.startswith("/v1/artifacts/") and path.endswith("/native-catalog"):
        return MAX_ENGINE_NATIVE_CATALOG_BYTES
    if path in {
        "/v1/archive-direct/catalog",
        "/v1/archive-nested/catalog",
        "/v1/session-archives/catalog",
        "/v1/par2/discover",
        "/v1/par2/map-sources",
        "/v1/par2/repair",
    }:
        return MAX_ENGINE_NATIVE_CATALOG_BYTES
    return MAX_ENGINE_CONTROL_BYTES


def _response_timeout(path: str) -> int | None:
    if _engine_operation("POST", path) in _UNBOUNDED_WORK_OPERATIONS:
        return None
    if (
        path == "/v1/sessions"
        or path.startswith("/v1/sessions/")
        and path.endswith("/read")
        or path.startswith("/v1/raw-composites/")
        and path.endswith("/read")
    ):
        return MAX_ENGINE_SESSION_RESPONSE_SECONDS
    return MAX_ENGINE_CONTROL_RESPONSE_SECONDS


@dataclass(frozen=True)
class EngineDescriptor:
    version: int
    socket_path: str
    runtime_id: str
    api_version: int

    @classmethod
    def load(cls, path: str | Path) -> "EngineDescriptor":
        file_fd = None
        try:
            file_fd = os.open(
                path,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
            descriptor_stat = os.fstat(file_fd)
            if (
                not stat.S_ISREG(descriptor_stat.st_mode)
                or stat.S_IMODE(descriptor_stat.st_mode) & 0o077
            ):
                raise OSError("invalid descriptor file")
            with os.fdopen(file_fd, "rb", closefd=False) as descriptor_file:
                document = descriptor_file.read(MAX_ENGINE_DESCRIPTOR_BYTES + 1)
            if len(document) > MAX_ENGINE_DESCRIPTOR_BYTES:
                raise ValueError("oversized descriptor")
            payload = orjson.loads(document)
            if not _has_fields(
                payload,
                {"version", "socket_path", "runtime_id", "api_version"},
            ):
                raise ValueError("invalid descriptor shape")
            descriptor = cls(
                version=payload["version"],
                socket_path=payload["socket_path"],
                runtime_id=payload["runtime_id"],
                api_version=payload["api_version"],
            )
        except (OSError, ValueError, KeyError, TypeError):
            raise EngineUnavailable("Usenet engine descriptor is unavailable") from None
        finally:
            if file_fd is not None:
                os.close(file_fd)
        try:
            socket_path = os.fsencode(descriptor.socket_path)
        except (TypeError, UnicodeEncodeError):
            socket_path = b""
        if (
            type(descriptor.version) is not int
            or descriptor.version != 1
            or type(descriptor.api_version) is not int
            or descriptor.api_version != 1
            or not isinstance(descriptor.socket_path, str)
            or not Path(descriptor.socket_path).is_absolute()
            or b"\0" in socket_path
            or len(socket_path) > MAX_UNIX_SOCKET_PATH_BYTES
            or not isinstance(descriptor.runtime_id, str)
            or not 16 <= len(descriptor.runtime_id) <= 64
        ):
            raise EngineUnavailable("Usenet engine descriptor is incompatible")
        return descriptor


class EngineTransport:
    def __init__(self, descriptor_path: str | Path):
        self._descriptor_path = Path(descriptor_path)
        self._cached_descriptor: EngineDescriptor | None = None
        self._cached_mtime: int | None = None

    def _load_descriptor(self) -> EngineDescriptor:
        try:
            descriptor_stat = self._descriptor_path.stat()
        except OSError as exc:
            self._cached_descriptor = None
            self._cached_mtime = None
            raise EngineUnavailable("Usenet engine descriptor is unavailable") from exc
        mtime_ns = descriptor_stat.st_mtime_ns
        if self._cached_descriptor is not None and self._cached_mtime == mtime_ns:
            return self._cached_descriptor
        descriptor = EngineDescriptor.load(self._descriptor_path)
        self._cached_descriptor = descriptor
        self._cached_mtime = mtime_ns
        return descriptor

    async def request(
        self, method: str, path: str, body: bytes = b""
    ) -> tuple[int, dict, bytes]:
        attempt = 0
        while True:
            response = await self._request_once(method, path, body)
            status, _headers, response_body = response
            if status < 400:
                return response
            try:
                code, retryable = _decode_engine_failure(
                    orjson.loads(response_body),
                    label="request",
                )
            except (EngineUnavailable, orjson.JSONDecodeError):
                return response
            if not retryable or code not in _ADMISSION_FAILURE_REASONS:
                return response
            await asyncio.sleep(0.05 * 2 ** min(attempt, 3))
            attempt += 1

    async def _request_once(
        self, method: str, path: str, body: bytes = b""
    ) -> tuple[int, dict, bytes]:
        started_at = time.monotonic_ns()
        if (
            method not in {"GET", "POST", "PUT", "DELETE"}
            or not isinstance(path, str)
            or not path.startswith("/v1/")
            or any(
                not character.isascii() or character.isspace() or ord(character) < 33
                for character in path
            )
            or not isinstance(body, bytes)
            or len(body) > MAX_ENGINE_NZB_METADATA_BYTES
        ):
            raise ValueError("invalid engine request")
        operation = _engine_operation(method, path)
        try:
            descriptor = self._load_descriptor()
            reader, writer = await asyncio.open_unix_connection(
                descriptor.socket_path,
                limit=MAX_ENGINE_HEADER_BYTES,
            )
        except EngineUnavailable as exc:
            _log_engine_request_failure(operation, started_at, exc)
            raise
        except OSError as exc:
            _log_engine_request_failure(operation, started_at, exc)
            raise EngineUnavailable("Usenet engine is unavailable") from exc
        try:
            request_id = current_request_id()
            correlation_header = (
                f"X-Comet-Request-Id: {request_id}\r\n"
                if request_id is not None
                else ""
            )
            header = (
                f"{method} {path} HTTP/1.1\r\n"
                "Host: comet-engine\r\n"
                "Connection: close\r\n"
                "X-Comet-Engine-Version: 1\r\n"
                f"{correlation_header}"
                f"Content-Length: {len(body)}\r\n\r\n"
            ).encode("ascii")
            writer.write(header)
            if body:
                writer.write(body)
            await asyncio.wait_for(
                writer.drain(),
                timeout=MAX_ENGINE_REQUEST_WRITE_SECONDS,
            )
            raw_headers = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"),
                timeout=_response_timeout(path),
            )
            lines = raw_headers.decode("latin-1").split("\r\n")
            status_parts = lines[0].split(" ", 2)
            if (
                len(status_parts) != 3
                or status_parts[0] != "HTTP/1.1"
                or len(status_parts[1]) != 3
                or not status_parts[1].isdigit()
                or not 100 <= int(status_parts[1]) <= 599
            ):
                raise ValueError("engine response has invalid status line")
            status = int(status_parts[1])
            response_headers = {}
            content_length = None
            for line in lines[1:]:
                if not line:
                    continue
                if ":" not in line or line[0].isspace():
                    raise ValueError("engine response has malformed headers")
                name, value = line.split(":", 1)
                name = name.lower()
                value = value.strip()
                if name == "content-length":
                    if content_length is not None:
                        raise ValueError("engine response has ambiguous content length")
                    content_length = value
                elif name == "transfer-encoding":
                    raise ValueError("engine response must not use transfer encoding")
                else:
                    response_headers[name] = value
            if (
                content_length is None
                or not content_length
                or any(character not in "0123456789" for character in content_length)
            ):
                raise ValueError("engine response has invalid content length")
            length = int(content_length)
            response_headers["content-length"] = content_length
            if not 0 <= length <= _maximum_response_bytes(path):
                raise ValueError("engine response exceeds the route limit")
            response_body = await asyncio.wait_for(
                reader.readexactly(length),
                timeout=_response_timeout(path),
            )
            if operation not in _POLLING_OPERATIONS and (
                status >= 400 or operation not in _QUIET_SUCCESS_OPERATIONS
            ):
                emit = log.info if status < 400 else log.warning
                failure_fields = {}
                if status >= 400:
                    try:
                        code, retryable = _decode_engine_failure(
                            orjson.loads(response_body),
                            label="request",
                        )
                    except (EngineUnavailable, orjson.JSONDecodeError):
                        pass
                    else:
                        failure_fields = {
                            "failure_reason": code,
                            "retryable": retryable,
                        }
                if failure_fields.get("failure_reason") not in _QUIET_FAILURE_REASONS:
                    emit(
                        "usenet.engine.completed",
                        "Usenet engine request completed",
                        operation=operation,
                        http_status=status,
                        response_bytes=len(response_body),
                        duration_ms=(time.monotonic_ns() - started_at) / 1_000_000,
                        **failure_fields,
                    )
            return status, response_headers, response_body
        except (
            TimeoutError,
            OSError,
            ValueError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ) as exc:
            _log_engine_request_failure(operation, started_at, exc)
            raise EngineUnavailable("Usenet engine request failed") from exc
        finally:
            writer.close()
            try:
                await asyncio.wait_for(
                    writer.wait_closed(),
                    timeout=MAX_ENGINE_CLOSE_SECONDS,
                )
            except (OSError, TimeoutError):
                pass
