import gc
import json
import logging
import os
import tracemalloc
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from comet.core.models import AppSettings
from comet.observability.context import (
    TerminalFlag,
    create_detached_task,
    current_request_id,
    current_terminal_flags,
    request_context,
)
from comet.observability.logging import (
    LoggingSettings,
    LogValidationError,
    bootstrap_failure,
    configuration_invalid,
    configure,
    configure_stdlib_bridge,
    ingest_native_event,
    log,
    recent_logs,
)


def logging_settings(profile="normal", format_="json", *, no_color=False):
    values = {"LOG_PROFILE": profile, "LOG_FORMAT": format_}
    if no_color:
        values["NO_COLOR"] = ""
    return LoggingSettings(_env_file=None, **values)


class LoggingContractTests(unittest.TestCase):
    def render(self, call):
        writes = []
        with patch("comet.observability.logging.os.write") as write:
            write.side_effect = lambda _fd, payload: (
                writes.append(payload) or len(payload)
            )
            call()
        return writes

    def configure(
        self,
        profile="normal",
        format_="json",
        *,
        strict=True,
        no_color=False,
    ):
        return configure(
            logging_settings(profile, format_, no_color=no_color),
            process_role="web_worker",
            strict=strict,
        )

    def test_settings_expose_exact_profiles_and_formats_without_echoing_input(self):
        for profile in ("quiet", "normal", "verbose", "debug"):
            self.assertEqual(
                logging_settings(profile).LOG_PROFILE.value,
                profile,
            )
        for format_ in ("pretty", "json"):
            self.assertEqual(
                logging_settings(format_=format_).LOG_FORMAT.value,
                format_,
            )
        canary = "secret-invalid-profile"
        with self.assertRaises(Exception) as context:
            logging_settings(canary)
        self.assertNotIn(canary, str(context.exception))

    def test_generated_secret_has_an_explicit_bounded_log_contract(self):
        self.configure("normal")
        records = self.render(
            lambda: log.info(
                "config.generated_secret",
                "Generated in-memory operator secret",
                setting_name="ADMIN_DASHBOARD_PASSWORD",
                generated_secret="generated-value",
            )
        )

        self.assertEqual(
            json.loads(records[0])["generated_secret"],
            "generated-value",
        )
        with self.assertRaises(LogValidationError):
            log.info(
                "config.generated_secret",
                "Generated in-memory operator secret",
                setting_name="ADMIN_DASHBOARD_PASSWORD",
                generated_secret="bad\nvalue",
            )

    def test_invalid_configuration_log_explains_cross_field_failure(self):
        self.configure("normal")
        try:
            AppSettings(_env_file=None, COMETNET_ENABLED=True)
        except ValidationError as error:
            records = self.render(
                lambda captured_error=error: configuration_invalid(
                    exception=captured_error
                )
            )
        else:
            self.fail("invalid CometNet topology was accepted")

        payload = json.loads(records[0])
        self.assertEqual(payload["event"], "config.invalid")
        self.assertIn("public CometNet requires", payload["details"])

    def test_invalid_configuration_preserves_loader_failure_diagnostic(self):
        self.configure("normal")
        error = ValueError(
            "stored operator setting is not recognized: LEGACY_SETTING"
        )

        records = self.render(lambda: configuration_invalid(exception=error))

        payload = json.loads(records[0])
        self.assertEqual(payload["event"], "config.invalid")
        self.assertEqual(payload["error_type"], "ValueError")
        self.assertEqual(payload["error_message"], str(error))

    def test_bootstrap_failure_identifies_invalid_setting_without_its_value(self):
        secret = "short-secret"
        try:
            AppSettings(
                _env_file=None,
                USENET_NATIVE_ACCESS_TOKEN=secret,
            )
        except ValidationError as error:
            with patch.dict(
                os.environ,
                {"LOG_FORMAT": "json", "LOG_PROFILE": "normal"},
                clear=True,
            ):
                records = self.render(
                    lambda captured_error=error: bootstrap_failure(
                        exception=captured_error,
                        process_role="supervisor",
                    )
                )
        else:
            self.fail("invalid native access token was accepted")

        payload = json.loads(records[0])
        self.assertEqual(payload["event"], "config.invalid")
        self.assertEqual(payload["setting_name"], "USENET_NATIVE_ACCESS_TOKEN")
        self.assertIn("must be an opaque 32-to-256-byte value", payload["details"])
        self.assertNotIn(secret, records[0].decode())

    def test_no_color_uses_presence_not_truthiness(self):
        with patch.dict(os.environ, {}, clear=True):
            absent = logging_settings()
        self.assertFalse(absent.no_color)
        for value in ("", "0", "1"):
            with self.subTest(value=value):
                present = LoggingSettings(
                    _env_file=None,
                    LOG_PROFILE="normal",
                    LOG_FORMAT="pretty",
                    NO_COLOR=value,
                )
                self.assertTrue(present.no_color)

    def test_profile_inclusion_is_monotonic(self):
        observed = {}
        for profile in ("quiet", "normal", "verbose", "debug"):
            self.configure(profile)
            writes = self.render(
                lambda: (
                    log.info("contract.normal", "Normal event"),
                    log.verbose("contract.verbose", "Verbose event"),
                    log.debug("contract.debug", "Debug event"),
                    log.warning(
                        "contract.warning",
                        "Warning event",
                        error_code="dependency_warning",
                    ),
                    log.error(
                        "contract.error",
                        "Error event",
                        error_code="unexpected_failure",
                    ),
                )
            )
            observed[profile] = {json.loads(line)["event"] for line in writes}

        self.assertEqual(
            observed["quiet"],
            {"contract.warning", "contract.error"},
        )
        self.assertLess(observed["quiet"], observed["normal"])
        self.assertLess(observed["normal"], observed["verbose"])
        self.assertLess(observed["verbose"], observed["debug"])

    def test_terminal_policy_and_request_flags_apply_before_filtering(self):
        self.configure("quiet")
        with request_context():
            writes = self.render(
                lambda: log.terminal(
                    "search.completed",
                    "Search completed",
                    outcome="ok",
                    candidate_count=0,
                )
            )
            self.assertEqual(writes, [])
            self.assertEqual(
                current_terminal_flags().snapshot(),
                TerminalFlag.BUSINESS_SEEN,
            )

        with request_context():
            records = self.render(
                lambda: log.terminal(
                    "search.completed",
                    "Search completed",
                    outcome="partial",
                    candidate_count=0,
                    error_code="search_failure",
                )
            )
            self.assertEqual(json.loads(records[0])["level"], "WARNING")
            self.assertEqual(
                current_terminal_flags().snapshot(),
                TerminalFlag.BUSINESS_SEEN | TerminalFlag.BUSINESS_FAILURE_EXPLAINED,
            )

    def test_transport_failure_control_is_private_and_owner_restricted(self):
        self.configure("debug")
        with request_context():
            records = self.render(
                lambda: log.terminal(
                    "stream.completed",
                    "Stream completed",
                    outcome="failed",
                    transfer_mode="full",
                    transferred_bytes=0,
                    duration_ms=1,
                    error_code="transport_failure",
                    transport_failure_explained=True,
                )
            )
            payload = json.loads(records[0])
            self.assertNotIn("transport_failure_explained", payload)
            self.assertTrue(
                current_terminal_flags().snapshot()
                & TerminalFlag.TRANSPORT_FAILURE_EXPLAINED
            )

        with self.assertRaises(LogValidationError):
            log.terminal(
                "search.completed",
                "Search completed",
                outcome="failed",
                transport_failure_explained=True,
            )

    def test_invalid_terminal_never_changes_request_flags(self):
        self.configure("debug")
        with request_context():
            with self.assertRaises(LogValidationError):
                log.terminal(
                    "search.completed",
                    "Search completed",
                    outcome="invented",
                )
            self.assertEqual(current_terminal_flags().snapshot(), TerminalFlag(0))

    def test_terminal_owner_is_unique_but_playback_and_stream_are_distinct(self):
        self.configure("normal")
        with request_context():
            self.render(
                lambda: log.terminal(
                    "playback.completed",
                    "Playback completed",
                    outcome="ok",
                    playback_mode="proxy",
                    duration_ms=1,
                )
            )
            self.render(
                lambda: log.terminal(
                    "stream.completed",
                    "Stream completed",
                    outcome="ok",
                    transfer_mode="full",
                    transferred_bytes=1,
                    duration_ms=1,
                )
            )
            with self.assertRaisesRegex(LogValidationError, "duplicate_terminal"):
                log.terminal(
                    "playback.completed",
                    "Playback completed",
                    outcome="ok",
                    playback_mode="proxy",
                    duration_ms=1,
                )

    def test_cancelled_terminal_explains_business_failure(self):
        self.configure("quiet")
        with request_context():
            writes = self.render(
                lambda: log.terminal(
                    "search.completed",
                    "Search completed",
                    outcome="cancelled",
                    candidate_count=0,
                )
            )
            self.assertEqual(writes, [])
            self.assertEqual(
                current_terminal_flags().snapshot(),
                TerminalFlag.BUSINESS_SEEN | TerminalFlag.BUSINESS_FAILURE_EXPLAINED,
            )

    def test_json_and_pretty_have_the_same_semantics_and_one_line(self):
        records = {}
        for format_ in ("json", "pretty"):
            self.configure("normal", format_, no_color=True)
            records[format_] = self.render(
                lambda: log.info(
                    "runtime.starting",
                    "Runtime starting",
                    log_profile="normal",
                    worker_count=2,
                )
            )[0]

        parsed = json.loads(records["json"])
        self.assertEqual(parsed["event"], "runtime.starting")
        self.assertEqual(parsed["message"], "Runtime starting")
        self.assertEqual(parsed["log_profile"], "normal")
        self.assertEqual(parsed["worker_count"], 2)
        self.assertRegex(
            parsed["timestamp"],
            r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$",
        )
        self.assertEqual(records["json"].count(b"\n"), 1)
        self.assertEqual(records["pretty"].count(b"\n"), 1)
        self.assertIn(b"profile=normal", records["pretty"])
        pretty = records["pretty"].decode("utf-8")
        self.assertRegex(pretty, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \|")
        self.assertIn(" | 🌠 COMET | INFO | ", pretty)

    def test_native_json_crosses_one_strict_structured_boundary(self):
        self.configure("normal", "json")
        native = (
            b'{"timestamp":"2026-07-31 00:00:00","level":"INFO",'
            b'"event":"native.session.opened","message":"Native session opened",'
            b'"category":"USENET","process_role":"usenet_engine","pid":42,'
            b'"engine_generation":3,"request_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
            b'"provider_host":"news.example.test","article_count":12}\n'
        )
        records = self.render(lambda: ingest_native_event(native))
        parsed = json.loads(records[0])
        self.assertEqual(parsed["process_role"], "usenet_engine")
        self.assertEqual(parsed["engine_generation"], 3)
        self.assertEqual(parsed["provider_host"], "news.example.test")
        self.assertEqual(parsed["article_count"], 12)

        with self.assertRaises(LogValidationError):
            ingest_native_event(native.replace(b'"USENET"', b'"STREAM"'))

    def test_human_views_share_the_compact_field_presentation(self):
        self.configure("normal", "pretty", no_color=True)
        rendered = self.render(
            lambda: log.info(
                "search.completed",
                "Search completed",
                outcome="ok",
                candidate_count=0,
                success_count=0,
                duration_ms=12.25,
            )
        )[0].decode()

        self.assertIn("candidates=0 duration=12.2ms", rendered)
        self.assertNotIn("outcome=", rendered)
        self.assertNotIn("succeeded=", rendered)
        self.assertNotIn("[search.completed]", rendered)
        dashboard_record = recent_logs()["logs"][-1]
        self.assertEqual(
            dashboard_record["details"],
            "candidates=0 duration=12.2ms",
        )
        self.assertNotIn("candidate_count", dashboard_record)
        self.assertNotIn("event", dashboard_record)

    def test_validation_rejects_dynamic_shapes_controls_and_non_finite_values(self):
        self.configure("debug")
        cases = (
            lambda: log.info("Bad.Event", "Safe"),
            lambda: log.info("safe.event", "unsafe\nmessage"),
            lambda: log.info("safe.event", "Safe", unknown="value"),
            lambda: log.info("safe.event", "Safe", candidate_count=[]),
            lambda: log.info("safe.event", "Safe", duration_ms=float("nan")),
            lambda: log.info(
                "safe.event",
                "Safe",
                **{f"field_{index}": index for index in range(13)},
            ),
        )
        for operation in cases:
            with (
                self.subTest(operation=operation),
                self.assertRaises(LogValidationError),
            ):
                operation()

    def test_evolving_operational_tokens_are_not_closed_by_logging(self):
        self.configure("normal")
        records = self.render(
            lambda: log.error(
                "ingestion.loop.failed",
                "Ingestion loop failed",
                error_code="ingestion_loop_failure",
                cache_state="unknown",
                playback_mode="future_mode",
                source_type="future_transport",
            )
        )

        payload = json.loads(records[0])
        self.assertEqual(payload["error_code"], "ingestion_loop_failure")
        self.assertEqual(payload["cache_state"], "unknown")
        self.assertEqual(payload["playback_mode"], "future_mode")
        self.assertEqual(payload["source_type"], "future_transport")

    def test_filtered_ordinary_event_skips_validation(self):
        self.configure("quiet")
        self.assertEqual(
            self.render(
                lambda: log.debug(
                    "ATTACKER\nEVENT",
                    "attacker\nmessage",
                    unknown={"payload": "canary"},
                )
            ),
            [],
        )

    def test_exception_exposes_message_and_safe_frames(self):
        canary = "exception-secret\n\x1b[31m"
        self.configure("debug")
        try:
            raise RuntimeError(canary)
        except RuntimeError as error:
            captured_error = error
            records = self.render(
                lambda: log.error(
                    "operation.failed",
                    "Operation failed",
                    error_code="unexpected_failure",
                    exc=captured_error,
                )
            )
        payload = json.loads(records[0])
        self.assertEqual(payload["error_type"], "RuntimeError")
        self.assertEqual(
            payload["error_message"],
            r"exception-secret\n\x1b[31m",
        )
        self.assertIn("debug_stack", payload)
        self.assertNotIn(os.getcwd(), payload["debug_stack"])

        self.configure("normal")
        payload = json.loads(
            self.render(
                lambda: log.error(
                    "operation.failed",
                    "Operation failed",
                    error_code="unexpected_failure",
                    exc=captured_error,
                )
            )[0]
        )
        self.assertEqual(payload["error_message"], r"exception-secret\n\x1b[31m")
        self.assertNotIn("debug_stack", payload)

        self.configure("debug", "pretty", no_color=True)
        rendered = self.render(
            lambda: log.error(
                "operation.failed",
                "Operation failed",
                error_code="unexpected_failure",
                exc=captured_error,
            )
        )[0].decode()
        self.assertEqual(rendered.count("\n"), 1)
        self.assertIn(
            r"exception=RuntimeError exception_message=exception-secret\n\x1b[31m",
            rendered,
        )

    def test_uvicorn_startup_failure_preserves_the_underlying_exception(self):
        self.configure("normal")
        configure_stdlib_bridge()

        records = self.render(
            lambda: logging.getLogger("uvicorn.error").error(
                OSError(98, "Address already in use")
            )
        )

        payload = json.loads(records[0])
        self.assertEqual(payload["event"], "dependency.uvicorn.failed")
        self.assertEqual(payload["error_type"], "OSError")
        self.assertEqual(payload["error_message"], "[Errno 98] Address already in use")

    def test_gunicorn_failure_preserves_context_and_exception(self):
        self.configure("normal")
        configure_stdlib_bridge()

        try:
            raise RuntimeError("worker boot failed")
        except RuntimeError:
            records = self.render(
                lambda: logging.getLogger("gunicorn.error").exception(
                    "Worker failed to boot"
                )
            )

        payload = json.loads(records[0])
        self.assertEqual(payload["event"], "dependency.gunicorn.failed")
        self.assertEqual(payload["details"], "Worker failed to boot")
        self.assertEqual(payload["error_type"], "RuntimeError")
        self.assertEqual(payload["error_message"], "worker boot failed")

    def test_production_rejection_is_fixed_and_does_not_raise(self):
        self.configure("normal", strict=False)
        canary = "secret\n\x1b[31m"
        records = self.render(lambda: log.info("invalid", canary, unknown=canary))
        self.assertLessEqual(len(records), 1)
        if records:
            rendered = records[0].decode()
            self.assertNotIn(canary, rendered)
            self.assertEqual(json.loads(records[0])["event"], "logging.record.rejected")

    def test_context_is_generated_and_restored(self):
        self.configure("normal")
        self.assertIsNone(current_request_id())
        with request_context() as identifier:
            self.assertRegex(identifier, r"^[0-9a-f]{32}$")
            payload = json.loads(
                self.render(lambda: log.info("request.observed", "Request observed"))[0]
            )
            self.assertEqual(payload["request_id"], identifier)
        self.assertIsNone(current_request_id())

    def test_one_million_filtered_events_retain_less_than_one_mebibyte(self):
        self.configure("normal")
        for _ in range(1_000):
            log.debug("contract.debug", "Debug event", candidate_count=1)
        gc.collect()
        tracemalloc.start()
        baseline = tracemalloc.get_traced_memory()[0]
        for _ in range(1_000_000):
            log.debug("contract.debug", "Debug event", candidate_count=1)
        gc.collect()
        retained = tracemalloc.get_traced_memory()[0] - baseline
        tracemalloc.stop()
        self.assertLess(retained, 1024 * 1024)


class DetachedTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_detached_task_does_not_inherit_request_id(self):
        configure(
            logging_settings("normal", "json"),
            process_role="web_worker",
            strict=True,
        )
        observed = []

        async def worker():
            observed.append(current_request_id())

        with request_context():
            task = create_detached_task(worker(), name="maintenance.cleanup")
            await task

        self.assertEqual(observed, [None])


if __name__ == "__main__":
    unittest.main()
