"""TorBox cloud-Usenet provider using brokered NZB documents only."""

from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from ipaddress import IPv4Address
from urllib.parse import urlsplit

import aiohttp

from comet.core.provider_json import (
    ProviderJsonError,
    is_success_status,
    read_provider_json,
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
from comet.usenet.file_selection import FileSelectionError, select_remote_video_file
from comet.usenet.limits import MAX_NZB_FILES, MAX_NZB_METADATA_BYTES

_BASE_URL = "https://api.torbox.app/v1/api/usenet"
_USER_URL = "https://api.torbox.app/v1/api/user/me"
_VALIDATION_TIMEOUT = aiohttp.ClientTimeout(total=10, connect=3, sock_read=5)
_READ_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=5, sock_read=10)
_CREATE_TIMEOUT = aiohttp.ClientTimeout(total=REMOTE_PREPARATION_TIMEOUT_SECONDS)
_LIBRARY_PAGE_SIZE = 1_000
_MAX_LIBRARY_ITEMS = MAX_NZB_FILES
_REMOTE_TERMINAL = frozenset(
    {"failed", "failed (processing)", "invalid", "expired", "(reported) missing"}
)
_TERMINAL = frozenset({"failed", "invalid"})
_LOWER_HEX = frozenset("0123456789abcdef")


class TorBoxUsenetError(ProviderRuntimeError):
    """A safe TorBox Usenet provider failure."""


@dataclass(frozen=True, slots=True)
class TorBoxUsenetItem:
    usenet_id: int
    status: str
    files: tuple["TorBoxUsenetFile", ...] = ()
    download_finished: bool | None = None
    download_present: bool | None = None
    post_processing: bool | None = None
    active: bool | None = None
    content_hash: str | None = None
    alternative_hashes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TorBoxUsenetFile:
    file_id: int
    name: str
    size: int


@dataclass(frozen=True, slots=True)
class TorBoxDownloadTarget:
    url: str


def _api_key(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("TorBox API key is invalid")
    return value


async def _validation_data(response) -> object:
    payload = await read_provider_json(response)
    if payload.get("success") is False or "data" not in payload:
        raise ProviderJsonError("invalid TorBox envelope")
    return payload["data"]


async def _provider_data(response) -> object:
    try:
        return await _validation_data(response)
    except ProviderJsonError as exc:
        raise TorBoxUsenetError("torbox_invalid_response") from exc


async def _validation_error_code(response) -> str | None:
    try:
        payload = await read_provider_json(response)
    except ProviderJsonError as exc:
        raise TorBoxUsenetError("torbox_invalid_response") from exc
    error = payload.get("error")
    return error if isinstance(error, str) else None


def _canonical_status(value: str) -> str:
    normalized = value.casefold()
    if normalized in _REMOTE_TERMINAL:
        return "invalid" if normalized == "invalid" else "failed"
    return value


def normalize_torbox_hash(value: object) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 512:
        return None
    normalized = value.lower()
    return normalized if len(value) == 32 and set(normalized) <= _LOWER_HEX else value


def _item(value: object) -> TorBoxUsenetItem:
    if not isinstance(value, dict):
        raise TorBoxUsenetError("torbox_invalid_response")
    identifier = value.get("id")
    status = value.get("download_state")
    if (
        isinstance(identifier, bool)
        or not isinstance(identifier, int)
        or not 0 <= identifier <= MAX_SIGNED_BIGINT
        or not isinstance(status, str)
        or not status
    ):
        raise TorBoxUsenetError("torbox_invalid_response")
    files_value = value.get("files")
    if files_value is None:
        files = ()
    elif not isinstance(files_value, list) or len(files_value) > MAX_NZB_FILES:
        raise TorBoxUsenetError("torbox_invalid_response")
    else:
        files = files_value
    flags = []
    for field in (
        "download_finished",
        "download_present",
        "post_processing",
        "active",
    ):
        flag = value.get(field)
        if flag is not None and not isinstance(flag, bool):
            raise TorBoxUsenetError("torbox_invalid_response")
        flags.append(flag)
    content_hash_value = value.get("hash")
    content_hash = (
        None
        if content_hash_value is None
        else normalize_torbox_hash(content_hash_value)
    )
    if content_hash_value is not None and content_hash is None:
        raise TorBoxUsenetError("torbox_invalid_response")
    alternative_hash_values = value.get("alternative_hashes", [])
    if (
        not isinstance(alternative_hash_values, list)
        or len(alternative_hash_values) > MAX_NZB_FILES
    ):
        raise TorBoxUsenetError("torbox_invalid_response")
    alternative_hashes = []
    seen_hashes = {content_hash} if content_hash is not None else set()
    for candidate in alternative_hash_values:
        parsed_hash = normalize_torbox_hash(candidate)
        if parsed_hash is None:
            raise TorBoxUsenetError("torbox_invalid_response")
        if parsed_hash not in seen_hashes:
            seen_hashes.add(parsed_hash)
            alternative_hashes.append(parsed_hash)
    parsed_files = {}
    for file in files:
        parsed = _file(file)
        if parsed.file_id in parsed_files:
            raise TorBoxUsenetError("torbox_invalid_response")
        parsed_files[parsed.file_id] = parsed
    return TorBoxUsenetItem(
        identifier,
        _canonical_status(status),
        tuple(parsed_files.values()),
        flags[0],
        flags[1],
        flags[2],
        flags[3],
        content_hash,
        tuple(alternative_hashes),
    )


def _created_item(value: object) -> TorBoxUsenetItem:
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= MAX_SIGNED_BIGINT
    ):
        return TorBoxUsenetItem(value, "queued")
    if isinstance(value, dict):
        identifier = value.get("usenetdownload_id")
        if (
            isinstance(identifier, int)
            and not isinstance(identifier, bool)
            and 0 <= identifier <= MAX_SIGNED_BIGINT
        ):
            content_hash = value.get("hash")
            normalized_hash = normalize_torbox_hash(content_hash)
            if content_hash is not None and normalized_hash is None:
                raise TorBoxUsenetError("torbox_invalid_response")
            return TorBoxUsenetItem(
                identifier,
                "queued",
                content_hash=normalized_hash,
            )
    return _item(value)


