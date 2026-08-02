from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.types import Receive, Scope, Send

from comet.utils.cache import NO_CACHE_HEADERS
from comet.utils.status_keys import normalize_status_key

STATUS_VIDEO_DIR = Path("comet/assets/status_videos")
DEFAULT_STATUS_KEY = "UNKNOWN"


class _NonSeekableFileResponse(FileResponse):
    """Serve a complete file while explicitly disabling byte-range playback."""

    def __init__(self, path: str, *, headers: dict[str, str]) -> None:
        super().__init__(path, headers={**headers, "Accept-Ranges": "none"})

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        request_headers = scope.get("headers", ())
        if any(name.lower() == b"range" for name, _value in request_headers):
            scope = {
                **scope,
                "headers": [
                    (name, value)
                    for name, value in request_headers
                    if name.lower() != b"range"
                ],
            }
        await super().__call__(scope, receive, send)


def _iter_normalized_keys(status_keys: Iterable[str | None]) -> list[str]:
    normalized_keys = []
    seen = set()
    for status_key in status_keys:
        normalized = normalize_status_key(status_key)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        normalized_keys.append(normalized)
    return normalized_keys


def _status_video_directory_revision() -> int | None:
    try:
        return STATUS_VIDEO_DIR.stat().st_mtime_ns
    except FileNotFoundError:
        return None


@lru_cache(maxsize=4)
def _build_status_video_index(
    directory_revision: int | None,
) -> dict[str, str]:
    del directory_revision
    status_files = sorted(STATUS_VIDEO_DIR.glob("*.mp4"))
    status_video_index = {}

    for status_file in status_files:
        normalized_key = normalize_status_key(status_file.stem)
        if normalized_key and normalized_key not in status_video_index:
            status_video_index[normalized_key] = str(status_file)

    return status_video_index


def resolve_status_video_path(
    status_keys: Iterable[str | None],
    default_key: str = DEFAULT_STATUS_KEY,
) -> str | None:
    status_video_index = _build_status_video_index(_status_video_directory_revision())

    for key in _iter_normalized_keys(status_keys):
        video_path = status_video_index.get(key)
        if video_path:
            return video_path

    default_normalized = normalize_status_key(default_key) or DEFAULT_STATUS_KEY
    default_video = status_video_index.get(default_normalized)
    if default_video:
        return default_video

    unknown_video = status_video_index.get(DEFAULT_STATUS_KEY)
    if unknown_video:
        return unknown_video

    return None


def build_status_video_response(
    status_keys: Iterable[str | None],
    default_key: str = DEFAULT_STATUS_KEY,
) -> Response:
    status_keys_tuple = tuple(status_keys)
    video_path = resolve_status_video_path(status_keys_tuple, default_key)

    if video_path is None:
        normalized_default_key = normalize_status_key(default_key) or DEFAULT_STATUS_KEY
        normalized_status_keys = _iter_normalized_keys(status_keys_tuple)
        return JSONResponse(
            status_code=500,
            content={
                "detail": "Status video asset is missing on server.",
                "status_keys": normalized_status_keys,
                "default_key": normalized_default_key,
            },
            headers=NO_CACHE_HEADERS,
        )

    return _NonSeekableFileResponse(
        video_path,
        headers=NO_CACHE_HEADERS,
    )
