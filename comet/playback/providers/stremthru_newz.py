"""StremThru Newz configuration and non-mutating capability validation."""

import base64
import binascii
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp

from comet.core.models import settings
from comet.core.provider_json import (
    ProviderJsonError,
    decode_provider_data,
    is_success_status,
    read_provider_body,
)
from comet.core.sources import MAX_SIGNED_BIGINT
from comet.playback.base import (
    REMOTE_PREPARATION_TIMEOUT_SECONDS,
    Actionability,
    BytePath,
    ProviderDescriptor,
    ProviderRuntimeError,
    ProviderStatus,
    Readiness,
)
from comet.usenet.easynews import bounded_retry_after
from comet.usenet.file_selection import FileSelectionError, select_remote_video_file
from comet.usenet.limits import MAX_NZB_FILES
from comet.usenet.provider_exports import NzbProviderExportError, export_base_url

_TERMINAL_STATUSES = frozenset({"failed", "invalid"})
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10, connect=3, sock_read=5)
_SUBMIT_TIMEOUT = aiohttp.ClientTimeout(total=REMOTE_PREPARATION_TIMEOUT_SECONDS)
_CONTROL_CONCURRENCY = 4
_CONTROL_LEASE_SECONDS = 12
_CONTROL_OPERATION = "stremthru-control"
_LOCKED_LINK_MAX_CHARACTERS = 4096


class StremThruNewzError(ProviderRuntimeError):
    """Closed provider failure safe for playback state transitions."""


@dataclass(frozen=True, slots=True)
class StremThruNewzOptions:
    base_url: str
    authorization: str


@dataclass(frozen=True, slots=True)
class StremThruNewzSubmission:
    remote_id: str
    remote_hash: str
    status: str


@dataclass(frozen=True, slots=True)
class StremThruNewzFile:
    index: int
    name: str
    size: int
    locked_link: str


@dataclass(frozen=True, slots=True)
class StremThruNewzRemoteItem:
    remote_id: str
    remote_hash: str
    status: str
    files: tuple[StremThruNewzFile, ...]
    terminal: bool


@dataclass(frozen=True, slots=True)
class StremThruGeneratedLink:
    url: str


def _bounded_text(value: object, maximum_characters: int) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum_characters:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _opaque(value: object, field: str) -> str:
    if not _bounded_text(value, 512):
        raise ValueError(f"StremThru {field} is invalid")
    return value


def _locked_link(value: object) -> str:
    if not _bounded_text(value, _LOCKED_LINK_MAX_CHARACTERS):
        raise ValueError("StremThru file link is invalid")
    return value


def _status(value: object) -> str:
    if not _bounded_text(value, 128):
        raise StremThruNewzError("stremthru_invalid_response")
    return value


def _provider_error(
    status: int,
    *,
    retry_after: str | None,
    unavailable: str,
    missing: bool = False,
) -> StremThruNewzError:
    if status in {401, 403}:
        return StremThruNewzError(
            "stremthru_credentials_rejected",
            auth_failed=True,
            mutation_rejected=True,
        )
    if status == 404 and missing:
        return StremThruNewzError(
            "remote_item_missing",
            remote_missing=True,
        )
    if status == 429:
        return StremThruNewzError(
            "stremthru_rate_limited",
            retryable=True,
            retry_after=bounded_retry_after(retry_after),
            mutation_rejected=True,
        )
    if status == 408 or status >= 500:
        return StremThruNewzError(unavailable, retryable=True)
    return StremThruNewzError(
        unavailable,
        mutation_rejected=400 <= status < 500,
    )


