"""Shared current Prometheus snapshot for private operator surfaces."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from prometheus_client.parser import text_string_to_metric_families

from comet.core.models import settings
from comet.observability.metrics import metrics, render_metrics
from comet.usenet.engine_client import EngineClient
from comet.usenet.engine_transport import EngineUnavailable

_REFRESH_SECONDS = 1.0
_engine_lock = asyncio.Lock()
_engine_refreshed_at = float("-inf")
_sample_lock = asyncio.Lock()
_samples_refreshed_at = float("-inf")
_samples: tuple[MetricSample, ...] = ()


@dataclass(frozen=True, slots=True)
class MetricSample:
    name: str
    labels: dict[str, str]
    value: float


async def current_metric_samples() -> tuple[MetricSample, ...]:
    global _samples_refreshed_at, _samples
    loop = asyncio.get_running_loop()
    if loop.time() - _samples_refreshed_at < _REFRESH_SECONDS:
        return _samples
    async with _sample_lock:
        if loop.time() - _samples_refreshed_at < _REFRESH_SECONDS:
            return _samples
        await refresh_usenet_metrics()
        _samples = tuple(
            MetricSample(sample.name, sample.labels, float(sample.value))
            for family in text_string_to_metric_families(render_metrics().decode())
            for sample in family.samples
            if sample.name.startswith("comet_")
        )
        _samples_refreshed_at = loop.time()
        return _samples


async def refresh_usenet_metrics() -> None:
    global _engine_refreshed_at
    if not settings.USENET_ENABLED:
        return
    loop = asyncio.get_running_loop()
    if loop.time() - _engine_refreshed_at < _REFRESH_SECONDS:
        return
    async with _engine_lock:
        if loop.time() - _engine_refreshed_at < _REFRESH_SECONDS:
            return
        try:
            async with asyncio.timeout(1):
                snapshot = await EngineClient(
                    Path(settings.USENET_RUNTIME_DIR) / "engine.json"
                ).stats()
        except (EngineUnavailable, TimeoutError):
            metrics.set_usenet_engine_stats(None)
        else:
            metrics.set_usenet_engine_stats(snapshot)
        _engine_refreshed_at = loop.time()


__all__ = ("MetricSample", "current_metric_samples", "refresh_usenet_metrics")
