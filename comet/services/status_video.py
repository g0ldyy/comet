from functools import lru_cache
from pathlib import Path
from typing import Iterable

from fastapi.responses import FileResponse, JSONResponse, Response

from comet.core.logger import logger
from comet.utils.cache import NO_CACHE_HEADERS
from comet.utils.status_keys import normalize_status_key

STATUS_VIDEO_DIR = Path("comet/assets/status_videos")
DEFAULT_STATUS_KEY = "UNKNOWN"


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
) -> str | None:
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

    fallback_path = STATUS_VIDEO_DIR / f"{default_normalized}.mp4"
    if fallback_path.exists():
        return str(fallback_path)

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
        logger.error(
            f"Missing status video in {STATUS_VIDEO_DIR} for keys={normalized_status_keys} "
            f"and default={normalized_default_key}"
        )
        return JSONResponse(
            status_code=500,
            content={
                "detail": "Status video asset is missing on server.",
                "status_keys": normalized_status_keys,
                "default_key": normalized_default_key,
            },
            headers=NO_CACHE_HEADERS,
        )

    return FileResponse(
        video_path,
        headers=NO_CACHE_HEADERS,
    )
