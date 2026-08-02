"""Small argparse boundary that never echoes argv or rejected input."""

from __future__ import annotations

import argparse
import os

_INVALID_ARGUMENTS = b"Comet command line is invalid\n"


def write_cli_error() -> None:
    try:
        os.write(2, _INVALID_ARGUMENTS)
    except OSError:
        pass


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        write_cli_error()
        raise SystemExit(2) from None

    def exit(self, status: int = 0, message: str | None = None) -> None:
        if status:
            write_cli_error()
        raise SystemExit(status) from None


__all__ = ("SafeArgumentParser", "write_cli_error")