def _remote_file(value: object) -> StremThruNewzFile:
    if not isinstance(value, dict):
        raise StremThruNewzError("stremthru_invalid_response")
    index, name, size, link = (
        value.get("index"),
        value.get("name"),
        value.get("size"),
        value.get("link"),
    )
    if (
        isinstance(index, bool)
        or not isinstance(index, int)
        or not -1 <= index <= MAX_SIGNED_BIGINT
        or not _bounded_text(name, 1024)
        or isinstance(size, bool)
        or not isinstance(size, int)
        or not 1 <= size <= MAX_SIGNED_BIGINT
    ):
        raise StremThruNewzError("stremthru_invalid_response")
    try:
        locked_link = _locked_link(link)
    except ValueError as exc:
        raise StremThruNewzError("stremthru_invalid_response") from exc
    return StremThruNewzFile(index, name, size, locked_link)


def _authorization(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("StremThru authorization is invalid")
    try:
        raw = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("StremThru authorization is invalid") from exc
    try:
        decoded = base64.b64decode(value, validate=True)
    except binascii.Error:
        decoded = raw
    if (
        b":" in decoded
        and len(decoded) <= 1024
        and all(byte >= 32 and byte != 127 for byte in decoded)
    ):
        username, password = decoded.split(b":", 1)
        if username and password:
            return base64.b64encode(decoded).decode("ascii")
    try:
        value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("StremThru authorization is invalid") from exc
    return value


def _base_url(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or "\\" in value
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in value
        )
    ):
        raise ValueError("StremThru base URL is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("StremThru base URL is invalid") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port == 0
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("StremThru base URL is invalid")
    normalized = urlunsplit(
        (parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip("/"), "", "")
    )
    host = parsed.hostname.rstrip(".").lower()
    authority = f"[{host}]" if ":" in host else host
    network_origin = (
        f"{parsed.scheme}://{authority}:"
        f"{port if port is not None else (443 if parsed.scheme == 'https' else 80)}"
    )
    if (
        parsed.scheme == "http"
        and network_origin not in settings.USENET_PRIVATE_UPSTREAM_ORIGINS
    ):
        raise ValueError("StremThru requires HTTPS or an allowed private origin")
    return normalized


def _provider_export_url(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or "\\" in value
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in value
        )
    ):
        raise ValueError("StremThru export URL is invalid")
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("StremThru export URL is invalid") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("StremThru export URL is invalid")
    return value


def options(config: dict) -> StremThruNewzOptions:
    if not isinstance(config, dict):
        raise ValueError("StremThru configuration is invalid")
    base_url = _base_url(config.get("baseUrl"))
    return StremThruNewzOptions(
        base_url,
        _authorization(config.get("authToken")),
    )


