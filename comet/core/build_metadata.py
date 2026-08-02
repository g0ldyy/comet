"""Canonical validation for image and local Git metadata."""

import re
from datetime import datetime

_COMMIT_PATTERN = re.compile(r"[0-9a-fA-F]{7,40}")
_BRANCH_PATTERN = re.compile(r"[^\s\x00-\x1f\x7f]{1,255}")


def normalize_commit(value: object) -> str | None:
    if type(value) is not str or _COMMIT_PATTERN.fullmatch(value) is None:
        return None
    return value.lower()


def normalize_branch(value: object) -> str | None:
    if type(value) is not str or _BRANCH_PATTERN.fullmatch(value) is None:
        return None
    return value


def normalize_build_date(value: object) -> str | None:
    if type(value) is not str or not value or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return value if parsed.tzinfo is not None else None
