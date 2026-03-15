import asyncio
import ctypes
import gc
import sys
from functools import lru_cache


@lru_cache(maxsize=1)
def _get_linux_libc():
    return ctypes.CDLL("libc.so.6")


def trim_process_memory(*, collect_garbage: bool = True) -> bool:
    if collect_garbage:
        gc.collect()

    try:
        if sys.platform == "linux":
            return bool(_get_linux_libc().malloc_trim(0))
        if sys.platform == "win32":
            process = ctypes.windll.kernel32.GetCurrentProcess()
            return bool(ctypes.windll.psapi.EmptyWorkingSet(process))
    except Exception:
        return False

    return False


async def periodic_memory_trim(interval_seconds: float | int | None) -> None:
    interval = max(0.0, float(interval_seconds or 0))
    if interval <= 0:
        return

    while True:
        await asyncio.sleep(interval)
        trim_process_memory()
