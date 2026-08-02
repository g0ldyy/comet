"""Fail-closed stderr proxy for third-party code that bypasses logging."""

from __future__ import annotations

import sys
from typing import Any


class _ClosedBinaryStderr:
    def write(self, payload: Any) -> int:
        try:
            return len(payload)
        except Exception:
            return 0

    def flush(self) -> None:
        return None

    def fileno(self) -> int:
        return 2

    def isatty(self) -> bool:
        return False


class ClosedStderr:
    encoding = "utf-8"
    errors = "replace"

    def __init__(self) -> None:
        self.buffer = _ClosedBinaryStderr()

    def write(self, payload: Any) -> int:
        try:
            return len(payload)
        except Exception:
            return 0

    def flush(self) -> None:
        return None

    def fileno(self) -> int:
        return 2

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True


_closed_stderr = ClosedStderr()


def install_stderr_proxy() -> None:
    sys.stderr = _closed_stderr


__all__ = ("ClosedStderr", "install_stderr_proxy")
