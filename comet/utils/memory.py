import asyncio
import ctypes
import gc
import os
import sys
from ctypes.util import find_library
from functools import lru_cache
from typing import Optional

from comet.core.logger import logger


def _env_contains(name: str, needle: str) -> bool:
    value = os.environ.get(name, "")
    if not value:
        return False
    return needle.lower() in value.lower()


@lru_cache(maxsize=1)
def _is_mimalloc_active() -> bool:
    return _env_contains("PYTHONMALLOC", "mimalloc") or _env_contains(
        "LD_PRELOAD", "mimalloc"
    )


@lru_cache(maxsize=1)
def _get_mimalloc_collect():
    candidates: list[Optional[str]] = [None]
    if sys.platform == "win32":
        candidates.extend(("mimalloc.dll", "mimalloc-redirect.dll"))
    elif sys.platform == "darwin":
        candidates.extend(
            (
                "libmimalloc.dylib",
                "libmimalloc.2.dylib",
                "libmimalloc-secure.dylib",
                "libmimalloc-secure.2.dylib",
            )
        )
    else:
        candidates.extend(
            (
                "libmimalloc.so",
                "libmimalloc.so.2",
                "libmimalloc-secure.so",
                "libmimalloc-secure.so.2",
            )
        )

    for candidate in candidates:
        try:
            library = ctypes.CDLL(candidate) if candidate else ctypes.CDLL(None)
            collect = getattr(library, "mi_collect", None)
            if collect is None:
                continue
            collect.argtypes = [ctypes.c_bool]
            collect.restype = None
            return collect
        except Exception:
            continue
    return None


@lru_cache(maxsize=1)
def _get_linux_malloc_trim():
    if sys.platform != "linux":
        return None

    candidates = []
    libc_name = find_library("c")
    if libc_name:
        candidates.append(libc_name)
    candidates.append(None)

    for candidate in candidates:
        try:
            library = ctypes.CDLL(candidate) if candidate else ctypes.CDLL(None)
            malloc_trim = getattr(library, "malloc_trim", None)
            if malloc_trim is None:
                continue
            malloc_trim.argtypes = [ctypes.c_size_t]
            malloc_trim.restype = ctypes.c_int
            return malloc_trim
        except Exception:
            continue
    return None


def _trim_with_mimalloc(*, aggressive: bool) -> bool:
    collect = _get_mimalloc_collect()
    if collect is None:
        return False
    try:
        collect(bool(aggressive))
        return True
    except Exception:
        return False


def _trim_with_libc() -> bool:
    if sys.platform == "linux":
        malloc_trim = _get_linux_malloc_trim()
        if malloc_trim is None:
            return False
        try:
            malloc_trim(0)
            return True
        except Exception:
            return False

    if sys.platform == "win32":
        try:
            process = ctypes.windll.kernel32.GetCurrentProcess()
            return bool(ctypes.windll.psapi.EmptyWorkingSet(process))
        except Exception:
            return False

    return False


def trim_process_memory(
    *,
    collect_garbage: bool = True,
    aggressive: bool = True,
) -> bool:
    if collect_garbage:
        gc.collect()

    if _is_mimalloc_active() and _trim_with_mimalloc(aggressive=aggressive):
        return True

    return _trim_with_libc()


async def periodic_memory_trim(interval_seconds: float | int | None) -> None:
    interval = max(0.0, float(interval_seconds or 0))
    if interval <= 0:
        return

    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(trim_process_memory, aggressive=False)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Periodic memory trim failed")
