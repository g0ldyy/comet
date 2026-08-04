"""AltMount native-stream capability validation."""

import hashlib
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlsplit

import aiohttp

from comet.core.models import settings
from comet.core.provider_json import (
    ProviderJsonError,
    is_success_status,
    read_provider_json,
)
from comet.playback.altmount_contract import (
    valid_altmount_virtual_path,
)
from comet.playback.base import (
    Actionability,
    BytePath,
    ProviderDescriptor,
    ProviderRuntimeError,
    ProviderStatus,
    Readiness,
)
from comet.usenet.file_selection import matches_episode_path
from comet.usenet.limits import MAX_NZB_METADATA_BYTES
from comet.usenet.upstream import UpstreamUrlError, normalize_upstream_base_url
from comet.utils.parsing import is_video

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=5, sock_read=10)
_MAX_RESPONSE_BYTES = 512 * 1024
_NATIVE_WAIT_SECONDS = 10
_PREPARATION_DEADLINE_SECONDS = 300


class AltMountError(ProviderRuntimeError):
    """AltMount failure with provider-bound transition semantics."""


@dataclass(frozen=True, slots=True)
class AltMountRemoteFile:
    virtual_path: str


@dataclass(frozen=True, slots=True)
class AltMountSelectedFile:
    virtual_path: str


@dataclass(frozen=True, slots=True)
class AltMountStream:
    files: tuple[AltMountRemoteFile, ...]


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


