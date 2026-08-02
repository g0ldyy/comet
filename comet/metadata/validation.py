"""Bounded values shared by metadata decoders and their persistent cache."""

_MAX_TITLE_BYTES = 1024
_MAX_ALIAS_SCOPES = 64
_MAX_ALIASES = 512
_MAX_EPISODE_COORDINATE = 2_147_483_647


def metadata_text(value: object, *, maximum: int = _MAX_TITLE_BYTES) -> str | None:
    if not isinstance(value, str) or not (value := value.strip()):
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return None
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if len(encoded) > maximum:
        return None
    return value


def metadata_year(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1800 <= value <= 9999 else None


def episode_coordinate(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        parsed = value
    elif (
        isinstance(value, str)
        and value
        and len(value) <= 10
        and value.isascii()
        and value.isdigit()
    ):
        parsed = int(value)
    else:
        return None
    return parsed if 0 <= parsed <= _MAX_EPISODE_COORDINATE else None


def normalize_aliases(value: object) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}

    normalized: dict[str, list[str]] = {}
    seen: dict[str, set[str]] = {}
    alias_count = 0
    for raw_scope, raw_titles in value.items():
        if len(normalized) >= _MAX_ALIAS_SCOPES or alias_count >= _MAX_ALIASES:
            break
        if (
            not isinstance(raw_scope, str)
            or not raw_scope
            or len(raw_scope) > 32
            or not raw_scope.isascii()
            or any(
                ord(character) < 33 or ord(character) == 127 for character in raw_scope
            )
            or not isinstance(raw_titles, list)
        ):
            continue
        scope = raw_scope.lower()
        scope_seen = seen.setdefault(scope, set())
        for raw_title in raw_titles:
            title = metadata_text(raw_title)
            if title is None or title in scope_seen:
                continue
            scope_seen.add(title)
            normalized.setdefault(scope, []).append(title)
            alias_count += 1
            if alias_count >= _MAX_ALIASES:
                break
    return normalized
