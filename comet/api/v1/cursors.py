import base64
import math

import orjson

from comet.api.v1.responses import ApiProblem


def encode_timestamp_cursor(timestamp: float, identifier: str) -> str:
    return (
        base64.urlsafe_b64encode(orjson.dumps([timestamp, identifier]))
        .decode("ascii")
        .rstrip("=")
    )


def decode_timestamp_cursor(cursor: str, *, subject: str) -> tuple[float, str]:
    try:
        value = orjson.loads(
            base64.b64decode(
                cursor + "=" * (-len(cursor) % 4),
                altchars=b"-_",
                validate=True,
            )
        )
    except (TypeError, ValueError, orjson.JSONDecodeError):
        value = None
    if (
        not isinstance(value, list)
        or len(value) != 2
        or isinstance(value[0], bool)
        or not isinstance(value[0], (int, float))
        or not math.isfinite(value[0])
        or value[0] < 0
        or not isinstance(value[1], str)
        or not 1 <= len(value[1].encode("utf-8")) <= 128
        or any(ord(character) < 32 or ord(character) == 127 for character in value[1])
    ):
        raise ApiProblem(
            status_code=422,
            code="invalid_cursor",
            message=f"The {subject} cursor is invalid.",
        )
    return float(value[0]), value[1]
