import asyncio
import time
import uuid
from collections.abc import Awaitable
from typing import TypeVar

from comet.core.database import database as default_database
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
        database=None,
    ):
        """
        Distributed lock system to prevent concurrent scraping.

        Args:
            lock_key: Unique key to identify the lock (e.g. media_id)
            timeout: Lock lifetime in seconds (None = uses SCRAPE_LOCK_TTL)
            retry_interval: Interval between acquisition attempts in seconds
        """
        self.lock_key = lock_key
        self.timeout = settings.SCRAPE_LOCK_TTL if timeout is None else timeout
        self.retry_interval = retry_interval
        self.database = default_database if database is None else database
        self.instance_id = str(uuid.uuid4())
        self.acquired = False

    async def acquire(self, wait_timeout: float | None = None):
        deadline = None if wait_timeout is None else time.monotonic() + wait_timeout

        while True:
            current_time = time.time()
            expires_at = current_time + self.timeout
            acquired = (
                await self.database.fetch_one(
                    _ACQUIRE_OR_REFRESH_LOCK_QUERY,
                    {
                        "lock_key": self.lock_key,
                        "instance_id": self.instance_id,
                        "updated_at": current_time,
                        "expires_at": expires_at,
                    },
                    force_primary=True,
                )
                is not None
            )

            if acquired:
                self.acquired = True
                return True

            self.acquired = False

            if wait_timeout is None:
                return False

            if time.monotonic() >= deadline:
                return False

            await asyncio.sleep(self.retry_interval)

    async def _renew_until_lost(self) -> None:
        while self.acquired:
            await asyncio.sleep(self.timeout / 2)
            if not await self.acquire():
                self.acquired = False
                return

    async def run(self, operation: Awaitable[_T]) -> _T:
        if not self.acquired:
            raise RuntimeError("distributed lock is not acquired")

        operation_task = asyncio.ensure_future(operation)
        renewal_task = asyncio.create_task(
            self._renew_until_lost(),
            name="distributed-lock-renewal",
        )
        try:
            done, _ = await asyncio.wait(
                (operation_task, renewal_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if renewal_task in done:
                await renewal_task
                if not self.acquired:
                    operation_task.cancel()
                    await asyncio.gather(operation_task, return_exceptions=True)
                    raise RuntimeError("Lost distributed lock")
            return await operation_task
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
            await self.database.execute(
                "DELETE FROM scrape_locks WHERE lock_key = :lock_key AND instance_id = :instance_id",
                {"lock_key": self.lock_key, "instance_id": self.instance_id},
            )
        finally:
            self.acquired = False

    async def __aenter__(self):
        success = await self.acquire()
        if not success:
            raise RuntimeError("failed to acquire distributed lock")
        return self

    async def __aexit__(self, *_args):
        await self.release()
