"""Bounded JSON response primitives shared by external providers."""

import json

from comet.utils.http_client import read_bounded_body

MAX_PROVIDER_JSON_BYTES = 2 * 1024 * 1024


class ProviderJsonError(ValueError):
    pass


def is_success_status(status: int) -> bool:
    return 200 <= status < 300


def _reject_constant(_value):
    raise ProviderJsonError("invalid JSON constant")


def _maximum(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_PROVIDER_JSON_BYTES
    ):
        raise ValueError("provider JSON limit is invalid")
    return value


async def read_provider_body(
    response,
    *,
    maximum: int = MAX_PROVIDER_JSON_BYTES,
) -> bytes:
    """Read one bounded provider body."""
    maximum = _maximum(maximum)
    try:
        return await read_bounded_body(response, maximum)
    except ValueError as exc:
        raise ProviderJsonError(str(exc)) from exc


def decode_provider_json(
    body: bytes,
    *,
    maximum: int = MAX_PROVIDER_JSON_BYTES,
) -> dict:
    """Decode one already-read bounded JSON object."""
    maximum = _maximum(maximum)
    if not isinstance(body, bytes) or not body or len(body) > maximum:
        raise ProviderJsonError("invalid provider body")
    try:
        text = body.decode("utf-8")
        payload = json.loads(
            text,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ProviderJsonError("invalid provider JSON") from exc
    if not isinstance(payload, dict):
        raise ProviderJsonError("invalid provider JSON object")
    return payload


def decode_provider_data(
    body: bytes,
    *,
    maximum: int = MAX_PROVIDER_JSON_BYTES,
) -> dict:
    """Decode a JSON envelope containing a named data object."""
    payload = decode_provider_json(body, maximum=maximum)
    if not isinstance(payload.get("data"), dict):
        raise ProviderJsonError("invalid provider envelope")
    return payload["data"]


async def read_provider_json(
    response,
    *,
    maximum: int = MAX_PROVIDER_JSON_BYTES,
) -> dict:
    """Read and decode one bounded JSON object."""
    return decode_provider_json(
        await read_provider_body(response, maximum=maximum),
        maximum=maximum,
    )


async def read_provider_data(
    response,
    *,
    maximum: int = MAX_PROVIDER_JSON_BYTES,
) -> dict:
    """Read one JSON envelope containing a named data object."""
    return decode_provider_data(
        await read_provider_body(response, maximum=maximum),
        maximum=maximum,
    )
