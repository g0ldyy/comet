"""Pure validation for AltMount stream representations and durable state."""

import re
from email.utils import parsedate_to_datetime


def valid_altmount_virtual_path(value: object) -> bool:
    if not isinstance(value, str) or not value or value.startswith("/"):
        return False
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return (
        len(encoded) <= 2048
        and "\\" not in value
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


def valid_altmount_strong_etag(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        encoded = value.encode("latin-1")
    except UnicodeEncodeError:
        return False
    return (
        len(encoded) <= 256
        and re.fullmatch(r'"[\x21\x23-\x7e\x80-\xff]*"', value) is not None
    )


def valid_altmount_last_modified(value: object) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or not value.isascii()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return False
    try:
        return parsedate_to_datetime(value).tzinfo is not None
    except (TypeError, ValueError, OverflowError):
        return False
