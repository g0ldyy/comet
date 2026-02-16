import re

_NON_ALNUM_PATTERN = re.compile(r"[^A-Za-z0-9]+")
_MULTI_UNDERSCORE_PATTERN = re.compile(r"_+")


def normalize_status_key(status_key: str | None) -> str | None:
    if not status_key:
        return None
    normalized = _NON_ALNUM_PATTERN.sub("_", str(status_key).strip()).strip("_").upper()
    normalized = _MULTI_UNDERSCORE_PATTERN.sub("_", normalized)
    return normalized or None
