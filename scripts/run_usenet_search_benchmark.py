#!/usr/bin/env python3
"""Reproducible SearchCoordinator fan-out regression gate."""

import asyncio
import json
import os
import platform
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from comet.core.capabilities import (
    CapabilityPlan,
    EligibleDiscovery,
    EligibleProvider,
)
from comet.core.sources import (
    LocatorKind,
    LocatorPolicy,
    RealNzbRef,
    ReleaseCandidate,
    ReleaseScope,
    TransportKind,
)
from comet.discovery.manager import SearchCoordinator
from comet.discovery.models import DiscoveryBatch, MediaQuery
from comet.observability.logging import LoggingSettings, LogProfile, configure

BASELINES_PATH = (
    ROOT / "native" / "usenet-engine" / "benchmarks" / "usenet-baselines.json"
)
FAN_OUT = 16
REQUESTS_PER_SAMPLE = 200


class Adapter:
    def __init__(self, candidate: ReleaseCandidate):
        self._batch = DiscoveryBatch(
            candidates=(candidate,),
            coverage=frozenset({"usenet"}),
        )

    async def search(self, _query, _context):
        await asyncio.sleep(0)
        return self._batch


def _candidate(index: int) -> ReleaseCandidate:
    configuration_id = f"source-{index}"
    return ReleaseCandidate(
        candidate_id=f"candidate-{index}",
        media_id="tt1234567",
        scope=ReleaseScope.MOVIE,
        transport=TransportKind.USENET,
        title=f"Benchmark.Release.{index}",
        locators=(
            RealNzbRef(
                locator_id=f"locator-{index}",
                kind=LocatorKind.REAL_NZB,
                policy=LocatorPolicy(frozenset({"stremio_nntp"})),
                adapter_configuration_id=configuration_id,
                remote_guid=f"guid-{index}",
            ),
        ),
    )


def _architecture() -> str:
    machine = platform.machine().lower()
    if machine in {"amd64", "x86_64"}:
        return "x86_64"
    if machine in {"arm64", "aarch64"}:
        return "aarch64"
    raise RuntimeError(f"unsupported benchmark architecture: {machine}")


async def _run() -> None:
    # Isolate coordinator fan-out from the independently tested log serializer.
    configure(
        LoggingSettings(_env_file=None, LOG_PROFILE=LogProfile.QUIET),
        process_role="cli",
    )
    baseline_document = json.loads(BASELINES_PATH.read_text())
    assert baseline_document["version"] == 1
    regression_threshold = baseline_document["regressionThresholdPercent"]
    warmups = baseline_document["warmupSamples"]
    sample_count = baseline_document["measuredSamples"]
    architecture = _architecture()
    baseline = baseline_document["architectures"][architecture]["search_fan_out"]
    assert baseline["unit"] == "request"

    candidates = [_candidate(index) for index in range(FAN_OUT)]
    coordinator = SearchCoordinator(
        {
            f"source-{index}": Adapter(candidate)
            for index, candidate in enumerate(candidates)
        }
    )
    plan = CapabilityPlan(
        transports=frozenset({TransportKind.USENET}),
        discovery_source_ids=tuple(f"source-{index}" for index in range(FAN_OUT)),
        providers=(EligibleProvider("stremio", "stremio_nntp", 0),),
        diagnostics=(),
        discovery=tuple(
            EligibleDiscovery(
                f"source-{index}",
                frozenset({TransportKind.USENET}),
            )
            for index in range(FAN_OUT)
        ),
    )
    query = MediaQuery("tt1234567", "movie", title="Benchmark Release", year=2026)

    async def sample() -> int:
        started = time.perf_counter_ns()
        for _ in range(REQUESTS_PER_SAMPLE):
            result = await coordinator.search(query, plan)
            assert len(result.candidates) == FAN_OUT
            assert not result.diagnostics
            assert result.capability_plan is plan
        return (time.perf_counter_ns() - started) // REQUESTS_PER_SAMPLE

    for _ in range(warmups):
        await sample()
    samples = sorted([await sample() for _ in range(sample_count)])
    p95 = samples[(len(samples) * 95 + 99) // 100 - 1]
    median = samples[len(samples) // 2]
    baseline_ns = baseline["baselineNsPerUnit"]
    maximum = baseline_ns * (100 + regression_threshold) // 100
    print(
        "USENET_BENCH"
        f" name=search_fan_out arch={architecture} unit=request"
        f" median_ns={median} p95_ns={p95}"
        f" baseline_ns={baseline_ns} maximum_ns={maximum}"
    )
    if "USENET_BENCH_REPORT_ONLY" not in os.environ:
        assert p95 <= maximum, (
            f"search_fan_out p95 {p95} ns/request exceeds "
            f"the {regression_threshold}% regression ceiling {maximum}"
        )
    assert p95 < 250_000_000, "hot synthetic fan-out exceeds the 250 ms target"


if __name__ == "__main__":
    asyncio.run(_run())
