import unittest
from unittest.mock import patch

from comet.observability.readiness import (
    ReadinessSnapshot,
    ReadinessTransitionTracker,
    evaluate_readiness,
)


class ReadinessEvaluationTests(unittest.TestCase):
    def test_required_and_optional_native_engine_have_distinct_states(self):
        common = {
            "worker_ready": True,
            "database_ready": True,
            "schema_current": True,
            "usenet_enabled": True,
            "artifact_storage_ready": True,
            "engine_ready": False,
        }
        optional = evaluate_readiness(**common, engine_required=False)
        required = evaluate_readiness(**common, engine_required=True)

        self.assertEqual((optional.state, optional.status_code), ("degraded", 200))
        self.assertEqual((required.state, required.status_code), ("unavailable", 503))


class ReadinessTransitionTests(unittest.TestCase):
    def test_degradation_is_reported_immediately_then_reminds_and_recovers(self):
        now = [0.0]
        tracker = ReadinessTransitionTracker(
            clock=lambda: now[0],
            reminder_seconds=900,
        )
        degraded = ReadinessSnapshot(
            "degraded",
            {"database": "ready", "usenet_engine": "degraded"},
        )
        ready = ReadinessSnapshot("ready", {})

        with (
            patch("comet.observability.readiness.metrics.set_readiness") as metric,
            patch("comet.observability.readiness.log.warning") as warning,
            patch("comet.observability.readiness.log.info") as info,
        ):
            tracker.observe(degraded)
            warning.assert_called_once()
            self.assertEqual(
                warning.call_args.kwargs["details"],
                "database=ready usenet_engine=degraded",
            )
            now[0] = 1
            tracker.observe(degraded)
            warning.assert_called_once()
            for value in (2, 100, 899):
                now[0] = value
                tracker.observe(degraded)
            warning.assert_called_once()
            now[0] = 901
            tracker.observe(degraded)
            self.assertEqual(warning.call_count, 2)
            now[0] = 902
            tracker.observe(ready)
            info.assert_called_once()
            self.assertEqual(
                info.call_args.args,
                ("readiness.recovered", "Application readiness recovered"),
            )
            self.assertGreaterEqual(metric.call_count, 7)

    def test_reset_starts_a_fresh_worker_transition_history(self):
        tracker = ReadinessTransitionTracker(clock=lambda: 1)
        unavailable = ReadinessSnapshot(
            "unavailable",
            {"database": "ready", "schema": "unavailable"},
        )
        with patch("comet.observability.readiness.log.error") as error:
            tracker.observe(unavailable)
            tracker.reset()
            tracker.observe(unavailable)
        self.assertEqual(error.call_count, 2)
        self.assertEqual(
            error.call_args.kwargs["details"],
            "database=ready schema=unavailable",
        )