class StremThruNewzProvider:
    descriptor = ProviderDescriptor(
        kind="stremthru_newz",
        label="StremThru Newz",
        accepted_locator_kinds=frozenset({"nzb_artifact"}),
        byte_paths=frozenset({BytePath.CLOUD_REDIRECT}),
        mutates_upstream=True,
    )

    def __init__(
        self,
        session,
        config: dict,
        *,
        governor=None,
        governor_scope: bytes | None = None,
    ):
        if (governor is None) != (governor_scope is None) or (
            governor_scope is not None
            and (not isinstance(governor_scope, bytes) or len(governor_scope) != 32)
        ):
            raise ValueError("StremThru governor is incomplete")
        self._session = session
        self._options = options(config)
        self._governor = governor
        self._governor_scope = governor_scope

    @asynccontextmanager
    async def _control_permit(self):
        if self._governor is None:
            yield
            return
        lease = await self._governor.acquire_concurrency(
            self._governor_scope,
            _CONTROL_OPERATION,
            limit=_CONTROL_CONCURRENCY,
            owner_request_id=secrets.token_hex(16),
            lease_seconds=_CONTROL_LEASE_SECONDS,
        )
        if lease is None:
            raise StremThruNewzError(
                "stremthru_busy",
                retryable=True,
                retry_after=1,
                mutation_rejected=True,
            )
        try:
            yield
        finally:
            await lease.release()

    async def _control_data(
        self,
        method: str,
        path: str,
        *,
        unavailable: str,
        effective: StremThruNewzOptions | None = None,
        params: dict[str, str] | None = None,
        json_body: dict[str, str] | None = None,
        timeout: aiohttp.ClientTimeout = _REQUEST_TIMEOUT,
    ) -> tuple[int, str | None, dict | None]:
        if self._session is None:
            raise StremThruNewzError(unavailable, retryable=True)
        effective = self._options if effective is None else effective
        headers = self._headers(effective)
        request_kwargs = {
            "headers": (
                {**headers, "Content-Type": "application/json"}
                if json_body is not None
                else headers
            ),
            "allow_redirects": False,
            "timeout": timeout,
        }
        if params is not None:
            request_kwargs["params"] = params
        if json_body is not None:
            request_kwargs["json"] = json_body
        try:
            async with self._control_permit():
                async with getattr(self._session, method)(
                    f"{effective.base_url}{path}",
                    **request_kwargs,
                ) as response:
                    status = response.status
                    retry_after = response.headers.get("Retry-After")
                    body = (
                        await read_provider_body(response)
                        if is_success_status(status)
                        else None
                    )
            data = decode_provider_data(body) if body is not None else None
        except ProviderJsonError as exc:
            raise StremThruNewzError("stremthru_invalid_response") from exc
        except StremThruNewzError:
            raise
        except (aiohttp.ClientError, TimeoutError):
            raise StremThruNewzError(unavailable, retryable=True) from None
        return status, retry_after, data

    def credential_binding(self) -> tuple[str, bytes]:
        """Return the normalized values needed for an HMAC-only account binding."""
        return self._options.base_url, self._options.authorization.encode("ascii")

    @staticmethod
    def _headers(effective: StremThruNewzOptions) -> dict[str, str]:
        return {
            "X-StremThru-Store-Name": "stremthru",
            "X-StremThru-Store-Authorization": f"Bearer {effective.authorization}",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }

    async def validate_config(self, config: dict) -> ProviderStatus:
        try:
            effective = options(config) if config else self._options
        except ValueError:
            return ProviderStatus(
                Readiness.TERMINAL_FAILURE,
                Actionability.NONE,
                code="configuration_required",
            )
        try:
            export_base_url()
        except NzbProviderExportError:
            return ProviderStatus(
                Readiness.TERMINAL_FAILURE,
                Actionability.NONE,
                code="nzb_export_base_url_required",
            )
        try:
            status, _retry_after, data = await self._control_data(
                "get",
                "/v0/store/user",
                unavailable="stremthru_unavailable",
                effective=effective,
            )
        except StremThruNewzError as exc:
            return ProviderStatus(
                (
                    Readiness.RETRYABLE_FAILURE
                    if exc.retryable
                    else Readiness.TERMINAL_FAILURE
                ),
                Actionability.REMOTE_PREPARE if exc.retryable else Actionability.NONE,
                code=(
                    "validation_unavailable" if exc.retryable else "validation_failed"
                ),
            )
        if status in {401, 403}:
            return ProviderStatus(
                Readiness.TERMINAL_FAILURE,
                Actionability.NONE,
                code="credentials_rejected",
                auth_failed=True,
            )
        if status in {404, 501}:
            return ProviderStatus(
                Readiness.TERMINAL_FAILURE,
                Actionability.NONE,
                code="newz_unavailable",
            )
        if not is_success_status(status) or data is None:
            return ProviderStatus(
                Readiness.RETRYABLE_FAILURE,
                Actionability.REMOTE_PREPARE,
                code="validation_unavailable",
            )
        if data.get("has_usenet") is False:
            return ProviderStatus(
                Readiness.TERMINAL_FAILURE,
                Actionability.NONE,
                code="newz_unavailable",
            )
        return ProviderStatus(
            Readiness.REQUIRES_PREPARE, Actionability.REMOTE_PREPARE, None
        )

    async def submit_export(self, export_url: str) -> StremThruNewzSubmission:
        """Submit the stable broker export once and retain only opaque remote state."""
        export_url = _provider_export_url(export_url)
        status, retry_after, data = await self._control_data(
            "post",
            "/v0/store/newz",
            unavailable="stremthru_unavailable",
            json_body={"link": export_url},
            timeout=_SUBMIT_TIMEOUT,
        )
        if not is_success_status(status) or data is None:
            raise _provider_error(
                status,
                retry_after=retry_after,
                unavailable="stremthru_submission_failed",
            )
        try:
            remote_id = _opaque(data.get("id"), "remote id")
            remote_hash = _opaque(data.get("hash"), "remote hash")
        except ValueError as exc:
            raise StremThruNewzError("stremthru_invalid_response") from exc
        status = _status(data.get("status"))
        return StremThruNewzSubmission(remote_id, remote_hash, status)

    async def get_item(
        self, remote_id: str, remote_hash: str
    ) -> StremThruNewzRemoteItem:
        """Read only the item sealed by this configuration's remote-work ledger."""
        try:
            remote_id = _opaque(remote_id, "remote id")
            remote_hash = _opaque(remote_hash, "remote hash")
        except ValueError as exc:
            raise StremThruNewzError("stremthru_invalid_response") from exc
        status, retry_after, data = await self._control_data(
            "get",
            f"/v0/store/newz/{quote(remote_id, safe='')}",
            unavailable="stremthru_unavailable",
        )
        if not is_success_status(status) or data is None:
            raise _provider_error(
                status,
                retry_after=retry_after,
                unavailable="stremthru_unavailable",
                missing=True,
            )
        try:
            if (
                _opaque(data.get("id"), "remote id") != remote_id
                or _opaque(data.get("hash"), "remote hash") != remote_hash
            ):
                raise ValueError
        except ValueError as exc:
            raise StremThruNewzError("stremthru_invalid_response") from exc
        status, files = _status(data.get("status")), data.get("files")
        if not isinstance(files, list) or len(files) > MAX_NZB_FILES:
            raise StremThruNewzError("stremthru_invalid_response")
        decoded_files = tuple(_remote_file(item) for item in files)
        parsed_files = tuple(
            StremThruNewzFile(
                position if item.index == -1 else item.index,
                item.name,
                item.size,
                item.locked_link,
            )
            for position, item in enumerate(decoded_files)
        )
        return StremThruNewzRemoteItem(
            remote_id,
            remote_hash,
            status,
            parsed_files,
            not parsed_files and status.casefold() in _TERMINAL_STATUSES,
        )

    @staticmethod
    def select_file(
        item: StremThruNewzRemoteItem, selection: tuple[object, ...]
    ) -> StremThruNewzFile:
        """Select the remote file through Comet's canonical Usenet selector."""
        try:
            return select_remote_video_file(
                ((file, file.name, file.size) for file in item.files),
                selection,
            )
        except FileSelectionError:
            raise StremThruNewzError(
                "stremthru_file_selection_ambiguous",
                terminal=True,
            ) from None

    async def generate_link(self, locked_link: str) -> StremThruGeneratedLink:
        try:
            locked_link = _locked_link(locked_link)
        except ValueError as exc:
            raise StremThruNewzError("stremthru_invalid_response") from exc
        status, retry_after, data = await self._control_data(
            "post",
            "/v0/store/newz/link/generate",
            unavailable="stremthru_link_unavailable",
            json_body={"link": locked_link},
        )
        if not is_success_status(status) or data is None:
            raise _provider_error(
                status,
                retry_after=retry_after,
                unavailable="stremthru_link_unavailable",
            )
        link = data.get("link")
        if not isinstance(link, str) or not link or len(link) > 8192:
            raise StremThruNewzError("stremthru_invalid_response")
        try:
            parsed = urlsplit(link)
            _ = parsed.port
        except ValueError as exc:
            raise StremThruNewzError("stremthru_invalid_response") from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise StremThruNewzError("stremthru_invalid_response")
        return StremThruGeneratedLink(link)
