import unittest
from unittest.mock import Mock, patch

from comet.main import run_with_uvicorn


class UvicornEntrypointTests(unittest.TestCase):
    def test_forwarded_headers_need_no_proxy_allowlist(self):
        server = Mock()
        with (
            patch("comet.main.uvicorn.Config", return_value=object()) as config,
            patch("comet.main.uvicorn.Server", return_value=server),
            patch("comet.main.log_startup_info"),
        ):
            run_with_uvicorn()

        self.assertIs(config.call_args.kwargs["proxy_headers"], True)
        self.assertEqual(config.call_args.kwargs["forwarded_allow_ips"], "*")

    def test_unexpected_server_failure_is_visible_to_the_process(self):
        server = Mock()
        server.run.side_effect = RuntimeError("startup failed")

        with (
            patch("comet.main.uvicorn.Config", return_value=object()),
            patch("comet.main.uvicorn.Server", return_value=server),
            patch("comet.main.log_startup_info"),
            patch("comet.main.logger.exception") as error_log,
            patch("comet.main.logger.log") as lifecycle_log,
            self.assertRaisesRegex(RuntimeError, "startup failed"),
        ):
            run_with_uvicorn()

        self.assertIn("startup failed", error_log.call_args.args[0])
        lifecycle_log.assert_called_with("COMET", "Server Shutdown")


if __name__ == "__main__":
    unittest.main()
