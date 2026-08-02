import re

_NON_ALNUM_PATTERN = re.compile(r"[^A-Za-z0-9]+")
_MULTI_UNDERSCORE_PATTERN = re.compile(r"_+")
_MAX_STATUS_KEY_LENGTH = 128


def normalize_status_key(status_key: str | None) -> str | None:
    if (
        type(status_key) is not str
        or not status_key
        or len(status_key) > _MAX_STATUS_KEY_LENGTH
    ):
        return None
    normalized = _NON_ALNUM_PATTERN.sub("_", status_key.strip()).strip("_").upper()
    normalized = _MULTI_UNDERSCORE_PATTERN.sub("_", normalized)
    return normalized or None
