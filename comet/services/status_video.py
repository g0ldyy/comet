import re
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from fastapi.responses import FileResponse

from comet.utils.cache import NO_CACHE_HEADERS

STATUS_VIDEO_DIR = Path("comet/assets/status_videos")
DEFAULT_STATUS_KEY = "UNKNOWN"

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")
_MULTI_UNDERSCORE = re.compile(r"_+")


def normalize_status_key(status_key: str | None) -> str | None:
    if not status_key:
        return None
    normalized = _NON_ALNUM.sub("_", str(status_key).strip()).strip("_").upper()
    normalized = _MULTI_UNDERSCORE.sub("_", normalized)
    return normalized or None


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


@lru_cache(maxsize=1)
def _build_status_video_index() -> tuple[dict[str, str], str | None]:
    status_files = sorted(STATUS_VIDEO_DIR.glob("*.mp4"))
    status_video_index = {}

    for status_file in status_files:
        normalized_key = normalize_status_key(status_file.stem)
        if normalized_key and normalized_key not in status_video_index:
            status_video_index[normalized_key] = str(status_file)

    first_status_video = str(status_files[0]) if status_files else None
    return status_video_index, first_status_video


def resolve_status_video_path(
    status_keys: Iterable[str | None],
    default_key: str = DEFAULT_STATUS_KEY,
) -> str:
    status_video_index, first_status_video = _build_status_video_index()

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

    if first_status_video:
        return first_status_video

    return str(STATUS_VIDEO_DIR / f"{default_normalized}.mp4")


def build_status_video_response(
    status_keys: Iterable[str | None],
    default_key: str = DEFAULT_STATUS_KEY,
) -> FileResponse:
    return FileResponse(
        resolve_status_video_path(status_keys, default_key),
        headers=NO_CACHE_HEADERS,
    )
