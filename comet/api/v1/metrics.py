"""Authenticated current operational metrics."""

from __future__ import annotations

import math
import time
from typing import Annotated, Literal

import aiohttp
import orjson
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, field_validator

from comet.api.v1.contracts import (
    ApiSuccess,
    CurrentMetricsData,
    MetricPointData,
    MetricRangeData,
    MetricSampleData,
    MetricSeriesData,
)
from comet.api.v1.responses import ApiProblem, success_response
from comet.api.v1.security import require_admin_session
from comet.core.models import database, settings
from comet.observability.database_metrics import (
    DatabaseMetricsSnapshot,
    current_database_metrics,
)
from comet.observability.metric_snapshot import current_metric_samples
from comet.utils.http_client import http_client_manager, read_bounded_body

router = APIRouter(prefix="/admin/metrics", tags=["API v1 Metrics"])
MetricRange = Literal["15m", "1h", "6h", "24h", "7d", "30d"]
MetricName = Literal[
    "background_oldest",
    "background_queue",
    "background_runs",
    "background_torrents",
    "cache_hit_ratio",
    "cache_results",
    "database_errors",
    "database_p95",
    "debrid_p95",
    "debrid_requests",
    "debrid_results",
    "http_error_ratio",
    "http_in_flight",
    "http_p95",
    "http_requests",
    "http_response_size_p95",
    "proxy_active",
    "proxy_bytes",
    "proxy_connections",
    "proxy_p95",
    "replica_fallbacks",
    "scraper_p95",
    "scraper_requests",
    "scraper_results",
    "search_rejections",
    "stream_requests",
    "stream_results",
    "usenet_engine_up",
    "usenet_nntp_bytes",
    "usenet_snapshot_age",
]
_RANGE_SECONDS: dict[MetricRange, int] = {
    "15m": 15 * 60,
    "1h": 60 * 60,
    "6h": 6 * 60 * 60,
    "24h": 24 * 60 * 60,
    "7d": 7 * 24 * 60 * 60,
    "30d": 30 * 24 * 60 * 60,
}
_QUERIES: dict[MetricName, str] = {
    "background_oldest": "max(comet_background_scraper_oldest_queue_item_age_seconds)",
    "background_queue": "sum(comet_background_scraper_queue_items) by (kind)",
    "background_runs": (
        "sum(rate(comet_background_scraper_runs_total[5m])) by (status)"
    ),
    "background_torrents": ("sum(rate(comet_background_scraper_torrents_total[5m]))"),
    "cache_hit_ratio": (
        'sum(rate(comet_torrent_cache_lookups_total{result="hit"}[5m])) / '
        "clamp_min(sum(rate(comet_torrent_cache_lookups_total[5m])), 1)"
    ),
    "cache_results": (
        "histogram_quantile(0.95, "
        "sum(rate(comet_torrent_cache_results_bucket[5m])) by (le))"
    ),
    "database_errors": (
        'sum(rate(comet_database_operations_total{outcome="error"}[5m]))'
    ),
    "database_p95": (
        "histogram_quantile(0.95, "
        "sum(rate(comet_database_operation_duration_seconds_bucket[5m])) by (le))"
    ),
    "debrid_p95": (
        "histogram_quantile(0.95, "
        "sum(rate(comet_debrid_request_duration_seconds_bucket[5m])) by (le))"
    ),
    "debrid_requests": "sum(rate(comet_debrid_requests_total[5m])) by (service, outcome)",
    "debrid_results": (
        "sum(rate(comet_debrid_results_total[5m])) by (service, operation)"
    ),
    "http_error_ratio": (
        'sum(rate(comet_http_requests_total{status=~"5.."}[5m])) / '
        "clamp_min(sum(rate(comet_http_requests_total[5m])), 1)"
    ),
    "http_in_flight": "sum(comet_http_requests_in_progress)",
    "http_p95": (
        "histogram_quantile(0.95, "
        "sum(rate(comet_http_request_duration_seconds_bucket[5m])) by (le))"
    ),
    "http_requests": "sum(rate(comet_http_requests_total[5m]))",
    "http_response_size_p95": (
        "histogram_quantile(0.95, "
        "sum(rate(comet_http_response_size_bytes_bucket[5m])) by (le))"
    ),
    "proxy_active": "sum(comet_proxy_stream_active_connections)",
    "proxy_bytes": "sum(rate(comet_proxy_stream_bytes_total[5m]))",
    "proxy_connections": "sum(rate(comet_proxy_stream_connections_total[5m]))",
    "proxy_p95": (
        "histogram_quantile(0.95, "
        "sum(rate(comet_proxy_stream_duration_seconds_bucket[5m])) by (le))"
    ),
    "replica_fallbacks": "sum(rate(comet_database_replica_fallbacks_total[5m]))",
    "scraper_p95": (
        "histogram_quantile(0.95, "
        "sum(rate(comet_scraper_request_duration_seconds_bucket[5m])) by (le))"
    ),
    "scraper_requests": "sum(rate(comet_scraper_requests_total[5m])) by (scraper, outcome)",
    "scraper_results": (
        "sum(rate(comet_scraper_torrents_total[5m])) by (scraper, context)"
    ),
    "search_rejections": "sum(rate(comet_search_rejections_total[5m])) by (reason)",
    "stream_requests": "sum(rate(comet_stream_requests_total[5m])) by (outcome)",
    "stream_results": (
        "histogram_quantile(0.95, sum(rate(comet_stream_results_bucket[5m])) by (le))"
    ),
    "usenet_engine_up": "min(comet_usenet_engine_up)",
    "usenet_nntp_bytes": (
        'sum(rate(comet_usenet_engine_stat{stat=~".*bytes.*"}[5m])) by (stat)'
    ),
    "usenet_snapshot_age": (
        "clamp_min(time() - "
        "max(comet_usenet_engine_last_snapshot_timestamp_seconds), 0)"
    ),
}
_MAX_QUERY_RESPONSE_BYTES = 2 * 1024 * 1024


