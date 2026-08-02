import unittest
from unittest.mock import AsyncMock, Mock, call, patch

from comet import main
from comet.core.models import AppSettings
from comet.core.operator_settings import EffectiveSettingsPayload
from comet.usenet.engine_transport import EngineUnavailable


class _Process:
    def __init__(self, polls, returncode=0):
        self._polls = iter(polls)
        self.returncode = returncode
        self.pid = 123

    def poll(self):
        try:
            return next(self._polls)
        except StopIteration:
            return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


class _Runtime:
    def __init__(self):
        self.published = 0
        self.withdrawn = 0
        self.engine_generation = 1

    async def publish_descriptor(self, timeout=30):
        self.published += 1
        self.timeout = timeout

    def withdraw_descriptor(self):
        self.withdrawn += 1

    def close(self):
        pass

    def prepare_runtime_dir(self):
        pass


class MainSupervisorTests(unittest.TestCase):
    def test_supervisor_restarts_the_engine_without_restarting_the_web_master(self):
        runtime = _Runtime()
        failed_engine = _Process([1])
        restarted_engine = _Process([None])
        web = _Process([None, None, 0])
        with (
            patch("comet.main._spawn_engine", return_value=restarted_engine) as spawned,
            patch("comet.main.time.sleep"),
        ):
            returncode, active_engine, active_runtime = main._supervise_engine(
                runtime, failed_engine, web
            )

        self.assertEqual(returncode, 1)
        self.assertIs(active_engine, restarted_engine)
        self.assertIs(active_runtime, runtime)
        self.assertEqual(runtime.published, 1)
        self.assertEqual(runtime.withdrawn, 1)
        self.assertEqual(runtime.timeout, main.settings.USENET_START_TIMEOUT_SECONDS)
        spawned.assert_called_once_with(runtime)

    def test_repeated_short_lived_engines_back_off_after_successful_publication(self):
        runtime = _Runtime()
        failed_engine = _Process([1])
        restarted_once = _Process([1])
        restarted_twice = _Process([None])
        web = _Process([None, None, None, None, 0])

        with (
            patch(
                "comet.main._spawn_engine",
                side_effect=(restarted_once, restarted_twice),
            ),
            patch("comet.main.time.monotonic", return_value=0.0),
            patch("comet.main.time.sleep") as sleep,
        ):
            main._supervise_engine(runtime, failed_engine, web)

        self.assertEqual(
            [entry.args[0] for entry in sleep.call_args_list],
            [0.25, 0.5],
        )
        self.assertEqual(runtime.published, 2)
        self.assertEqual(runtime.withdrawn, 2)

    def test_recovery_requires_stability_and_is_emitted_once(self):
        runtime = _Runtime()
        failed_engine = _Process([1])
        restarted_engine = _Process([None, None])
        web = _Process([None, None, None, 0])
        with (
            patch("comet.main._spawn_engine", return_value=restarted_engine),
            patch("comet.main._ENGINE_STABLE_SECONDS", 0),
            patch("comet.main.time.sleep"),
            patch("comet.main.log.info") as info,
        ):
            main._supervise_engine(runtime, failed_engine, web)

        recovered = [
            item
            for item in info.call_args_list
            if item.args and item.args[0] == "native_engine.recovered"
        ]
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].kwargs["attempt_count"], 1)

    def test_engine_settings_reload_replaces_only_the_native_runtime(self):
        runtime = _Runtime()
        replacement_runtime = _Runtime()
        engine = _Process([None])
        replacement_engine = _Process([None])
        web = _Process([None, 0])
        reload_requests = iter((True, False))

        with (
            patch("comet.main._reload_supervisor_settings", return_value=True),
            patch(
                "comet.main._build_engine_supervisor",
                return_value=replacement_runtime,
            ),
            patch("comet.main._spawn_engine", return_value=replacement_engine),
            patch("comet.main._drain_and_stop_engine") as drain,
        ):
            returncode, active_engine, active_runtime = main._supervise_engine(
                runtime,
                engine,
                web,
                engine_reload_requested=lambda: next(reload_requests),
            )

        self.assertEqual(returncode, 1)
        self.assertIs(active_engine, replacement_engine)
        self.assertIs(active_runtime, replacement_runtime)
        drain.assert_called_once_with(runtime, engine)
        self.assertEqual(replacement_runtime.published, 1)

    def test_supervisor_reload_keeps_usenet_disablement_atomic(self):
        current = AppSettings(
            _env_file=None,
            USENET_ENABLED=True,
            USENET_ENGINE_ENABLED=True,
            USENET_ENGINE_REQUIRED=True,
            USENET_NATIVE_ACCESS_TOKEN="u" * 32,
            COMET_CAPABILITY_SECRET="A" * 43,
            COMET_CAPABILITY_SECRET_FILE=None,
        )
        payload = EffectiveSettingsPayload(
            revision=1,
            values={
                "USENET_ENABLED": False,
                "USENET_ENGINE_ENABLED": False,
                "USENET_ENGINE_REQUIRED": False,
                "USENET_NATIVE_ACCESS_TOKEN": None,
                "COMET_CAPABILITY_SECRET": None,
                "COMET_CAPABILITY_SECRET_FILE": None,
            },
            dashboard_keys=frozenset(),
            generated_keys=frozenset(),
            deployment_keys=frozenset(),
        )
        live = Mock()
        live.active_snapshot.return_value = current

        with (
            patch.object(main, "settings", live),
            patch.object(
                main,
                "_prepare_effective_payload",
                new=AsyncMock(return_value=payload),
            ),
        ):
            self.assertTrue(main._reload_supervisor_settings())

        candidate = live.publish.call_args.args[0]
        self.assertTrue(candidate.USENET_ENABLED)
        self.assertTrue(candidate.USENET_ENGINE_ENABLED)
        self.assertTrue(candidate.USENET_ENGINE_REQUIRED)
        self.assertEqual(candidate.USENET_NATIVE_ACCESS_TOKEN, "u" * 32)
        self.assertEqual(candidate.COMET_CAPABILITY_SECRET, "A" * 43)

    def test_worker_count_is_resolved_once_for_the_web_master(self):
        with patch("comet.main.settings.FASTAPI_WORKERS", 3):
            environment = main._web_environment()

        self.assertEqual(environment["COMET_RESOLVED_FASTAPI_WORKERS"], "3")

    def test_supervisor_preserves_a_nonzero_web_master_exit(self):
        runtime = _Runtime()
        engine = _Process([None])
        web = _Process([7], returncode=7)

        returncode, active_engine, active_runtime = main._supervise_engine(
            runtime, engine, web
        )

        self.assertEqual(returncode, 7)
        self.assertIs(active_engine, engine)
        self.assertIs(active_runtime, runtime)
        self.assertEqual(runtime.published, 0)

    def test_shutdown_stops_supervision_without_restarting_the_engine(self):
        runtime = _Runtime()
        engine = _Process([1])
        web = _Process([None])

        with patch("comet.main._spawn_engine") as spawned:
            returncode, active_engine, active_runtime = main._supervise_engine(
                runtime,
                engine,
                web,
                shutdown_requested=lambda: True,
            )

        self.assertEqual(returncode, 0)
        self.assertIs(active_engine, engine)
        self.assertIs(active_runtime, runtime)
        spawned.assert_not_called()

    def test_shutdown_during_restart_backoff_does_not_spawn_a_new_engine(self):
        runtime = _Runtime()
        engine = _Process([1])
        web = _Process([None])
        shutdown = False

        def sleep(_delay):
            nonlocal shutdown
            shutdown = True

        with (
            patch("comet.main._spawn_engine") as spawned,
            patch("comet.main.time.sleep", side_effect=sleep),
        ):
            returncode, active_engine, active_runtime = main._supervise_engine(
                runtime,
                engine,
                web,
                shutdown_requested=lambda: shutdown,
            )

        self.assertEqual(returncode, 0)
        self.assertIs(active_engine, engine)
        self.assertIs(active_runtime, runtime)
        spawned.assert_not_called()

    def test_web_exit_during_restart_backoff_does_not_spawn_a_new_engine(self):
        runtime = _Runtime()
        engine = _Process([1])
        web = _Process([None, 7], returncode=7)

        with (
            patch("comet.main._spawn_engine") as spawned,
            patch("comet.main.time.sleep"),
        ):
            returncode, active_engine, active_runtime = main._supervise_engine(
                runtime,
                engine,
                web,
            )

        self.assertEqual(returncode, 7)
        self.assertIs(active_engine, engine)
        self.assertIs(active_runtime, runtime)
        spawned.assert_not_called()

    def test_unexpected_descriptor_publication_failure_surfaces(self):
        runtime = _Runtime()
        runtime.publish_descriptor = AsyncMock(
            side_effect=RuntimeError("descriptor persistence failed")
        )
        failed_engine = _Process([1])
        restarted_engine = _Process([None])
        web = _Process([None, None])

        with (
            patch("comet.main._spawn_engine", return_value=restarted_engine),
            patch("comet.main._stop_child") as stop,
            patch("comet.main.time.sleep"),
            self.assertRaisesRegex(RuntimeError, "descriptor persistence failed"),
        ):
            main._supervise_engine(
                runtime,
                failed_engine,
                web,
            )

        self.assertEqual(
            stop.call_args_list,
            [call(failed_engine), call(restarted_engine)],
        )

    def test_supervisor_closes_runtime_when_child_shutdown_fails(self):
        runtime = Mock()
        runtime.publish_descriptor = AsyncMock()
        engine = _Process([None])
        web = _Process([None])
        old_term = object()
        old_int = object()
        old_usr1 = object()
        signal_calls = []

        def install_signal(signum, handler):
            signal_calls.append((signum, handler))
            if len(signal_calls) == 1:
                return old_term
            if len(signal_calls) == 2:
                return old_int
            return old_usr1

        with (
            patch("comet.main.EngineSupervisor", return_value=runtime),
            patch("comet.main._web_environment", return_value={}),
            patch("comet.main._spawn_engine", return_value=engine),
            patch("comet.main.subprocess.Popen", return_value=web),
            patch(
                "comet.main._supervise_engine",
                return_value=(0, engine, runtime),
            ),
            patch(
                "comet.main._shutdown_children",
                side_effect=RuntimeError("shutdown failed"),
            ),
            patch("comet.main.signal.signal", side_effect=install_signal),
            self.assertRaisesRegex(RuntimeError, "shutdown failed"),
        ):
            main._run_supervisor()

        runtime.close.assert_called_once_with()
        self.assertEqual(
            signal_calls[-3:],
            [
                (main.signal.SIGTERM, old_term),
                (main.signal.SIGINT, old_int),
                (main.signal.SIGUSR1, old_usr1),
            ],
        )

    def test_runtime_drain_precedes_any_process_group_signal(self):
        runtime = type("Runtime", (), {"descriptor_path": "/engine.json"})()
        engine = _Process([None, None])
        drain = AsyncMock()

        with (
            patch("comet.main.EngineClient") as client,
            patch("comet.main.os.killpg") as kill_group,
        ):
            client.return_value.drain = drain
            main._drain_and_stop_engine(runtime, engine)

        drain.assert_awaited_once_with()
        kill_group.assert_not_called()

    def test_failed_runtime_drain_terminates_the_entire_process_group(self):
        runtime = type("Runtime", (), {"descriptor_path": "/engine.json"})()
        engine = _Process([None, None])

        with (
            patch("comet.main.EngineClient") as client,
            patch("comet.main.os.killpg") as kill_group,
        ):
            client.return_value.drain = AsyncMock(
                side_effect=EngineUnavailable("offline")
            )
            main._drain_and_stop_engine(runtime, engine)

        kill_group.assert_called_once_with(engine.pid, main.signal.SIGTERM)

    def test_child_timeout_kills_the_entire_process_group(self):
        child = _Process([None])
        child.wait = Mock(
            side_effect=[
                main.subprocess.TimeoutExpired("child", 30),
                0,
            ]
        )

        with patch("comet.main.os.killpg") as kill_group:
            result = main._stop_child(child)

        self.assertEqual(result, main.StopResult.KILLED)
        self.assertEqual(
            kill_group.call_args_list,
            [
                call(child.pid, main.signal.SIGTERM),
                call(child.pid, main.signal.SIGKILL),
            ],
        )
        self.assertEqual(
            child.wait.call_args_list[-1],
            call(timeout=main._KILL_REAP_TIMEOUT_SECONDS),
        )

    def test_missing_process_group_does_not_hide_a_live_child(self):
        child = _Process([None])
        child.wait = Mock(side_effect=main.subprocess.TimeoutExpired("child", 0))

        with (
            patch("comet.main.os.killpg", side_effect=ProcessLookupError),
            self.assertRaises(main.subprocess.TimeoutExpired),
        ):
            main._stop_child(child)

    def test_shutdown_drains_web_before_the_native_runtime(self):
        events = []

        with (
            patch(
                "comet.main._stop_child",
                side_effect=lambda child, **_kwargs: events.append(("web", child)),
            ),
            patch(
                "comet.main._drain_and_stop_engine",
                side_effect=lambda runtime, child: events.append(
                    ("engine", runtime, child)
                ),
            ),
        ):
            main._shutdown_children("runtime", "web", "engine")

        self.assertEqual(
            events,
            [
                ("web", "web"),
                ("engine", "runtime", "engine"),
            ],
        )