def _file(value: object) -> TorBoxUsenetFile:
    if not isinstance(value, dict):
        raise TorBoxUsenetError("torbox_invalid_response")
    file_id = value.get("id")
    name = value.get("name")
    size = value.get("size")
    if (
        isinstance(file_id, bool)
        or not isinstance(file_id, int)
        or not 0 <= file_id <= MAX_SIGNED_BIGINT
        or not isinstance(name, str)
        or not name
        or isinstance(size, bool)
        or not isinstance(size, int)
        or not 1 <= size <= MAX_SIGNED_BIGINT
    ):
        raise TorBoxUsenetError("torbox_invalid_response")
    return TorBoxUsenetFile(file_id, name, size)


def cache_hashes_from_manifest(manifest: object) -> tuple[str, ...]:
    """Read TorBox's parser-produced first-message hashes without parsing XML."""
    if not isinstance(manifest, list) or not 1 <= len(manifest) <= MAX_NZB_FILES:
        raise ValueError("TorBox NZB manifest is invalid")
    hashes = []
    seen = set()
    for file in manifest:
        if not isinstance(file, dict):
            raise ValueError("TorBox NZB manifest is invalid")
        content_hash = file.get("first_segment_md5")
        if content_hash is None:
            return ()
        if (
            not isinstance(content_hash, str)
            or len(content_hash) != 32
            or any(character not in _LOWER_HEX for character in content_hash)
        ):
            raise ValueError("TorBox NZB manifest is invalid")
        if content_hash not in seen:
            seen.add(content_hash)
            hashes.append(content_hash)
    return tuple(hashes)


def cache_hash_from_alias(value: object) -> tuple[str, ...]:
    """Return a cache-check key only for a canonical TorBox MD5 alias."""
    if (
        not isinstance(value, str)
        or len(value) != 32
        or any(character not in _LOWER_HEX for character in value)
    ):
        return ()
    return (value,)


def _requested_cache_hashes(hashes: object) -> tuple[str, ...]:
    if not isinstance(hashes, tuple) or any(
        not isinstance(value, str)
        or len(value) != 32
        or any(character not in _LOWER_HEX for character in value)
        for value in hashes
    ):
        raise ValueError("TorBox cache hashes are invalid")
    requested = tuple(dict.fromkeys(hashes))
    if not 1 <= len(requested) <= MAX_NZB_FILES:
        raise ValueError("TorBox cache hashes are invalid")
    return requested


