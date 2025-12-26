from fastapi import Response
from prometheus_client import (CONTENT_TYPE_LATEST, CollectorRegistry, Counter,
                               generate_latest)

# Dedicated registry so we only expose Comet-specific metrics
_registry = CollectorRegistry()

_stream_requests_total = Counter(
    "comet_stream_requests_total",
    "Total number of stream requests grouped by debrid service",
    ["debrid_service"],
    registry=_registry,
)

_non_debrid_stream_requests_total = Counter(
    "comet_stream_requests_non_debrid_total",
    "Total number of stream requests that do not require a user API key",
    registry=_registry,
)


def record_stream_request(debrid_service: str | None):
    """Increment the stream request counter for the provided service."""
    label = (debrid_service or "unknown").lower()
    _stream_requests_total.labels(debrid_service=label).inc()


def record_non_debrid_stream_request():
    """Increment the counter tracking torrent (non-API) stream requests."""
    _non_debrid_stream_requests_total.inc()


def prom_response() -> Response:
    """Return a Response containing the current Prometheus metrics payload."""
    payload = generate_latest(_registry)
    return Response(content=payload, media_type=CONTENT_TYPE_LATEST)
