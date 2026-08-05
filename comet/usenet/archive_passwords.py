"""Archive-passphrase discovery shared by Usenet ingestion and playback."""

from collections.abc import Mapping

MAX_ARCHIVE_PASSPHRASE_BYTES = 4 * 1024
ARCHIVE_CREDENTIAL_FAILURES = frozenset(
    {"archive_password_required", "archive_password_invalid"}
)


def resolve_archive_passphrase(
    metadata: Mapping[str, str], release_name: str | None = None
) -> str | None:
    """Prefer NZB metadata, then the conventional ``{password}`` title token."""
    candidate = metadata.get("password")
    if not isinstance(candidate, str):
        candidate = None
    if not candidate and release_name:
        candidate = _title_token(release_name, "{{", "}}")
        if candidate is None:
            candidate = _title_token(release_name, "{", "}")
    if not candidate:
        return None
    candidate = candidate.strip()
    try:
        encoded = candidate.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if (
        not candidate
        or len(encoded) > MAX_ARCHIVE_PASSPHRASE_BYTES
        or any(
            character.isascii() and not character.isprintable()
            for character in candidate
        )
    ):
        return None
    return candidate


def _title_token(value: str, opening: str, closing: str) -> str | None:
    start = value.find(opening)
    if start < 0:
        return None
    start += len(opening)
    end = value.find(closing, start)
    return value[start:end] if end >= start else None