def _download_target(value: object) -> TorBoxDownloadTarget:
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise TorBoxUsenetError("torbox_invalid_response")
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise TorBoxUsenetError("torbox_invalid_response") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise TorBoxUsenetError("torbox_invalid_response")
    return TorBoxDownloadTarget(value)


class TorBoxUsenetProvider:
    descriptor = ProviderDescriptor(
        kind="torbox_usenet",
        label="TorBox Usenet",
        accepted_locator_kinds=frozenset({"nzb_artifact"}),
        byte_paths=frozenset({BytePath.CLOUD_REDIRECT}),
        mutates_upstream=True,
    )

    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        client_ip: str = "",
        *,
        governor=None,
        governor_scope: bytes | None = None,
    ):
        if (governor is None) != (governor_scope is None) or (
            governor_scope is not None
            and (not isinstance(governor_scope, bytes) or len(governor_scope) != 32)
        ):
            raise ValueError("TorBox governor is incomplete")
        self._session = session
        self._api_key = _api_key(api_key)
        self._client_ip = client_ip
        self._governor = governor
        self._governor_scope = governor_scope

    def _headers(self, api_key: str | None = None) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {api_key or self._api_key}",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }

    def credential_binding(self) -> tuple[str, bytes]:
        """Expose account material only for transient partition derivation."""
        return _BASE_URL, self._api_key.encode()

    def _public_ipv4(self) -> str | None:
        try:
            address = IPv4Address(self._client_ip)
        except ValueError:
            return None
        return str(address) if address.is_global else None

    @asynccontextmanager
    async def _request(self, operation: str, request, *args, **kwargs):
        permit = None
        if self._governor is not None:
            permit = await self._governor.acquire_window(
                self._governor_scope,
                f"torbox_api:{operation}",
                limit=300,
                window_seconds=60,
            )
            if permit is None:
                raise TorBoxUsenetError("torbox_rate_limited", retryable=True)
        async with request(*args, **kwargs) as response:
            try:
                yield response
            finally:
                if response.status == 429 and permit is not None:
                    await self._governor.tighten_window(
                        self._governor_scope,
                        f"torbox_api:{operation}",
                        limit=permit.used,
                        window_seconds=60,
                    )

    async def validate_config(self, config: dict) -> ProviderStatus:
        try:
            api_key = _api_key(config.get("apiKey", self._api_key))
        except ValueError:
            return ProviderStatus(
                Readiness.TERMINAL_FAILURE,
                Actionability.NONE,
                code="api_key_required",
                auth_failed=True,
            )
        if self._session is None:
            return ProviderStatus(
                Readiness.RETRYABLE_FAILURE,
                Actionability.REMOTE_PREPARE,
                code="validation_unavailable",
            )
        try:
            async with self._request(
                "user_me",
                self._session.get,
                _USER_URL,
                headers=self._headers(api_key),
                allow_redirects=False,
                timeout=_VALIDATION_TIMEOUT,
            ) as response:
                if response.status in {401, 403}:
                    return ProviderStatus(
                        Readiness.TERMINAL_FAILURE,
                        Actionability.NONE,
                        code="api_key_rejected",
                        auth_failed=True,
                    )
                if response.status == 429 or response.status >= 500:
                    return ProviderStatus(
                        Readiness.RETRYABLE_FAILURE,
                        Actionability.REMOTE_PREPARE,
                        code="validation_unavailable",
                    )
                if not is_success_status(response.status):
                    return ProviderStatus(
                        Readiness.TERMINAL_FAILURE,
                        Actionability.NONE,
                        code="validation_failed",
                    )
                await _validation_data(response)
            async with self._request(
                "usenet_mylist",
                self._session.get,
                f"{_BASE_URL}/mylist",
                params={"offset": "0", "limit": "1", "bypass_cache": "false"},
                headers=self._headers(api_key),
                allow_redirects=False,
                timeout=_VALIDATION_TIMEOUT,
            ) as response:
                if response.status in {401, 403}:
                    return ProviderStatus(
                        Readiness.TERMINAL_FAILURE,
                        Actionability.NONE,
                        code="api_key_rejected",
                        auth_failed=True,
                    )
                if response.status == 429 or response.status >= 500:
                    return ProviderStatus(
                        Readiness.RETRYABLE_FAILURE,
                        Actionability.REMOTE_PREPARE,
                        code="validation_unavailable",
                    )
                if not is_success_status(response.status):
                    error_code = await _validation_error_code(response)
                    return ProviderStatus(
                        Readiness.TERMINAL_FAILURE,
                        Actionability.NONE,
                        code=(
                            "usenet_plan_required"
                            if error_code == "PLAN_RESTRICTED_FEATURE"
                            else "usenet_unavailable"
                        ),
                    )
                await _validation_data(response)
        except (TimeoutError, aiohttp.ClientError):
            return ProviderStatus(
                Readiness.RETRYABLE_FAILURE,
                Actionability.REMOTE_PREPARE,
                code="validation_unavailable",
            )
        except TorBoxUsenetError as exc:
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
        except ProviderJsonError:
            return ProviderStatus(
                Readiness.TERMINAL_FAILURE,
                Actionability.NONE,
                code="validation_failed",
            )
        return ProviderStatus(Readiness.UNKNOWN, Actionability.REMOTE_PREPARE)

    async def find_existing(
        self,
        hashes: tuple[str, ...],
    ) -> TorBoxUsenetItem | None:
        """Find an account-library item by an exact content identity."""
        requested = frozenset(_requested_cache_hashes(hashes))
        seen_ids = set()
        for offset in range(0, _MAX_LIBRARY_ITEMS, _LIBRARY_PAGE_SIZE):
            try:
                async with self._request(
                    "usenet_mylist",
                    self._session.get,
                    f"{_BASE_URL}/mylist",
                    params={
                        "offset": str(offset),
                        "limit": str(_LIBRARY_PAGE_SIZE),
                        "bypass_cache": "true",
                    },
                    headers=self._headers(),
                    allow_redirects=False,
                    timeout=_READ_TIMEOUT,
                ) as response:
                    if response.status in {401, 403}:
                        raise TorBoxUsenetError(
                            "torbox_auth_failed",
                            auth_failed=True,
                        )
                    if response.status == 429 or response.status >= 500:
                        raise TorBoxUsenetError(
                            "torbox_unavailable",
                            retryable=True,
                        )
                    if not is_success_status(response.status):
                        raise TorBoxUsenetError(
                            "torbox_library_unavailable",
                            retryable=True,
                        )
                    data = await _provider_data(response)
            except (TimeoutError, aiohttp.ClientError) as exc:
                raise TorBoxUsenetError(
                    "torbox_unavailable",
                    retryable=True,
                ) from exc
            if not isinstance(data, list):
                raise TorBoxUsenetError("torbox_invalid_response")
            if len(data) > _LIBRARY_PAGE_SIZE:
                raise TorBoxUsenetError("torbox_invalid_response")
            hash_matches = []
            for value in data:
                parsed = _item(value)
                if parsed.usenet_id in seen_ids:
                    continue
                seen_ids.add(parsed.usenet_id)
                hash_match = (
                    parsed.content_hash in requested
                    or not requested.isdisjoint(parsed.alternative_hashes)
                )
                status = self.status(parsed)
                if status.readiness is Readiness.TERMINAL_FAILURE:
                    continue
                if hash_match:
                    hash_matches.append(parsed)
            if hash_matches:
                return max(
                    hash_matches,
                    key=lambda item: (
                        self.status(item).readiness is Readiness.READY,
                        item.usenet_id,
                    ),
                )
            if len(data) < _LIBRARY_PAGE_SIZE:
                return None
        raise TorBoxUsenetError(
            "torbox_library_unavailable",
            retryable=True,
        )

    @staticmethod
    def status(item: TorBoxUsenetItem) -> ProviderStatus:
        if (
            item.download_finished is True
            and item.download_present is True
            and (
                item.post_processing is False
                or (item.post_processing is None and item.active is False)
            )
            and bool(item.files)
        ):
            return ProviderStatus(Readiness.READY, Actionability.SERVER_ON_DEMAND)
        if item.status in _TERMINAL:
            return ProviderStatus(
                Readiness.TERMINAL_FAILURE,
                Actionability.NONE,
                code="remote_failed",
            )
        if item.active is True:
            return ProviderStatus(Readiness.PREPARING, Actionability.REMOTE_PREPARE)
        return ProviderStatus(Readiness.UNKNOWN, Actionability.REMOTE_PREPARE)

    async def submit_artifact(
        self,
        document: bytes,
        *,
        name: str | None = None,
        governor=None,
        governor_scope: bytes | None = None,
    ) -> TorBoxUsenetItem:
        if (
            not isinstance(document, bytes)
            or not document
            or len(document) > MAX_NZB_METADATA_BYTES
        ):
            raise ValueError("NZB document is required")
        form = aiohttp.FormData()
        form.add_field(
            "file", document, filename="comet.nzb", content_type="application/x-nzb"
        )
        form.add_field("post_processing", "-1")
        form.add_field("as_queued", "false")
        form.add_field("add_only_if_cached", "false")
        if name is not None:
            form.add_field("name", name)
        return await self._submit_create(
            form,
            governor=governor,
            governor_scope=governor_scope,
        )

    async def _submit_create(
        self,
        form: aiohttp.FormData,
        *,
        governor,
        governor_scope: bytes | None,
    ) -> TorBoxUsenetItem:
        if (governor is None) != (governor_scope is None):
            raise ValueError("TorBox create governor is incomplete")
        permit = None
        if governor is not None:
            permit = await governor.acquire_window(
                governor_scope,
                "torbox_usenet_create",
                limit=60,
                window_seconds=60 * 60,
            )
            if permit is None:
                raise TorBoxUsenetError(
                    "torbox_create_rate_limited",
                    retryable=True,
                )
        try:
            async with self._request(
                "usenet_create",
                self._session.post,
                f"{_BASE_URL}/createusenetdownload",
                headers=self._headers(),
                data=form,
                allow_redirects=False,
                timeout=_CREATE_TIMEOUT,
            ) as response:
                upstream_limit = _create_rate_limit(response.headers)
                if permit is not None and upstream_limit is not None:
                    await governor.tighten_window(
                        governor_scope,
                        "torbox_usenet_create",
                        limit=upstream_limit,
                        window_seconds=60 * 60,
                    )
                if response.status == 429 and permit is not None:
                    await governor.tighten_window(
                        governor_scope,
                        "torbox_usenet_create",
                        limit=permit.used,
                        window_seconds=60 * 60,
                    )
                if response.status in {401, 403}:
                    raise TorBoxUsenetError(
                        "torbox_auth_failed",
                        auth_failed=True,
                    )
                if response.status == 429:
                    raise TorBoxUsenetError(
                        "torbox_create_rate_limited",
                        retryable=True,
                    )
                if response.status >= 500:
                    raise TorBoxUsenetError(
                        "torbox_unavailable",
                        retryable=True,
                    )
                if not is_success_status(response.status):
                    raise TorBoxUsenetError("torbox_submission_failed")
                data = await _provider_data(response)
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise TorBoxUsenetError(
                "torbox_unavailable",
                retryable=True,
            ) from exc
        parsed = _created_item(data)
        return parsed

    async def delete_owned(self, usenet_id: int) -> None:
        """Delete one ledger-authorized item; callers must prove ownership."""
        if (
            isinstance(usenet_id, bool)
            or not isinstance(usenet_id, int)
            or not 0 <= usenet_id <= MAX_SIGNED_BIGINT
        ):
            raise ValueError("TorBox Usenet id is invalid")
        try:
            async with self._request(
                "usenet_control",
                self._session.post,
                f"{_BASE_URL}/controlusenetdownload",
                json={
                    "usenet_id": usenet_id,
                    "operation": "delete",
                    "all": False,
                },
                headers=self._headers(),
                allow_redirects=False,
                timeout=_READ_TIMEOUT,
            ) as response:
                if response.status in {401, 403}:
                    raise TorBoxUsenetError(
                        "torbox_auth_failed",
                        auth_failed=True,
                    )
                if response.status == 429:
                    raise TorBoxUsenetError(
                        "torbox_rate_limited",
                        retryable=True,
                    )
                if response.status >= 500:
                    raise TorBoxUsenetError(
                        "torbox_unavailable",
                        retryable=True,
                    )
                if not is_success_status(response.status):
                    error_code = await _validation_error_code(response)
                    if error_code == "DOWNLOAD_NOT_FOUND":
                        return
                    raise TorBoxUsenetError("torbox_control_failed")
                if response.status != 204:
                    await _provider_data(response)
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise TorBoxUsenetError(
                "torbox_unavailable",
                retryable=True,
            ) from exc

    async def get_item(self, usenet_id: int) -> TorBoxUsenetItem:
        """Read one exact TorBox item; global-library adoption is never permitted."""
        if (
            isinstance(usenet_id, bool)
            or not isinstance(usenet_id, int)
            or not 0 <= usenet_id <= MAX_SIGNED_BIGINT
        ):
            raise ValueError("TorBox Usenet id is invalid")
        try:
            async with self._request(
                "usenet_mylist",
                self._session.get,
                f"{_BASE_URL}/mylist",
                params={
                    "offset": "0",
                    "limit": "1",
                    "bypass_cache": "true",
                    "id": str(usenet_id),
                },
                headers=self._headers(),
                allow_redirects=False,
                timeout=_READ_TIMEOUT,
            ) as response:
                if response.status in {401, 403}:
                    raise TorBoxUsenetError(
                        "torbox_auth_failed",
                        auth_failed=True,
                    )
                if response.status == 429 or response.status >= 500:
                    raise TorBoxUsenetError(
                        "torbox_unavailable",
                        retryable=True,
                    )
                if not is_success_status(response.status):
                    raise TorBoxUsenetError("torbox_item_unavailable")
                data = await _provider_data(response)
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise TorBoxUsenetError(
                "torbox_unavailable",
                retryable=True,
            ) from exc
        values = data if isinstance(data, list) else (data,)
        for value in values:
            item = _item(value)
            if item.usenet_id == usenet_id:
                return item
        raise TorBoxUsenetError("torbox_invalid_response")

    async def request_download(
        self,
        usenet_id: int,
        *,
        file_id: int,
    ) -> TorBoxDownloadTarget:
        if (
            isinstance(usenet_id, bool)
            or not isinstance(usenet_id, int)
            or not 0 <= usenet_id <= MAX_SIGNED_BIGINT
            or isinstance(file_id, bool)
            or not isinstance(file_id, int)
            or not 0 <= file_id <= MAX_SIGNED_BIGINT
        ):
            raise ValueError("TorBox file id is invalid")
        params = {
            "token": self._api_key,
            "usenet_id": usenet_id,
            "file_id": file_id,
            "zip_link": "false",
            "redirect": "false",
        }
        if (client_ip := self._public_ipv4()) is not None:
            params["user_ip"] = client_ip
        try:
            async with self._request(
                "usenet_requestdl",
                self._session.get,
                f"{_BASE_URL}/requestdl",
                params=params,
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                },
                allow_redirects=False,
                timeout=_READ_TIMEOUT,
            ) as response:
                if response.status in {401, 403}:
                    raise TorBoxUsenetError(
                        "torbox_auth_failed",
                        auth_failed=True,
                    )
                if response.status == 429 or response.status >= 500:
                    raise TorBoxUsenetError(
                        "torbox_unavailable",
                        retryable=True,
                    )
                if not is_success_status(response.status):
                    raise TorBoxUsenetError("torbox_link_failed")
                data = await _provider_data(response)
        except (TimeoutError, aiohttp.ClientError):
            # requestdl requires the account key in its query. Never retain an
            # aiohttp exception that may render that URL in a traceback.
            raise TorBoxUsenetError(
                "torbox_unavailable",
                retryable=True,
            ) from None
        return _download_target(data)

    @staticmethod
    def select_file(
        item: TorBoxUsenetItem, selection: tuple[object, ...]
    ) -> TorBoxUsenetFile:
        """Select the remote file through Comet's canonical Usenet selector."""
        if TorBoxUsenetProvider.status(item).readiness is not Readiness.READY:
            raise TorBoxUsenetError("torbox_file_unavailable")
        try:
            return select_remote_video_file(
                ((file, file.name, file.size) for file in item.files),
                selection,
            )
        except FileSelectionError:
            raise TorBoxUsenetError(
                "torbox_file_selection_ambiguous",
                terminal=True,
            ) from None


def _create_rate_limit(headers: Mapping[str, str]) -> int | None:
    for name in ("RateLimit-Limit", "X-RateLimit-Limit"):
        value = headers.get(name)
        if (
            isinstance(value, str)
            and value.isdigit()
            and len(value) <= 10
            and 1 <= int(value)
        ):
            return min(int(value), 60)
    return None
