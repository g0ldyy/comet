"""Closed HTTP boundary for fixed-origin metadata services."""

from dataclasses import dataclass

import aiohttp

from comet.core.provider_json import (
    ProviderJsonError,
    is_success_status,
    read_provider_json,
)

_METADATA_JSON_MAX_BYTES = 2 * 1024 * 1024
_METADATA_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=5, sock_read=10)
_BASE_HEADERS = {
    "Accept": "application/json",
    "Accept-Encoding": "identity",
}


class MetadataHttpError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MetadataHttpResponse:
    status: int
    payload: dict | None

    @property
    def successful(self) -> bool:
        return is_success_status(self.status)


async def get_metadata_json(
    session: aiohttp.ClientSession,
    url: str,
    *,
    headers: dict[str, str] | None = None,
) -> MetadataHttpResponse:
    """Fetch one bounded JSON object without following redirects."""
    request_headers = {}
    if headers:
        request_headers.update(headers)
    request_headers.update(_BASE_HEADERS)
    try:
        async with session.get(
            url,
            headers=request_headers,
            timeout=_METADATA_TIMEOUT,
            allow_redirects=False,
        ) as response:
            status = response.status
            if type(status) is not int or not 100 <= status <= 599:
                raise MetadataHttpError("metadata service returned an invalid status")
            if not is_success_status(status):
                return MetadataHttpResponse(status, None)
            try:
                payload = await read_provider_json(
                    response,
                    maximum=_METADATA_JSON_MAX_BYTES,
                )
            except ProviderJsonError:
                raise MetadataHttpError(
                    "metadata service returned an invalid response"
                ) from None
            return MetadataHttpResponse(status, payload)
    except MetadataHttpError:
        raise
    except (TimeoutError, aiohttp.ClientError):
        raise MetadataHttpError("metadata service request failed") from None
