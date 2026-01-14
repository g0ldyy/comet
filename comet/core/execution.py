import atexit
import multiprocessing
import os
import signal
from concurrent.futures import ProcessPoolExecutor

from comet.core.models import settings

_mp_context = None
try:
    _mp_context = multiprocessing.get_context("forkserver")
except ValueError:
    _mp_context = multiprocessing.get_context("spawn")

app_executor = None
max_workers = settings.EXECUTOR_MAX_WORKERS
# if max_workers is None:
#     cpu_count = os.cpu_count() or 1
#     max_workers = min(cpu_count, 4)


def worker_initializer():
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def setup_executor():
    global app_executor

    app_executor = ProcessPoolExecutor(
        max_workers=max_workers, mp_context=_mp_context, initializer=worker_initializer
    )


def shutdown_executor():
    global app_executor
    if app_executor:
        app_executor.shutdown(wait=True, cancel_futures=True)
        app_executor = None


atexit.register(shutdown_executor)


def get_executor():
    return app_executor
