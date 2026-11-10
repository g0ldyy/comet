import asyncio
import ctypes
import gc

from comet.core.logger import logger

# glibc only: jemalloc (Docker image) purges in its background thread, so the
# libc symbol is a no-op there, and other platforms simply lack it.
try:
    _malloc_trim = ctypes.CDLL(None).malloc_trim
except (AttributeError, OSError, TypeError):
    _malloc_trim = None


def trim_process_memory():
    gc.collect()
    if _malloc_trim is not None:
        _malloc_trim(0)


async def periodic_memory_trim(interval_seconds: int):
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await asyncio.to_thread(trim_process_memory)
        except Exception:
            logger.exception("Periodic memory trim failed")
