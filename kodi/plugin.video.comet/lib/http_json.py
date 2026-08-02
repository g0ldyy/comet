import json
import re
from urllib import parse

_PATH_SEGMENT = re.compile(r"[A-Za-z0-9._~-]+", re.ASCII)


class JsonHttpError(ValueError):
    pass


def validate_http_url(url: str, *, base_only: bool = False):
    if not isinstance(url, str):
        raise JsonHttpError("invalid URL")
    normalized = url.strip()
    try:
        normalized.encode("utf-8")
    except UnicodeError as exc:
        raise JsonHttpError("invalid URL") from exc
    if not normalized or any(
        ord(character) < 32 or ord(character) == 127 for character in normalized
    ):
        raise JsonHttpError("invalid URL")

    try:
        parsed = parse.urlsplit(normalized)
        port = parsed.port
        hostname = parsed.hostname
    except ValueError as exc:
        raise JsonHttpError("invalid URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (base_only and parsed.query)
    ):
        raise JsonHttpError("invalid URL")
    try:
        hostname.encode("ascii")
    except UnicodeError as exc:
        raise JsonHttpError("invalid URL") from exc
    if port is not None and not 1 <= port <= 65535:
        raise JsonHttpError("invalid URL")
    return normalized.rstrip("/") if base_only else normalized


def normalize_api_prefix(value: str):
    if not isinstance(value, str):
        raise JsonHttpError("invalid API prefix")
    if value and not value.strip():
        raise JsonHttpError("invalid API prefix")
    normalized = value.strip().strip("/")
    try:
        normalized.encode("ascii")
    except UnicodeError as exc:
        raise JsonHttpError("invalid API prefix") from exc
    if not normalized:
        return ""
    segments = normalized.split("/")
    if any(
        segment in {".", ".."} or _PATH_SEGMENT.fullmatch(segment) is None
        for segment in segments
    ):
        raise JsonHttpError("invalid API prefix")
    return normalized


def origin_label(url: str):
    try:
        parsed = parse.urlsplit(validate_http_url(url))
        hostname = parsed.hostname
        port = parsed.port
    except JsonHttpError:
        return "configured service"
    if ":" in hostname:
        hostname = f"[{hostname}]"
    return f"{hostname}:{port}" if port is not None else hostname


def response_status(error):
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    return status if type(status) is int and 100 <= status <= 599 else None


def _decode_json_response(response):
    raw = getattr(response, "raw", None)
    if raw is None or not hasattr(raw, "read"):
        raise JsonHttpError("invalid response body")
    try:
        payload = raw.read(decode_content=True)
    except Exception as exc:
        raise JsonHttpError("response read failed") from exc
    if not isinstance(payload, bytes):
        raise JsonHttpError("invalid response body")
    try:
        decoded = json.loads(
            payload.decode("utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"invalid constant {value}")
            ),
        )
    except (UnicodeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
        raise JsonHttpError("invalid JSON response") from exc
    if not isinstance(decoded, dict):
        raise JsonHttpError("invalid JSON response")
    return decoded


def request_json(session, method: str, url: str, *, timeout: int, payload=None):
    target = validate_http_url(url)
    if method not in {"GET", "POST"}:
        raise JsonHttpError("unsupported HTTP method")
    request = session.get if method == "GET" else session.post
    kwargs = {
        "timeout": timeout,
        "allow_redirects": False,
        "stream": True,
        "headers": {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        },
    }
    if method == "POST":
        kwargs["json"] = payload

    response = request(target, **kwargs)
    try:
        status = getattr(response, "status_code", None)
        if type(status) is not int or not 200 <= status <= 299:
            response.raise_for_status()
            raise JsonHttpError("unexpected HTTP status")
        return _decode_json_response(response)
    finally:
        response.close()
