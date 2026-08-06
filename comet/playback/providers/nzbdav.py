"""NzbDAV validation and brokered-NZB upload boundary."""

import asyncio
import re
import uuid
import xml.etree.ElementTree as element_tree
from collections import deque
from dataclasses import dataclass
from urllib.parse import quote, unquote, urljoin, urlsplit

import aiohttp

from comet.core.models import settings
from comet.core.provider_json import (
    ProviderJsonError,
    is_success_status,
    read_provider_json,
)
from comet.core.sources import MAX_SIGNED_BIGINT
from comet.playback.base import (
    Actionability,
    BytePath,
    ProviderDescriptor,
    ProviderRuntimeError,
    ProviderStatus,
    Readiness,
)
from comet.usenet.file_selection import FileSelectionError, select_remote_video_file
from comet.usenet.limits import MAX_NZB_METADATA_BYTES
from comet.usenet.outbound import configured_http_origin, http_url_with_basic_auth
from comet.usenet.upstream import UpstreamUrlError, normalize_upstream_base_url
from comet.utils.http_client import read_bounded_body

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=5, sock_read=10)
_MAX_DAV_RESPONSE_BYTES = 1024 * 1024
_MAX_DAV_TRAVERSAL_BYTES = 4 * 1024 * 1024
_MAX_DAV_ENTRIES = 2048
_MAX_DAV_DEPTH = 16
_SAB_PAGE_SIZE = 200
_MAX_SAB_RECONCILIATION_ITEMS = 2_000
_SAB_RECONCILIATION_TIMEOUT_SECONDS = 20


class NzbDavError(ProviderRuntimeError):
    """NzbDAV failure with provider-bound transition semantics."""


@dataclass(frozen=True, slots=True)
class NzbDavJob:
    job_id: str
    status: ProviderStatus
    verified_name: str | None = None
    observed: bool = False


@dataclass(frozen=True, slots=True)
class NzbDavWebDavEntry:
    relative_path: str
    byte_size: int | None
    is_collection: bool


@dataclass(frozen=True, slots=True)
class NzbDavReconciliation:
    job_id: str
    status: str


def _bounded_text(value: object, maximum_bytes: int) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return len(encoded) <= maximum_bytes and not any(
        ord(character) < 32 or ord(character) == 127 for character in value
    )


def _base_url(value: object, *, server_reachable: bool) -> str | None:
    try:
        return normalize_upstream_base_url(
            value,
            allowed_http_origins=(
                settings.USENET_PRIVATE_UPSTREAM_ORIGINS if server_reachable else None
            ),
        )
    except UpstreamUrlError:
        return None


def _local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1]


