"""Shared canonical archive-path validation for Python trust boundaries."""

from __future__ import annotations

import unicodedata

MAX_ARCHIVE_PATH_BYTES = 2_048
MAX_ARCHIVE_PATH_COMPONENTS = 64

_WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


def normalize_archive_relative_path(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        encoded_length = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return None
    value = value.replace("\\", "/")
    if (
        not value
        or encoded_length > MAX_ARCHIVE_PATH_BYTES
        or value.startswith("/")
        or "\x00" in value
        or any(ord(character) < 32 for character in value)
        or unicodedata.normalize("NFC", value) != value
        or any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value
        )
    ):
        return None
    parts = value.split("/")
    if len(parts) > MAX_ARCHIVE_PATH_COMPONENTS or any(
        not part
        or part in {".", ".."}
        or part != part.rstrip(" .")
        or ":" in part
        or len(part.encode("utf-8")) > 255
        or part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED
        for part in parts
    ):
        return None
    return "/".join(parts)
