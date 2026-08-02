from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from comet.api.v1.contracts import StreamActivityBucket

StreamActivityRange = Literal["auto", "15m", "1h", "6h", "24h", "7d"]

_RETENTION_SECONDS = 7 * 24 * 60 * 60
_FIXED_WINDOWS: dict[str, tuple[int, int]] = {
    "15m": (15 * 60, 15),
    "1h": (60 * 60, 60),
    "6h": (6 * 60 * 60, 5 * 60),
    "24h": (24 * 60 * 60, 20 * 60),
    "7d": (_RETENTION_SECONDS, 2 * 60 * 60),
}
_AUTO_BUCKETS = (
    (15 * 60, 15),
    (60 * 60, 60),
    (6 * 60 * 60, 5 * 60),
    (24 * 60 * 60, 20 * 60),
    (_RETENTION_SECONDS, 2 * 60 * 60),
)
_EMPTY_AUTO_WINDOW_SECONDS = 15 * 60


@dataclass(frozen=True, slots=True)
class ActivityWindow:
    started_at: float
    ended_at: float
    bucket_seconds: int
    bucket_count: int


def earliest_timestamp(*values: float | None) -> float | None:
    timestamps = [value for value in values if value is not None]
    return min(timestamps) if timestamps else None


def activity_window(
    now: float,
    selection: StreamActivityRange,
    activity_started_at: float | None,
) -> ActivityWindow:
    if selection == "auto":
        if activity_started_at is None:
            span = _EMPTY_AUTO_WINDOW_SECONDS
        else:
            span = min(
                _RETENTION_SECONDS,
                max(60, now - min(activity_started_at, now)),
            )
        bucket_seconds = next(
            bucket for maximum, bucket in _AUTO_BUCKETS if span <= maximum
        )
    else:
        span, bucket_seconds = _FIXED_WINDOWS[selection]

    raw_start = now - span
    if selection == "auto" and activity_started_at is not None:
        raw_start = max(raw_start, min(activity_started_at, now))
    started_at = math.floor(raw_start / bucket_seconds) * bucket_seconds
    bucket_count = max(1, math.ceil((now - started_at) / bucket_seconds))
    return ActivityWindow(
        started_at=started_at,
        ended_at=now,
        bucket_seconds=bucket_seconds,
        bucket_count=bucket_count,
    )


def activity_buckets(window: ActivityWindow) -> list[StreamActivityBucket]:
    return [
        StreamActivityBucket(
            started_at=window.started_at + index * window.bucket_seconds,
            bytes_transferred=0,
            completed=0,
            failed=0,
            interrupted=0,
            active=0,
            peak_active=None,
        )
        for index in range(window.bucket_count)
    ]


def current_bucket_index(window: ActivityWindow) -> int:
    return window.bucket_count - 1
