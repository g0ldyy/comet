import unittest
from unittest.mock import Mock, patch

from comet.core import execution


class ExecutionLifecycleTests(unittest.TestCase):
    def tearDown(self):
        execution.app_executor = None

    def test_setup_is_idempotent_and_shutdown_is_nonblocking(self):
        executor = Mock()
        with patch.object(
            execution,
            "ProcessPoolExecutor",
            return_value=executor,
        ) as executor_class:
            execution.app_executor = None
            execution.setup_executor(1, "normal", "pretty", True)
            execution.setup_executor(1, "normal", "pretty", True)
            self.assertIs(execution.get_executor(), executor)
            executor_class.assert_called_once()

            execution.shutdown_executor()

        self.assertIsNone(execution.get_executor())
        executor.shutdown.assert_called_once_with(
            wait=False,
            cancel_futures=True,
        )


if __name__ == "__main__":
    unittest.main()
