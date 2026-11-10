import asyncio
import ctypes
import gc

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


class MemoryTrimmer:
    def __init__(self) -> None:
        self._interval = 0.0
        self._configuration_changed = asyncio.Event()

    def reconfigure(self, config) -> None:
        self._interval = max(0.0, float(config.MEMORY_TRIM_INTERVAL))
        self._configuration_changed.set()

    async def run(self) -> None:
        while True:
            self._configuration_changed.clear()
            if self._interval <= 0:
                await self._configuration_changed.wait()
                continue
            try:
                await asyncio.wait_for(
                    self._configuration_changed.wait(),
                    timeout=self._interval,
                )
            except TimeoutError:
                try:
                    await asyncio.to_thread(trim_process_memory)
                except Exception:
                    pass


memory_trimmer = MemoryTrimmer()