def parse_webdav_entries(
    root_url: str, document: bytes
) -> tuple[NzbDavWebDavEntry, ...]:
    """Parse one bounded DAV multistatus document below a fixed completed job."""
    if not isinstance(document, bytes) or not document or len(document) > 1024 * 1024:
        raise ValueError("invalid NzbDAV WebDAV response")
    if b"<!ENTITY" in document.upper():
        raise ValueError("invalid NzbDAV WebDAV response")
    root_parts = urlsplit(root_url)
    try:
        configured_http_origin(root_url)
    except ValueError as exc:
        raise ValueError("invalid NzbDAV WebDAV root") from exc
    if (
        not root_parts.path.rstrip("/")
        or root_parts.query
        or root_parts.fragment
        or "\\" in root_url
        or any(character.isspace() for character in root_url)
    ):
        raise ValueError("invalid NzbDAV WebDAV root")
    root_path = root_parts.path.rstrip("/")
    try:
        document_root = element_tree.fromstring(document)
    except element_tree.ParseError as exc:
        raise ValueError("invalid NzbDAV WebDAV response") from exc
    if _local_name(document_root.tag) != "multistatus":
        raise ValueError("invalid NzbDAV WebDAV response")
    entries = {}
    for response in document_root:
        if _local_name(response.tag) != "response":
            continue
        href_element = next(
            (child for child in response if _local_name(child.tag) == "href"), None
        )
        href = (
            href_element.text.strip()
            if href_element is not None and href_element.text
            else ""
        )
        resolved = urlsplit(urljoin(f"{root_url.rstrip('/')}/", href))
        try:
            # NzbDAV emits its backend origin here when its bundled reverse proxy
            # forwards WebDAV requests. Only the confined path is consumed below;
            # playback URLs are rebuilt from the configured stream base.
            configured_http_origin(resolved.geturl())
        except ValueError as exc:
            raise ValueError(
                "NzbDAV WebDAV response escapes the completed job"
            ) from exc
        if (
            not href
            or resolved.query
            or resolved.fragment
            or (
                resolved.path != root_path
                and not resolved.path.startswith(f"{root_path}/")
            )
        ):
            raise ValueError("NzbDAV WebDAV response escapes the completed job")
        encoded_relative = resolved.path.removeprefix(root_path).lstrip("/").rstrip("/")
        if re.search(r"%(?![0-9A-Fa-f]{2})", encoded_relative):
            raise ValueError("invalid NzbDAV WebDAV path")
        try:
            relative = unquote(
                encoded_relative,
                errors="strict",
            )
        except UnicodeDecodeError as exc:
            raise ValueError("invalid NzbDAV WebDAV path") from exc
        if not relative:
            continue
        parts = relative.split("/")
        if (
            any(
                not part
                or part in {".", ".."}
                or any(
                    ord(character) < 32 or ord(character) == 127 for character in part
                )
                for part in parts
            )
            or "\\" in relative
            or len(relative.encode("utf-8")) > 2048
        ):
            raise ValueError("invalid NzbDAV WebDAV path")
        successful_properties = []
        for propstat in (
            child for child in response if _local_name(child.tag) == "propstat"
        ):
            status_element = next(
                (child for child in propstat if _local_name(child.tag) == "status"),
                None,
            )
            status_parts = (
                status_element.text.strip().split()
                if status_element is not None and status_element.text
                else ()
            )
            if status_parts and (
                len(status_parts) < 2
                or not status_parts[1].isascii()
                or not status_parts[1].isdigit()
                or not is_success_status(int(status_parts[1]))
            ):
                continue
            properties = [
                child for child in propstat if _local_name(child.tag) == "prop"
            ]
            if len(properties) != 1:
                raise ValueError("invalid NzbDAV WebDAV response")
            successful_properties.append(properties[0])
        if not successful_properties:
            raise ValueError("invalid NzbDAV WebDAV response")
        collection = any(
            _local_name(child.tag) == "collection"
            for properties in successful_properties
            for child in properties.iter()
        )
        length_element = next(
            (
                child
                for properties in successful_properties
                for child in properties
                if _local_name(child.tag) == "getcontentlength"
            ),
            None,
        )
        byte_size = None
        if not collection:
            if length_element is None or length_element.text is None:
                raise ValueError("NzbDAV file has no size")
            raw_length = length_element.text.strip()
            if not raw_length or not raw_length.isascii() or not raw_length.isdigit():
                raise ValueError("NzbDAV file has an invalid size")
            try:
                byte_size = int(raw_length)
            except ValueError as exc:
                raise ValueError("NzbDAV file has an invalid size") from exc
            if not 1 <= byte_size <= MAX_SIGNED_BIGINT:
                raise ValueError("NzbDAV file has an invalid size")
        entries[relative] = NzbDavWebDavEntry(relative, byte_size, collection)
        if len(entries) > _MAX_DAV_ENTRIES:
            raise ValueError("NzbDAV WebDAV response is too large")
    return tuple(entries.values())


