import asyncio
import time
import uuid
from collections.abc import Awaitable
from typing import TypeVar

from comet.core.database import database, fetch_flag
from comet.core.logger import logger
from comet.core.models import settings

_ACQUIRE_OR_REFRESH_LOCK_QUERY = """
    INSERT INTO scrape_locks (lock_key, instance_id, updated_at, expires_at)
    VALUES (:lock_key, :instance_id, :updated_at, :expires_at)
    ON CONFLICT (lock_key) DO UPDATE SET
        instance_id = EXCLUDED.instance_id,
        updated_at = EXCLUDED.updated_at,
        expires_at = EXCLUDED.expires_at
    WHERE scrape_locks.expires_at < :updated_at
       OR scrape_locks.instance_id = EXCLUDED.instance_id
    RETURNING 1
"""
_T = TypeVar("_T")


class DistributedLock:
    def __init__(
        self,
        lock_key: str,
        timeout: float | None = None,
        retry_interval: float = 0.5,
    ):
        """
        Distributed lock system to prevent concurrent scraping.

        Args:
            lock_key: Unique key to identify the lock (e.g. media_id)
            timeout: Lock lifetime in seconds (None = uses SCRAPE_LOCK_TTL)
            retry_interval: Interval between acquisition attempts in seconds
        """
        self.lock_key = lock_key
        self.timeout = timeout if timeout else settings.SCRAPE_LOCK_TTL
        self.retry_interval = retry_interval
        self.instance_id = str(uuid.uuid4())
        self.acquired = False

    async def acquire(self, wait_timeout: int = None):
        start_time = time.time()

        while True:
            try:
                loop_time = time.time()
                expires_at = int(loop_time + self.timeout)
                acquired = await fetch_flag(
                    _ACQUIRE_OR_REFRESH_LOCK_QUERY,
                    {
                        "lock_key": self.lock_key,
                        "instance_id": self.instance_id,
                        "updated_at": loop_time,
                        "expires_at": expires_at,
                    },
                    force_primary=True,
                )

                if acquired:
                    self.acquired = True
                    return True

                self.acquired = False

                # If we don't want to wait
                if wait_timeout is None:
                    return False

                # Check if wait timeout is exceeded
                if wait_timeout > 0 and (loop_time - start_time) >= wait_timeout:
                    logger.log(
                        "LOCK", f"⏰ Lock acquisition timeout for {self.lock_key}"
                    )
                    return False

                # Wait before retrying
                await asyncio.sleep(self.retry_interval)

            except Exception as e:
                logger.log("LOCK", f"❌ Error acquiring lock for {self.lock_key}: {e}")
                return False

    async def _renew_until_lost(self) -> None:
        while self.acquired:
            await asyncio.sleep(self.timeout / 2)
            if not await self.acquire():
                return

    async def run(self, operation: Awaitable[_T]) -> _T:
        if not self.acquired:
            raise RuntimeError(f"Lock is not acquired for {self.lock_key}")

        operation_task = asyncio.ensure_future(operation)
        renewal_task = asyncio.create_task(
            self._renew_until_lost(),
            name=f"distributed-lock-renewal:{self.lock_key}",
        )
        try:
            done, _ = await asyncio.wait(
                (operation_task, renewal_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if operation_task in done:
                return await operation_task

            operation_task.cancel()
            await asyncio.gather(operation_task, return_exceptions=True)
            raise RuntimeError(f"Lost distributed lock for {self.lock_key}")
        finally:
            for task in (renewal_task, operation_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                renewal_task,
                operation_task,
                return_exceptions=True,
            )

    async def release(self):
        if not self.acquired:
            return

        try:
            await database.execute(
                "DELETE FROM scrape_locks WHERE lock_key = :lock_key AND instance_id = :instance_id",
                {"lock_key": self.lock_key, "instance_id": self.instance_id},
            )
            self.acquired = False
        except Exception as e:
            logger.log("LOCK", f"❌ Error releasing lock for {self.lock_key}: {e}")

    async def __aenter__(self):
        success = await self.acquire()
        if not success:
            raise RuntimeError(f"Failed to acquire lock for {self.lock_key}")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.release()
