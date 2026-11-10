import unittest
from unittest.mock import patch

from comet.utils.memory import trim_process_memory


class MemoryUtilityLifecycleTests(unittest.TestCase):
    def test_trim_collects_garbage_then_releases_libc_heap(self):
        with (
            patch("comet.utils.memory.gc.collect") as collect,
            patch("comet.utils.memory._malloc_trim") as malloc_trim,
        ):
            trim_process_memory()

        collect.assert_called_once_with()
        malloc_trim.assert_called_once_with(0)

    def test_trim_without_libc_symbol_only_collects_garbage(self):
        with (
            patch("comet.utils.memory.gc.collect") as collect,
            patch("comet.utils.memory._malloc_trim", None),
        ):
            trim_process_memory()

        collect.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
