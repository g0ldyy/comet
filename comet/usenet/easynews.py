"""Account-bound Easynews primitives shared by discovery and playback."""

import base64
from urllib.parse import urlencode

import aiohttp

from comet.core.provider_json import is_success_status
from comet.usenet.limits import MAX_NZB_DOCUMENT_BYTES
from comet.utils.http_client import read_bounded_body

GENERATE_NZB_URL = "https://members.easynews.com/2.0/api/dl-nzb"
_GENERATE_TIMEOUT = aiohttp.ClientTimeout(
    total=120,
    connect=5,
    sock_read=30,
)


class EasynewsNzbError(RuntimeError):
    """A safe generated-NZB failure with account/retry semantics."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        retry_after: int | None = None,
        auth_failed: bool = False,
    ):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after
        self.auth_failed = auth_failed


def _bounded_utf8(
    value: object,
    maximum_characters: int,
    label: str,
    *,
    forbid_controls: bool = False,
) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Easynews {label} is invalid")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"Easynews {label} is invalid") from exc
    if len(value) > maximum_characters or (
        forbid_controls
        and any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"Easynews {label} is invalid")
    return encoded


def credential(value: object) -> str:
    _bounded_utf8(value, 512, "credential", forbid_controls=True)
    return value


def authorization_header(username: object, password: object) -> dict[str, str]:
    normalized_username = credential(username)
    normalized_password = credential(password)
    token = base64.b64encode(
        f"{normalized_username}:{normalized_password}".encode()
    ).decode()
    return {"Authorization": f"Basic {token}"}


def generated_nzb_form(payload: object) -> bytes:
    """Encode the exact closed Easynews single-item form."""
    if not isinstance(payload, dict):
        raise ValueError("Easynews locator is invalid")
    content_hash = payload.get("hash")
    filename = payload.get("filename")
    extension = payload.get("extension")
    signature = payload.get("signature")
    _bounded_utf8(content_hash, 256, "hash")
    encoded_filename = (
        base64.b64encode(_bounded_utf8(filename, 512, "filename")).decode().rstrip("=")
    )
    encoded_extension = (
        base64.b64encode(_bounded_utf8(extension, 32, "extension")).decode().rstrip("=")
    )
    if signature is not None:
        _bounded_utf8(signature, 512, "signature")
    item_field = "0" if signature is None else f"0&sig={signature}"
    return urlencode(
        (
            ("autoNZB", "1"),
            (
                item_field,
                f"{content_hash}|{encoded_filename}:{encoded_extension}",
            ),
        )
    ).encode("ascii")


async def generate_nzb(
    session,
    payload: dict,
    username: str,
    password: str,
) -> bytes:
    """Generate and bound one NZB from its exact account-owned search row."""
    form = generated_nzb_form(payload)
    headers = {
        **authorization_header(username, password),
        "Accept": "application/x-nzb, */*",
        "Accept-Encoding": "identity",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    try:
        async with session.post(
            GENERATE_NZB_URL,
            data=form,
            headers=headers,
            allow_redirects=False,
            timeout=_GENERATE_TIMEOUT,
        ) as response:
            if response.status in {401, 403}:
                raise EasynewsNzbError(
                    "easynews_auth_failed",
                    auth_failed=True,
                )
            if response.status == 429:
                raise EasynewsNzbError(
                    "easynews_rate_limited",
                    retryable=True,
                    retry_after=bounded_retry_after(
                        response.headers.get("Retry-After")
                    ),
                )
            if response.status >= 500:
                raise EasynewsNzbError(
                    "easynews_generate_unavailable",
                    retryable=True,
                )
            if not is_success_status(response.status):
                raise EasynewsNzbError("easynews_generate_rejected")
            try:
                document = await read_bounded_body(
                    response,
                    MAX_NZB_DOCUMENT_BYTES,
                )
            except ValueError as exc:
                raise EasynewsNzbError("easynews_generate_too_large") from exc
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise EasynewsNzbError(
            "easynews_generate_unavailable",
            retryable=True,
        ) from exc
    return document


def bounded_retry_after(value: object) -> int | None:
    """Parse one integer provider delay into the fixed retry window."""
    if (
        not isinstance(value, str)
        or not value
        or not value.isdigit()
        or len(value) > 10
    ):
        return None
    parsed = int(value)
    return min(max(parsed, 1), 300)