class _PrometheusSeries(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")

    metric: dict[str, str]
    values: list[list[float | int | str]]

    @field_validator("values")
    @classmethod
    def validate_points(cls, values):
        if any(len(point) != 2 for point in values):
            raise ValueError("Prometheus matrix points must contain time and value")
        return values


class _PrometheusData(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")

    resultType: Literal["matrix"]
    result: list[_PrometheusSeries]


class _PrometheusResponse(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")

    status: Literal["success"]
    data: _PrometheusData


@router.get(
    "/current",
    response_model=ApiSuccess[CurrentMetricsData],
)
async def current_metrics(
    request: Request,
    _session: Annotated[str, Depends(require_admin_session)],
):
    samples = await current_metric_samples()
    return success_response(
        request,
        CurrentMetricsData(
            collected_at=time.time(),
            history_available=settings.PROMETHEUS_QUERY_URL is not None,
            history_ranges=list(_RANGE_SECONDS),
            samples=[
                MetricSampleData(
                    name=sample.name,
                    labels=sample.labels,
                    value=sample.value,
                )
                for sample in samples
            ],
        ),
    )


@router.get(
    "/database",
    response_model=ApiSuccess[DatabaseMetricsSnapshot],
)
async def database_metrics(
    request: Request,
    _session: Annotated[str, Depends(require_admin_session)],
):
    snapshot = await current_database_metrics(
        database,
        cache_ttl=settings.METRICS_CACHE_TTL,
        debrid_cache_ttl=settings.DEBRID_CACHE_TTL,
    )
    return success_response(request, snapshot)


@router.get(
    "/range/{metric}",
    response_model=ApiSuccess[MetricRangeData],
)
async def range_metrics(
    request: Request,
    _session: Annotated[str, Depends(require_admin_session)],
    metric: MetricName,
    range: MetricRange = "1h",
):
    if settings.PROMETHEUS_QUERY_URL is None:
        raise ApiProblem(
            status_code=404,
            code="metrics_history_unavailable",
            message="Historical metrics are not configured.",
        )
    duration = _RANGE_SECONDS[range]
    step = max(5, duration // 240)
    ended_at = time.time()
    headers = (
        {"Authorization": f"Bearer {settings.PROMETHEUS_QUERY_TOKEN}"}
        if settings.PROMETHEUS_QUERY_TOKEN
        else None
    )
    session = await http_client_manager.get_session()
    try:
        async with session.get(
            f"{settings.PROMETHEUS_QUERY_URL.rstrip('/')}/api/v1/query_range",
            headers=headers,
            params={
                "query": _QUERIES[metric],
                "start": ended_at - duration,
                "end": ended_at,
                "step": step,
            },
        ) as response:
            if response.status != 200:
                raise ApiProblem(
                    status_code=503,
                    code="metrics_history_failed",
                    message="Historical metrics could not be queried.",
                )
            document = _PrometheusResponse.model_validate(
                orjson.loads(
                    await read_bounded_body(response, _MAX_QUERY_RESPONSE_BYTES)
                )
            )
            series = [
                MetricSeriesData(
                    labels=item.metric,
                    points=[
                        MetricPointData(
                            timestamp=float(timestamp),
                            value=(
                                value
                                if math.isfinite(value := float(raw_value))
                                else None
                            ),
                        )
                        for timestamp, raw_value in item.values
                    ],
                )
                for item in document.data.result
            ]
    except ApiProblem:
        raise
    except (
        aiohttp.ClientError,
        TimeoutError,
        ValueError,
    ):
        raise ApiProblem(
            status_code=503,
            code="metrics_history_failed",
            message="Historical metrics could not be queried.",
        ) from None
    return success_response(
        request,
        MetricRangeData(
            metric=metric,
            range=range,
            step=step,
            series=series,
        ),
    )