class NzbDavProvider:
    descriptor = ProviderDescriptor(
        kind="nzbdav",
        label="NzbDAV",
        accepted_locator_kinds=frozenset({"nzb_artifact"}),
        byte_paths=frozenset({BytePath.CLOUD_REDIRECT}),
        mutates_upstream=True,
    )

    def __init__(self, session):
        self._session = session

    @staticmethod
    def _category(value: object, default: str) -> str:
        category = default if value is None else value
        if not isinstance(category, str) or not category:
            raise ValueError("NzbDAV category is invalid")
        try:
            category.encode()
        except UnicodeEncodeError as exc:
            raise ValueError("NzbDAV category is invalid") from exc
        return category

    @staticmethod
    def _credentials(config: dict) -> tuple[str, str, str] | None:
        if not isinstance(config, dict):
            return None
        values = [
            config.get(key) for key in ("sabApiKey", "webdavUsername", "webdavPassword")
        ]
        if not all(_bounded_text(value, 1024) for value in values):
            return None
        if not values[0].isascii() or ":" in values[1]:
            return None
        return values[0], values[1], values[2]

    @classmethod
    def _options(cls, config: dict) -> tuple[str, str, str, str] | None:
        if not isinstance(config, dict):
            return None
        base_url = _base_url(
            config.get("internalBaseUrl"),
            server_reachable=True,
        )
        credentials = cls._credentials(config)
        if base_url is None or credentials is None:
            return None
        return base_url, *credentials

    @staticmethod
    def _stream_base(config: dict, default: str) -> str | None:
        return _base_url(
            config.get("streamBaseUrl", default),
            server_reachable=False,
        )

    @classmethod
    def has_valid_options(cls, config: dict) -> bool:
        try:
            cls._category(config.get("movieCategory"), "movies")
            cls._category(config.get("seriesCategory"), "tv")
        except (AttributeError, ValueError):
            return False
        options = cls._options(config)
        return options is not None and cls._stream_base(config, options[0]) is not None

    @classmethod
    def category_for(cls, config: dict, selection: tuple[object, ...]) -> str:
        if selection == (0,):
            return cls._category(config.get("movieCategory"), "movies")
        if (
            len(selection) == 3
            and selection[0] == 1
            and all(
                isinstance(value, int) and not isinstance(value, bool) and value >= 0
                for value in selection[1:]
            )
        ):
            return cls._category(config.get("seriesCategory"), "tv")
        raise ValueError("NzbDAV selection is invalid")

    @classmethod
    def credential_binding(cls, config: dict) -> tuple[str, bytes]:
        options = cls._options(config)
        if options is None:
            raise ValueError("NzbDAV configuration is unavailable")
        base_url, api_key, username, password = options
        stream_base = cls._stream_base(config, base_url)
        if stream_base is None:
            raise ValueError("NzbDAV configuration is unavailable")
        return base_url, b"\0".join(
            value.encode("utf-8")
            for value in (api_key, username, password, stream_base)
        )

    @classmethod
    def verified_content_root(
        cls, config: dict, verified_name: str, category: str
    ) -> str:
        """Build only the sealed WebDAV subtree for an exact completed SAB job."""
        options = cls._options(config)
        if options is None:
            raise ValueError("NzbDAV configuration is unavailable")
        if (
            not isinstance(verified_name, str)
            or not verified_name.startswith("comet-")
            or len(verified_name) != 70
            or any(
                character not in "0123456789abcdef" for character in verified_name[6:]
            )
        ):
            raise ValueError("invalid NzbDAV completed job")
        category = cls._category(category, "")
        return f"{options[0]}/content/{quote(category, safe='')}/{quote(verified_name, safe='')}"

    @classmethod
    def webdav_headers(cls, config: dict) -> dict[str, str]:
        options = cls._options(config)
        if options is None:
            raise ValueError("NzbDAV configuration is unavailable")
        return {"Authorization": aiohttp.encode_basic_auth(options[2], options[3])}

    @classmethod
    def webdav_validation_root(cls, config: dict) -> str:
        options = cls._options(config)
        if options is None:
            raise ValueError("NzbDAV configuration is unavailable")
        return f"{options[0]}/content"

    @classmethod
    def completed_file_url(
        cls, config: dict, verified_name: str, category: str, relative_path: str
    ) -> str:
        root = cls.verified_content_root(config, verified_name, category)
        if (
            not isinstance(relative_path, str)
            or not relative_path
            or not _bounded_text(relative_path, 2048)
            or "\\" in relative_path
        ):
            raise ValueError("invalid NzbDAV file path")
        parts = relative_path.split("/")
        if any(not part or part in {".", ".."} for part in parts):
            raise ValueError("invalid NzbDAV file path")
        return f"{root}/{'/'.join(quote(part, safe='') for part in parts)}"

    @classmethod
    def direct_download_url(
        cls,
        config: dict,
        verified_name: str,
        category: str,
        relative_path: str,
    ) -> str:
        options = cls._options(config)
        if options is None:
            raise ValueError("NzbDAV configuration is unavailable")
        stream_base = cls._stream_base(config, options[0])
        if stream_base is None:
            raise ValueError("NzbDAV configuration is unavailable")
        internal_url = cls.completed_file_url(
            config,
            verified_name,
            category,
            relative_path,
        )
        public_url = f"{stream_base}{internal_url[len(options[0]) :]}"
        return http_url_with_basic_auth(public_url, options[2], options[3])

    async def completed_file(
        self,
        config: dict,
        verified_name: str,
        category: str,
        selection: tuple[object, ...],
    ) -> NzbDavWebDavEntry:
        """Return one direct video file only after its sealed WebDAV tree is proven."""
        if self._session is None:
            raise NzbDavError("nzbdav_unavailable", retryable=True)
        root = self.verified_content_root(config, verified_name, category)
        status, body = await self._propfind(config, root, "infinity")
        if is_success_status(status):
            try:
                entries = parse_webdav_entries(root, body)
            except ValueError:
                raise NzbDavError(
                    "nzbdav_invalid_response",
                    retryable=True,
                ) from None
        elif 400 <= status < 500 or status == 501:
            entries = await self._walk_webdav(config, root)
        else:
            raise NzbDavError("nzbdav_webdav_unavailable", retryable=True)
        files = (entry for entry in entries if not entry.is_collection)
        try:
            return select_remote_video_file(
                ((entry, entry.relative_path, entry.byte_size) for entry in files),
                selection,
            )
        except FileSelectionError:
            raise NzbDavError(
                "nzbdav_file_selection_ambiguous",
                terminal=True,
            ) from None

    async def _propfind(
        self,
        config: dict,
        url: str,
        depth: str,
    ) -> tuple[int, bytes]:
        headers = {
            "Depth": depth,
            "Accept-Encoding": "identity",
            **self.webdav_headers(config),
        }
        try:
            async with self._session.request(
                "PROPFIND",
                url,
                headers=headers,
                allow_redirects=False,
                timeout=_REQUEST_TIMEOUT,
            ) as response:
                status = response.status
                if response.status in {401, 403}:
                    raise NzbDavError(
                        "nzbdav_credentials_rejected",
                        auth_failed=True,
                        terminal=True,
                    )
                if response.status >= 500:
                    raise NzbDavError("nzbdav_unavailable", retryable=True)
                if not is_success_status(response.status):
                    return response.status, b""
                try:
                    body = await read_bounded_body(
                        response,
                        _MAX_DAV_RESPONSE_BYTES,
                    )
                except ValueError:
                    raise NzbDavError(
                        "nzbdav_invalid_response",
                        retryable=True,
                    ) from None
        except (aiohttp.ClientError, TimeoutError):
            raise NzbDavError("nzbdav_unavailable", retryable=True) from None
        return status, body

    async def _walk_webdav(
        self,
        config: dict,
        root: str,
    ) -> tuple[NzbDavWebDavEntry, ...]:
        queue = deque([(root, "")])
        entries = {}
        total_bytes = 0
        while queue:
            url, requested_relative = queue.popleft()
            status, body = await self._propfind(config, url, "1")
            if not is_success_status(status):
                raise NzbDavError(
                    "nzbdav_webdav_unavailable",
                    retryable=True,
                )
            total_bytes += len(body)
            if total_bytes > _MAX_DAV_TRAVERSAL_BYTES:
                raise NzbDavError("nzbdav_invalid_response", retryable=True)
            try:
                parsed_entries = parse_webdav_entries(root, body)
            except ValueError:
                raise NzbDavError(
                    "nzbdav_invalid_response",
                    retryable=True,
                ) from None
            for entry in parsed_entries:
                if entry.relative_path == requested_relative:
                    continue
                if entry.relative_path.rpartition("/")[0] != requested_relative:
                    raise NzbDavError("nzbdav_invalid_response", retryable=True)
                if entry.relative_path in entries:
                    entries[entry.relative_path] = entry
                    continue
                if len(entries) >= _MAX_DAV_ENTRIES:
                    raise NzbDavError("nzbdav_invalid_response", retryable=True)
                depth = len(entry.relative_path.split("/"))
                if depth > _MAX_DAV_DEPTH:
                    raise NzbDavError("nzbdav_invalid_response", retryable=True)
                entries[entry.relative_path] = entry
                if entry.is_collection:
                    queue.append(
                        (
                            f"{root}/"
                            + "/".join(
                                quote(part, safe="")
                                for part in entry.relative_path.split("/")
                            ),
                            entry.relative_path,
                        )
                    )
        return tuple(entries.values())

    async def validate_config(self, config: dict) -> ProviderStatus:
        credentials = self._credentials(config)
        try:
            required_categories = {
                self._category(config.get("movieCategory"), "movies"),
                self._category(config.get("seriesCategory"), "tv"),
            }
        except (AttributeError, ValueError):
            required_categories = set()
        if credentials is None or not required_categories:
            return ProviderStatus(
                Readiness.TERMINAL_FAILURE,
                Actionability.NONE,
                code="configuration_required",
            )
        try:
            base_url = normalize_upstream_base_url(
                config.get("internalBaseUrl"),
                allowed_http_origins=settings.USENET_PRIVATE_UPSTREAM_ORIGINS,
            )
            normalize_upstream_base_url(
                config.get("streamBaseUrl", base_url),
                allowed_http_origins=None,
            )
        except UpstreamUrlError as exc:
            return ProviderStatus(
                Readiness.TERMINAL_FAILURE,
                Actionability.NONE,
                code=exc.code,
            )
        if self._session is None:
            return ProviderStatus(
                Readiness.RETRYABLE_FAILURE,
                Actionability.REMOTE_PREPARE,
                code="validation_unavailable",
            )
        api_key, username, password = credentials
        sab_ok = False
        sab_auth = False
        try:
            headers = {
                "X-Api-Key": api_key,
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            }
            async with self._session.get(
                f"{base_url}/api",
                params={"mode": "get_cats", "output": "json"},
                headers=headers,
                allow_redirects=False,
                timeout=_REQUEST_TIMEOUT,
            ) as categories_response:
                sab_auth = categories_response.status in {401, 403}
                category_payload = (
                    await read_provider_json(categories_response)
                    if is_success_status(categories_response.status)
                    else {}
                )
            if sab_auth:
                return ProviderStatus(
                    Readiness.TERMINAL_FAILURE,
                    Actionability.NONE,
                    code="credentials_rejected",
                    auth_failed=True,
                )
            categories = category_payload.get("categories")
            sab_ok = isinstance(categories, list) and all(
                category in categories for category in required_categories
            )
        except (aiohttp.ClientError, TimeoutError, ProviderJsonError):
            pass
        dav_ok = False
        dav_auth = False
        try:
            dav_headers = {
                "Depth": "0",
                "Authorization": aiohttp.encode_basic_auth(username, password),
                "Accept-Encoding": "identity",
            }
            async with self._session.request(
                "PROPFIND",
                self.webdav_validation_root(config),
                headers=dav_headers,
                allow_redirects=False,
                timeout=_REQUEST_TIMEOUT,
            ) as dav_response:
                dav_ok = dav_response.status in {200, 207}
                dav_auth = dav_response.status in {401, 403}
        except (aiohttp.ClientError, TimeoutError):
            pass
        if sab_auth or dav_auth:
            return ProviderStatus(
                Readiness.TERMINAL_FAILURE,
                Actionability.NONE,
                code="credentials_rejected",
                auth_failed=True,
            )
        if not sab_ok or not dav_ok:
            return ProviderStatus(
                Readiness.RETRYABLE_FAILURE,
                Actionability.REMOTE_PREPARE,
                code="validation_incomplete",
            )
        return ProviderStatus(Readiness.REQUIRES_PREPARE, Actionability.REMOTE_PREPARE)

    async def submit_artifact(
        self, config: dict, document: bytes, artifact_sha256: str, category: str
    ) -> str:
        options = self._options(config)
        if options is None or self._session is None:
            raise ValueError("NzbDAV configuration is unavailable")
        if (
            not isinstance(document, bytes)
            or not document
            or len(document) > MAX_NZB_METADATA_BYTES
            or not isinstance(artifact_sha256, str)
            or len(artifact_sha256) != 64
            or any(character not in "0123456789abcdef" for character in artifact_sha256)
        ):
            raise ValueError("invalid NzbDAV artifact submission")
        try:
            category = self._category(category, "")
        except ValueError as exc:
            raise ValueError("invalid NzbDAV artifact submission") from exc
        base_url, api_key, _username, _password = options
        filename = f"comet-{artifact_sha256}.nzb"
        form = aiohttp.FormData()
        form.add_field(
            "nzbFile", document, filename=filename, content_type="application/x-nzb"
        )
        params = {
            "mode": "addfile",
            "output": "json",
            "cat": category,
            "priority": "1",
            "pp": "-1",
            "nzbname": filename,
        }
        try:
            async with self._session.post(
                f"{base_url}/api",
                params=params,
                headers={
                    "X-Api-Key": api_key,
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                },
                data=form,
                allow_redirects=False,
                timeout=_REQUEST_TIMEOUT,
            ) as response:
                if response.status in {401, 403}:
                    raise NzbDavError(
                        "nzbdav_credentials_rejected",
                        auth_failed=True,
                        terminal=True,
                        mutation_rejected=True,
                    )
                if response.status == 429:
                    raise NzbDavError(
                        "nzbdav_rate_limited",
                        retryable=True,
                        mutation_rejected=True,
                    )
                if response.status >= 500:
                    raise NzbDavError("nzbdav_unavailable", retryable=True)
                if 400 <= response.status < 500:
                    raise NzbDavError(
                        "nzbdav_submission_failed",
                        terminal=True,
                        mutation_rejected=True,
                    )
                if not is_success_status(response.status):
                    raise NzbDavError(
                        "nzbdav_submission_failed",
                        terminal=True,
                    )
                try:
                    payload = await read_provider_json(response)
                except ProviderJsonError:
                    raise NzbDavError(
                        "nzbdav_invalid_response",
                        retryable=True,
                    ) from None
        except (aiohttp.ClientError, TimeoutError):
            raise NzbDavError("nzbdav_unavailable", retryable=True) from None
        ids = payload.get("nzo_ids") if isinstance(payload, dict) else None
        if not isinstance(ids, list) or not ids:
            raise NzbDavError("nzbdav_invalid_response", retryable=True)
        return _job_uuid(ids[0])

    async def reconcile_artifact(
        self,
        config: dict,
        artifact_sha256: str,
        category: str,
    ) -> NzbDavReconciliation | None:
        """Reconcile one sealed deterministic submission without global adoption."""
        options = self._options(config)
        if (
            options is None
            or self._session is None
            or not isinstance(artifact_sha256, str)
            or len(artifact_sha256) != 64
            or any(character not in "0123456789abcdef" for character in artifact_sha256)
        ):
            raise ValueError("NzbDAV reconciliation is unavailable")
        category = self._category(category, "")
        base_url, api_key, _username, _password = options
        headers = {
            "X-Api-Key": api_key,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        filename = f"comet-{artifact_sha256}.nzb"
        queue_matches = []
        history_matches = []
        try:
            async with asyncio.timeout(_SAB_RECONCILIATION_TIMEOUT_SECONDS):
                for mode in ("queue", "history"):
                    for start in range(
                        0,
                        _MAX_SAB_RECONCILIATION_ITEMS,
                        _SAB_PAGE_SIZE,
                    ):
                        async with self._session.get(
                            f"{base_url}/api",
                            params={
                                "mode": mode,
                                "output": "json",
                                "start": str(start),
                                "limit": str(_SAB_PAGE_SIZE),
                            },
                            headers=headers,
                            allow_redirects=False,
                            timeout=_REQUEST_TIMEOUT,
                        ) as response:
                            if response.status in {401, 403}:
                                raise NzbDavError(
                                    "nzbdav_credentials_rejected",
                                    auth_failed=True,
                                    terminal=True,
                                )
                            if response.status == 429:
                                raise NzbDavError(
                                    "nzbdav_rate_limited",
                                    retryable=True,
                                )
                            if response.status >= 500:
                                raise NzbDavError(
                                    "nzbdav_unavailable",
                                    retryable=True,
                                )
                            if not is_success_status(response.status):
                                raise NzbDavError(
                                    "nzbdav_reconciliation_unavailable",
                                    retryable=True,
                                )
                            payload = await read_provider_json(response)
                        page = _sab_slots(payload, mode)
                        for slot in page:
                            if not isinstance(slot, dict):
                                raise NzbDavError(
                                    "nzbdav_invalid_response",
                                    retryable=True,
                                )
                            slot_category = _sab_alias(
                                slot,
                                "cat" if mode == "queue" else "category",
                                "category" if mode == "queue" else "cat",
                            )
                            if mode == "queue":
                                if (
                                    slot.get("filename") == filename
                                    and slot_category == category
                                ):
                                    queue_matches.append(_job_uuid(slot.get("nzo_id")))
                            elif (
                                slot.get("nzb_name") == filename
                                and slot_category == category
                            ):
                                status = _sab_history_status(slot.get("status"))
                                job_id = _job_uuid(slot.get("nzo_id"))
                                history_matches.append(
                                    NzbDavReconciliation(
                                        job_id,
                                        status
                                        if status in {"completed", "failed"}
                                        else "queued",
                                    )
                                )
                        if len(page) < _SAB_PAGE_SIZE:
                            break
                    else:
                        raise NzbDavError(
                            "nzbdav_reconciliation_unavailable",
                            retryable=True,
                        )
        except ProviderJsonError:
            raise NzbDavError(
                "nzbdav_invalid_response",
                retryable=True,
            ) from None
        except (aiohttp.ClientError, TimeoutError):
            raise NzbDavError("nzbdav_unavailable", retryable=True) from None
        if queue_matches:
            return NzbDavReconciliation(queue_matches[0], "queued")
        if not history_matches:
            return None
        return history_matches[0]

    async def poll_artifact(
        self, config: dict, job_id: str, artifact_sha256: str, category: str
    ) -> NzbDavJob:
        options = self._options(config)
        if options is None or self._session is None:
            raise ValueError("NzbDAV configuration is unavailable")
        try:
            normalized_job_id = str(uuid.UUID(job_id))
        except ValueError as exc:
            raise ValueError("invalid NzbDAV job") from exc
        if job_id != normalized_job_id:
            raise ValueError("invalid NzbDAV job")
        job_id = normalized_job_id
        if (
            not isinstance(artifact_sha256, str)
            or len(artifact_sha256) != 64
            or any(character not in "0123456789abcdef" for character in artifact_sha256)
        ):
            raise ValueError("invalid NzbDAV job")
        category = self._category(category, "")
        base_url, api_key, _username, _password = options
        slots_by_mode: dict[str, list] = {}
        for mode in ("queue", "history"):
            try:
                async with self._session.get(
                    f"{base_url}/api",
                    params={
                        "mode": mode,
                        "output": "json",
                        "nzo_ids": job_id,
                    },
                    headers={
                        "X-Api-Key": api_key,
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                    },
                    allow_redirects=False,
                    timeout=_REQUEST_TIMEOUT,
                ) as response:
                    if response.status in {401, 403}:
                        return _terminal_job(job_id, "nzbdav_credentials_rejected")
                    if not is_success_status(response.status):
                        raise NzbDavError(
                            "nzbdav_unavailable",
                            retryable=True,
                        )
                    try:
                        payload = await read_provider_json(response)
                    except ProviderJsonError:
                        raise NzbDavError(
                            "nzbdav_invalid_response",
                            retryable=True,
                        ) from None
                    slots = _sab_slots(payload, mode)
            except (aiohttp.ClientError, TimeoutError):
                raise NzbDavError("nzbdav_unavailable", retryable=True) from None
            matching = []
            for value in slots:
                if not isinstance(value, dict):
                    raise NzbDavError(
                        "nzbdav_invalid_response",
                        retryable=True,
                    )
                if _job_uuid(value.get("nzo_id")) == job_id:
                    matching.append(value)
            slots_by_mode[mode] = matching
            if mode == "queue" and matching:
                item = matching[0]
                slot_category = _sab_alias(item, "cat", "category")
                if slot_category != category:
                    return _terminal_job(job_id, "category_mismatch", observed=True)
                if item.get("filename") != f"comet-{artifact_sha256}.nzb":
                    return _terminal_job(job_id, "job_mismatch", observed=True)
                return NzbDavJob(
                    job_id,
                    ProviderStatus(
                        Readiness.PREPARING,
                        Actionability.REMOTE_PREPARE,
                    ),
                    observed=True,
                )
        if not slots_by_mode["history"]:
            return _terminal_job(job_id, "remote_item_missing")
        item = slots_by_mode["history"][0]
        slot_category = _sab_alias(item, "category", "cat")
        if slot_category != category:
            return _terminal_job(job_id, "category_mismatch", observed=True)
        state = _sab_history_status(item.get("status"))
        if state == "failed":
            return _terminal_job(job_id, "remote_failed", observed=True)
        expected = f"comet-{artifact_sha256}"
        name = item.get("nzb_name")
        normalized = name.removesuffix(".nzb") if isinstance(name, str) else ""
        if normalized != expected:
            return _terminal_job(job_id, "job_mismatch", observed=True)
        if state == "completed":
            return NzbDavJob(
                job_id,
                ProviderStatus(
                    Readiness.REQUIRES_PREPARE,
                    Actionability.REMOTE_PREPARE,
                ),
                normalized,
                True,
            )
        return NzbDavJob(
            job_id,
            ProviderStatus(Readiness.PREPARING, Actionability.REMOTE_PREPARE),
            observed=True,
        )


def _terminal_job(job_id: str, code: str, *, observed: bool = False) -> NzbDavJob:
    return NzbDavJob(
        job_id,
        ProviderStatus(
            Readiness.TERMINAL_FAILURE,
            Actionability.NONE,
            code=code,
        ),
        observed=observed,
    )


def _sab_slots(payload: object, key: str) -> list:
    container = payload.get(key) if isinstance(payload, dict) else None
    slots = container.get("slots") if isinstance(container, dict) else None
    if not isinstance(slots, list):
        raise NzbDavError("nzbdav_invalid_response", retryable=True)
    return slots


def _sab_alias(slot: dict, primary: str, alias: str) -> object:
    return slot.get(primary, slot.get(alias))


def _sab_history_status(value: object) -> str:
    if not isinstance(value, str):
        raise NzbDavError("nzbdav_invalid_response", retryable=True)
    normalized = value.casefold()
    if normalized in {"completed", "failed"}:
        return normalized
    return "preparing"


def _job_uuid(value: object) -> str:
    if not isinstance(value, str):
        raise NzbDavError("nzbdav_invalid_response", retryable=True)
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise NzbDavError("nzbdav_invalid_response", retryable=True) from None