class AltMountProvider:
    descriptor = ProviderDescriptor(
        kind="altmount",
        label="AltMount",
        accepted_locator_kinds=frozenset({"nzb_artifact"}),
        byte_paths=frozenset({BytePath.CLOUD_REDIRECT}),
        mutates_upstream=True,
    )

    def __init__(self, session):
        self._session = session

    @staticmethod
    def _options(config: dict) -> tuple[str, str] | None:
        if not isinstance(config, dict):
            return None
        api_key = config.get("apiKey")
        if not _bounded_text(api_key, 1024):
            return None
        try:
            base_url = normalize_upstream_base_url(
                config.get("internalBaseUrl"),
                allowed_http_origins=settings.USENET_PRIVATE_UPSTREAM_ORIGINS,
            )
        except UpstreamUrlError:
            return None
        return base_url, api_key

    @classmethod
    def has_valid_options(cls, config: dict) -> bool:
        try:
            cls.category_for(config)
        except (AttributeError, ValueError):
            return False
        options = cls._options(config)
        return options is not None and cls._stream_base(config, options[0]) is not None

    @staticmethod
    def preparation_deadline() -> int:
        return _PREPARATION_DEADLINE_SECONDS

    @staticmethod
    def _stream_base(config: dict, default: str) -> str | None:
        try:
            return normalize_upstream_base_url(
                config.get("streamBaseUrl", default),
                allowed_http_origins=None,
            )
        except UpstreamUrlError:
            return None

    @classmethod
    def credential_binding(cls, config: dict) -> tuple[str, bytes]:
        options = cls._options(config)
        if options is None:
            raise ValueError("AltMount configuration is unavailable")
        return options[0], options[1].encode("utf-8")

    @staticmethod
    def category_for(config: dict) -> str:
        category = config.get("category", "stremio")
        if not isinstance(category, str) or not category:
            raise ValueError("AltMount category is invalid")
        try:
            category.encode()
        except UnicodeEncodeError as exc:
            raise ValueError("AltMount category is invalid") from exc
        return category

    @staticmethod
    def filename_for(
        artifact_sha256: str,
        selection: tuple[object, ...],
    ) -> str:
        if selection == (0,):
            prefix = "Comet.Movie"
        elif (
            len(selection) == 3
            and selection[0] == 1
            and all(
                isinstance(value, int)
                and not isinstance(value, bool)
                and 0 <= value <= 999
                for value in selection[1:]
            )
        ):
            prefix = f"Comet.S{selection[1]:02d}E{selection[2]:02d}"
        else:
            raise ValueError("AltMount selection is invalid")
        if (
            not isinstance(artifact_sha256, str)
            or len(artifact_sha256) != 64
            or any(character not in "0123456789abcdef" for character in artifact_sha256)
        ):
            raise ValueError("AltMount artifact is invalid")
        return f"{prefix}.{artifact_sha256}.nzb"

    @staticmethod
    def _stream_paths(streams: object) -> tuple[str, ...]:
        if not isinstance(streams, list):
            raise AltMountError(
                "altmount_invalid_response",
                terminal_status="invalid",
            )
        paths = {}
        for stream in streams:
            if not isinstance(stream, dict):
                raise AltMountError(
                    "altmount_invalid_response",
                    terminal_status="invalid",
                )
            virtual_path = stream.get("path")
            try:
                if virtual_path is None:
                    query = parse_qs(
                        urlsplit(stream["url"]).query,
                        keep_blank_values=True,
                    )
                    virtual_path = query["path"][0]
            except (KeyError, TypeError, ValueError):
                raise AltMountError(
                    "altmount_invalid_response",
                    terminal_status="invalid",
                ) from None
            if not valid_altmount_virtual_path(virtual_path):
                raise AltMountError(
                    "altmount_invalid_response",
                    terminal_status="invalid",
                )
            paths.setdefault(virtual_path, None)
        return tuple(paths)

    @classmethod
    def stream_url(cls, config: dict, virtual_path: str) -> str:
        options = cls._options(config)
        stream_base = (
            cls._stream_base(config, options[0]) if options is not None else None
        )
        if options is None or stream_base is None:
            raise ValueError("AltMount configuration is unavailable")
        if not valid_altmount_virtual_path(virtual_path):
            raise ValueError("AltMount path is unavailable")
        expected_key = hashlib.sha256(options[1].encode()).hexdigest()
        return f"{stream_base}/api/files/stream?{urlencode({'path': virtual_path, 'download_key': expected_key})}"

    async def validate_config(self, config: dict) -> ProviderStatus:
        try:
            self.category_for(config)
        except (AttributeError, ValueError):
            return ProviderStatus(
                Readiness.TERMINAL_FAILURE,
                Actionability.NONE,
                code="configuration_required",
            )
        if not isinstance(config, dict) or not _bounded_text(config.get("apiKey"), 1024):
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
        api_key = config["apiKey"]
        empty_multipart = aiohttp.MultipartWriter("form-data")
        try:
            async with self._session.post(
                f"{base_url}/api/nzb/streams",
                headers={
                    "X-Api-Key": api_key,
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                },
                data=empty_multipart,
                allow_redirects=False,
                timeout=_REQUEST_TIMEOUT,
            ) as response:
                status = response.status
        except (aiohttp.ClientError, TimeoutError):
            return ProviderStatus(
                Readiness.RETRYABLE_FAILURE,
                Actionability.REMOTE_PREPARE,
                code="validation_unavailable",
            )
        if status in {401, 403}:
            return ProviderStatus(
                Readiness.TERMINAL_FAILURE,
                Actionability.NONE,
                code="credentials_rejected",
                auth_failed=True,
            )
        if status in {404, 405, 501}:
            return ProviderStatus(
                Readiness.TERMINAL_FAILURE,
                Actionability.NONE,
                code="native_api_required",
            )
        if status == 422:
            return ProviderStatus(
                Readiness.REQUIRES_PREPARE, Actionability.REMOTE_PREPARE, None
            )
        return ProviderStatus(
            Readiness.RETRYABLE_FAILURE,
            Actionability.REMOTE_PREPARE,
            code="validation_incomplete",
        )

    @staticmethod
    def select_file(
        result: AltMountStream,
        selection: tuple[object, ...],
    ) -> AltMountSelectedFile:
        """Select exactly one video path without opening the media URL."""
        videos = tuple(file for file in result.files if is_video(file.virtual_path))
        if selection == (0,):
            candidates = videos
        elif (
            len(selection) == 3
            and selection[0] == 1
            and all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in selection[1:]
            )
        ):
            season, episode = selection[1:]
            candidates = tuple(
                file
                for file in videos
                if matches_episode_path(
                    file.virtual_path,
                    season,
                    episode,
                )
            )
        else:
            raise AltMountError(
                "altmount_file_selection_ambiguous",
                terminal_status="invalid",
            )
        if len(candidates) != 1:
            raise AltMountError(
                "altmount_file_selection_ambiguous",
                terminal_status="invalid",
            )
        return AltMountSelectedFile(candidates[0].virtual_path)

    async def submit_artifact(
        self,
        config: dict,
        document: bytes,
        artifact_sha256: str,
        selection: tuple[object, ...],
    ) -> AltMountStream:
        options = self._options(config)
        if options is None or self._session is None:
            raise ValueError("AltMount configuration is unavailable")
        if (
            not isinstance(document, bytes)
            or not document
            or len(document) > MAX_NZB_METADATA_BYTES
            or not isinstance(artifact_sha256, str)
            or len(artifact_sha256) != 64
            or any(character not in "0123456789abcdef" for character in artifact_sha256)
        ):
            raise ValueError("invalid AltMount artifact submission")
        base_url, api_key = options
        category = self.category_for(config)
        filename = self.filename_for(artifact_sha256, selection)
        form = aiohttp.FormData()
        form.add_field(
            "file", document, filename=filename, content_type="application/x-nzb"
        )
        form.add_field("category", category)
        form.add_field("timeout", str(_NATIVE_WAIT_SECONDS))
        if selection[:1] == (1,):
            form.add_field("season", str(selection[1]))
            form.add_field("episode", str(selection[2]))
        try:
            async with self._session.post(
                f"{base_url}/api/nzb/streams",
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
                    raise AltMountError(
                        "altmount_credentials_rejected",
                        auth_failed=True,
                        terminal_status="failed",
                    )
                if response.status in {404, 405, 501}:
                    raise AltMountError(
                        "altmount_native_api_required",
                        terminal_status="failed",
                    )
                if response.status == 408:
                    raise AltMountError(
                        "altmount_unavailable",
                        retryable=True,
                    )
                if response.status == 429:
                    raise AltMountError(
                        "altmount_rate_limited",
                        retryable=True,
                    )
                if response.status >= 500:
                    raise AltMountError(
                        "altmount_unavailable",
                        retryable=True,
                    )
                if not is_success_status(response.status):
                    raise AltMountError(
                        "altmount_submission_failed",
                        terminal_status="invalid",
                    )
                payload = await read_provider_json(
                    response,
                    maximum=_MAX_RESPONSE_BYTES,
                )
        except ProviderJsonError:
            raise AltMountError(
                "altmount_invalid_response",
                terminal_status="invalid",
            ) from None
        except (aiohttp.ClientError, TimeoutError):
            raise AltMountError(
                "altmount_unavailable",
                retryable=True,
            ) from None
        paths = self._stream_paths(payload.get("streams"))
        if not paths:
            raise AltMountError(
                "altmount_invalid_response",
                terminal_status="invalid",
            )
        return AltMountStream(tuple(AltMountRemoteFile(path) for path in paths))
