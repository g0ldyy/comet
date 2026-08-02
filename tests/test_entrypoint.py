import unittest
from unittest.mock import Mock, patch

from comet import web
from comet.web import resolved_workers, run_with_gunicorn, run_with_uvicorn


class UvicornEntrypointTests(unittest.TestCase):
    def test_owned_runtime_restart_reenters_the_topology_launcher(self):
        configured = Mock(USENET_ENABLED=False, USE_GUNICORN=False)
        environment = {"COMET_TEST_RESTART": "1"}
        with (
            patch.object(web, "settings", configured),
            patch.object(web, "resolved_workers", return_value=1),
            patch.object(web, "start_event_persistence"),
            patch.object(web, "stop_event_persistence"),
            patch.object(web, "log_runtime_starting"),
            patch.object(web, "log_startup_configuration"),
            patch.object(web, "run_with_uvicorn", side_effect=KeyboardInterrupt),
            patch.object(web, "consume_runtime_restart_request", return_value=True),
            patch.object(web, "fresh_runtime_environment", return_value=environment),
            patch.object(web.os, "execvpe") as execute,
        ):
            web.main()

        execute.assert_called_once_with(
            web.sys.executable,
            [web.sys.executable, "-m", "comet.main"],
            environment,
        )

    def test_supervised_web_child_leaves_restart_to_its_parent(self):
        configured = Mock(USENET_ENABLED=True, USE_GUNICORN=False)
        with (
            patch.object(web, "settings", configured),
            patch.object(web, "resolved_workers", return_value=1),
            patch.object(web, "start_event_persistence"),
            patch.object(web, "stop_event_persistence"),
            patch.object(web, "log_startup_configuration"),
            patch.object(web, "run_with_uvicorn"),
            patch.object(web, "consume_runtime_restart_request") as consume,
        ):
            web.main()

        consume.assert_not_called()

    def test_forwarded_headers_need_no_proxy_allowlist(self):
        with patch("comet.web.uvicorn.run") as run:
            run_with_uvicorn(3)

        self.assertIs(run.call_args.kwargs["proxy_headers"], True)
        self.assertEqual(run.call_args.kwargs["forwarded_allow_ips"], "*")
        self.assertEqual(run.call_args.kwargs["workers"], 3)
        self.assertIs(run.call_args.kwargs["access_log"], False)

    def test_unexpected_server_failure_is_visible_to_the_process(self):
        failure = Mock(side_effect=RuntimeError("startup failed"))

        with (
            patch("comet.web.uvicorn.run", failure),
            self.assertRaisesRegex(RuntimeError, "startup failed"),
        ):
            run_with_uvicorn(1)

    def test_resolved_worker_count_has_one_bounded_domain(self):
        with patch.dict(
            "comet.web.os.environ",
            {"COMET_RESOLVED_FASTAPI_WORKERS": "64"},
            clear=True,
        ):
            self.assertEqual(resolved_workers(), 64)

        for value in ("65", "not-an-integer"):
            with (
                self.subTest(value=value),
                patch.dict(
                    "comet.web.os.environ",
                    {"COMET_RESOLVED_FASTAPI_WORKERS": value},
                    clear=True,
                ),
                self.assertRaises(RuntimeError),
            ):
                resolved_workers()

    def test_gunicorn_configuration_is_applied_exactly(self):
        with (
            patch("comet.web.install_stderr_proxy"),
            patch(
                "gunicorn.app.base.BaseApplication.run",
                autospec=True,
            ) as run,
        ):
            run_with_gunicorn(3)

        application = run.call_args.args[0]
        self.assertEqual(application.cfg.workers, 3)
        self.assertIsNone(application.cfg.accesslog)
        self.assertIsNone(application.cfg.errorlog)
        self.assertEqual(application.cfg.forwarded_allow_ips, ["*"])
        self.assertIs(application.cfg.control_socket_disable, True)


if __name__ == "__main__":
    unittest.main()
