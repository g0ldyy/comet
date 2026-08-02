import os
import unittest
from contextlib import chdir
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from comet.healthcheck import main


class HealthcheckTests(unittest.TestCase):
    def test_uses_server_port_from_dotenv(self):
        with (
            TemporaryDirectory() as directory,
            patch.dict(os.environ, {}, clear=True),
            chdir(directory),
            patch("comet.healthcheck.HTTPConnection") as http_connection,
        ):
            Path(".env").write_text("FASTAPI_PORT=7020\n", encoding="utf-8")
            connection = http_connection.return_value
            connection.getresponse.return_value.status = 200

            exit_code = main()

        self.assertEqual(exit_code, 0)
        http_connection.assert_called_once_with("127.0.0.1", 7020, timeout=5)
        connection.request.assert_called_once_with("GET", "/ready")
        connection.close.assert_called_once_with()

    def test_fails_silently_when_server_is_unreachable(self):
        with patch("comet.healthcheck.HTTPConnection") as http_connection:
            connection = http_connection.return_value
            connection.request.side_effect = OSError

            self.assertEqual(main(), 1)
            connection.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
