"""Pure readiness evaluation and bounded process-local transition reporting."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from comet.observability.logging import log
from comet.observability.metrics import metrics

ReadinessState = Literal["ready", "degraded", "unavailable"]


@dataclass(frozen=True, slots=True)
class ReadinessSnapshot:
    state: ReadinessState
    components: dict[str, str]

    @property
    def status_code(self) -> int:
        return 503 if self.state == "unavailable" else 200

    @property
    def component_details(self) -> str:
        return " ".join(
            f"{component}={status}" for component, status in self.components.items()
        )


def evaluate_readiness(
    *,
    worker_ready: bool,
    database_ready: bool,
    schema_current: bool,
    usenet_enabled: bool,
    artifact_storage_ready: bool | None,
    engine_ready: bool | None,
    engine_required: bool,
) -> ReadinessSnapshot:
    """Map component facts to the response, metrics and transition state."""

    components = {
        "worker": "ready" if worker_ready else "unavailable",
        "database": "ready" if database_ready else "unavailable",
        "schema": "current" if schema_current else "unavailable",
        "artifact_storage": "disabled",
        "usenet_engine": "disabled",
    }
    unavailable = not worker_ready or not database_ready or not schema_current
    degraded = False
    if usenet_enabled:
        storage_ready = artifact_storage_ready is True
        components["artifact_storage"] = "ready" if storage_ready else "unavailable"
        unavailable = unavailable or not storage_ready
        if engine_ready is True:
            components["usenet_engine"] = "ready"
        elif engine_required:
            components["usenet_engine"] = "required_unavailable"
            unavailable = True
        else:
            components["usenet_engine"] = "degraded"
            degraded = True
    state: ReadinessState
    if unavailable:
        state = "unavailable"
    elif degraded:
        state = "degraded"
    else:
        state = "ready"
    return ReadinessSnapshot(state=state, components=components)


class ReadinessTransitionTracker:
    """Emit state changes once and reminders no more often than every 15 min."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        reminder_seconds: float = 900.0,
    ) -> None:
        self._clock = clock
        self._reminder_seconds = reminder_seconds
        self._state: ReadinessState = "ready"
        self._changed_at = clock()
        self._last_emitted_at = self._changed_at
        self._suppressed_count = 0

    def observe(self, snapshot: ReadinessSnapshot) -> None:
        metrics.set_readiness(snapshot.state)
        now = self._clock()
        observed = snapshot.state
        if observed == "ready":
            if self._state != "ready":
                duration_ms = (now - self._changed_at) * 1000
                suppressed_count = self._suppressed_count
                self._state = "ready"
                self._changed_at = now
                self._last_emitted_at = now
                self._suppressed_count = 0
                log.info(
                    "readiness.recovered",
                    "Application readiness recovered",
                    duration_ms=duration_ms,
                    suppressed_count=suppressed_count,
                )
            return

        if observed != self._state:
            suppressed_count = self._suppressed_count
            self._state = observed
            self._changed_at = now
            self._last_emitted_at = now
            self._suppressed_count = 0
            if observed == "degraded":
                log.warning(
                    "readiness.degraded",
                    "Application readiness is degraded",
                    error_code="readiness_degraded",
                    details=snapshot.component_details,
                    suppressed_count=suppressed_count,
                )
            else:
                log.error(
                    "readiness.unavailable",
                    "Application is not ready",
                    error_code="readiness_unavailable",
                    details=snapshot.component_details,
                    suppressed_count=suppressed_count,
                )
            return

        self._suppressed_count += 1
        if now - self._last_emitted_at < self._reminder_seconds:
            return
        suppressed_count = self._suppressed_count
        self._suppressed_count = 0
        self._last_emitted_at = now
        if observed == "degraded":
            log.warning(
                "readiness.degraded",
                "Application readiness is degraded",
                error_code="readiness_degraded",
                details=snapshot.component_details,
                suppressed_count=suppressed_count,
            )
        else:
            log.error(
                "readiness.unavailable",
                "Application is not ready",
                error_code="readiness_unavailable",
                details=snapshot.component_details,
                suppressed_count=suppressed_count,
            )

    def reset(self) -> None:
        """Reset inherited state for a freshly started worker lifecycle."""
        now = self._clock()
        self._state = "ready"
        self._changed_at = now
        self._last_emitted_at = now
        self._suppressed_count = 0


readiness_tracker = ReadinessTransitionTracker()


__all__ = (
    "ReadinessSnapshot",
    "ReadinessTransitionTracker",
    "evaluate_readiness",
    "readiness_tracker",
)
