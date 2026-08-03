"""Import-safe lifecycle for the CPU process pool."""

from __future__ import annotations

import atexit
import multiprocessing
import os
import signal
from concurrent.futures import ProcessPoolExecutor

from comet.observability.stderr import install_stderr_proxy

app_executor: ProcessPoolExecutor | None = None
_max_workers: int | None = None
_executor_config: tuple[int, str, str, bool] | None = None
PreparedExecutor = tuple[ProcessPoolExecutor, tuple[int, str, str, bool]]


def _multiprocessing_context():
    try:
        return multiprocessing.get_context("forkserver")
    except ValueError:
        return multiprocessing.get_context("spawn")


def worker_initializer(
    log_profile: str,
    log_format: str,
    no_color: bool,
) -> None:
    install_stderr_proxy()
    try:
        from comet.core.models import database
        from comet.observability.context import clear_context
        from comet.observability.events import start_event_persistence
        from comet.observability.logging import LoggingSettings, configure

        values = {
            "LOG_PROFILE": log_profile,
            "LOG_FORMAT": log_format,
        }
        if no_color:
            values["NO_COLOR"] = "1"
        configure(
            LoggingSettings(_env_file=None, **values),
            process_role="executor_worker",
        )
        start_event_persistence(str(database.url))
        clear_context()
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except BaseException as exc:
        try:
            from comet.observability.logging import bootstrap_failure

            bootstrap_failure(exception=exc, process_role="executor_worker")
        finally:
            os._exit(78)


def setup_executor(
    max_workers: int,
    log_profile: str,
    log_format: str,
    no_color: bool,
) -> None:
    global app_executor, _executor_config, _max_workers

    if app_executor is not None:
        return
    _max_workers = max_workers
    _executor_config = (max_workers, log_profile, log_format, no_color)
    app_executor = _create_executor(
        max_workers,
        log_profile,
        log_format,
        no_color,
    )


def _create_executor(
    max_workers: int,
    log_profile: str,
    log_format: str,
    no_color: bool,
) -> ProcessPoolExecutor:
    return ProcessPoolExecutor(
        max_workers=max_workers,
        mp_context=_multiprocessing_context(),
        initializer=worker_initializer,
        initargs=(log_profile, log_format, no_color),
    )


def replace_executor(
    max_workers: int,
    log_profile: str,
    log_format: str,
    no_color: bool,
) -> None:
    prepared = prepare_executor(max_workers, log_profile, log_format, no_color)
    if prepared is None:
        return
    install_executor(prepared)


def prepare_executor(
    max_workers: int,
    log_profile: str,
    log_format: str,
    no_color: bool,
) -> PreparedExecutor | None:
    config = (max_workers, log_profile, log_format, no_color)
    if app_executor is None or _executor_config == config:
        return None
    return _create_executor(*config), config


def install_executor(prepared: PreparedExecutor) -> None:
    global app_executor, _executor_config, _max_workers

    replacement, config = prepared
    previous, app_executor = app_executor, replacement
    _max_workers = config[0]
    _executor_config = config
    previous.shutdown(wait=False, cancel_futures=False)


def discard_executor(prepared: PreparedExecutor | None) -> None:
    if prepared is not None:
        prepared[0].shutdown(wait=False, cancel_futures=True)


def shutdown_executor() -> None:
    global app_executor, _executor_config, _max_workers
    executor = app_executor
    app_executor = None
    _executor_config = None
    _max_workers = None
    if executor:
        executor.shutdown(wait=False, cancel_futures=True)


def get_executor() -> ProcessPoolExecutor | None:
    return app_executor


def configured_max_workers() -> int | None:
    return _max_workers


atexit.register(shutdown_executor)

__all__ = (
    "configured_max_workers",
    "discard_executor",
    "get_executor",
    "install_executor",
    "prepare_executor",
    "replace_executor",
    "setup_executor",
    "shutdown_executor",
    "worker_initializer",
)
