from urllib.parse import urlsplit

from comet.observability import log
from comet.usenet.outbound import OutboundUrlError, fetch_http_bytes

trackers = []
_TRACKERS_URL = (
    "https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt"
)
_MAX_TRACKER_DOCUMENT_BYTES = 64 * 1024
_MAX_TRACKERS = 1_024
_MAX_TRACKER_URL_BYTES = 2_048


class InvalidTrackerDocument(ValueError):
    pass


def _decode_trackers(document: bytes) -> list[str]:
    decoded = []
    seen = set()
    for encoded_line in document.splitlines():
        try:
            raw_line = encoded_line.decode("utf-8")
        except UnicodeDecodeError:
            continue
        value = raw_line.strip()
        if not value or value.startswith("#"):
            continue
        try:
            parsed = urlsplit(value)
            _ = parsed.port
        except ValueError:
            continue
        if (
            len(value.encode("utf-8")) > _MAX_TRACKER_URL_BYTES
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
            or parsed.scheme.casefold() not in {"http", "https", "udp"}
            or not parsed.hostname
        ):
            continue
        if value in seen:
            continue
        seen.add(value)
        decoded.append(value)
        if len(decoded) == _MAX_TRACKERS:
            break
    if not decoded:
        raise InvalidTrackerDocument("tracker document has no usable entries")
    return decoded


async def download_best_trackers():
    try:
        document = await fetch_http_bytes(
            _TRACKERS_URL,
            max_bytes=_MAX_TRACKER_DOCUMENT_BYTES,
            headers={"Accept": "text/plain"},
            redirects=1,
        )
        downloaded = _decode_trackers(document)

        trackers[:] = downloaded
    except (OutboundUrlError, InvalidTrackerDocument) as exc:
        log.warning(
            "trackers.download.failed",
            "Tracker download failed",
            provider_name="trackerslist",
            operation="download",
            error_code="dependency_warning",
            exc=exc,
        )
