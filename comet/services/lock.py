import asyncio
import time
import uuid

from comet.core.database import database
from comet.core.logger import logger
from comet.core.models import settings


class DistributedLock:
    def __init__(self, lock_key: str, timeout: int = None, retry_interval: float = 0.5):
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
                await self._cleanup_expired_locks()
                loop_time = time.time()
                expires_at = int(loop_time + self.timeout)

                # If already acquired, refresh the lock
                if self.acquired:
                    query = "UPDATE scrape_locks SET expires_at = :expires_at, updated_at = :updated_at WHERE lock_key = :lock_key AND instance_id = :instance_id"
                    params = {
                        "lock_key": self.lock_key,
                        "instance_id": self.instance_id,
                        "updated_at": loop_time,
                        "expires_at": expires_at,
                    }

                    if await database.fetch_val(
                        query + " RETURNING 1",
                        params,
                        force_primary=True,
                    ):
                        return True

                    self.acquired = False

                # Attempt to acquire the lock
                inserted = await database.fetch_val(
                    """
                    INSERT INTO scrape_locks (lock_key, instance_id, updated_at, expires_at)
                    VALUES (:lock_key, :instance_id, :updated_at, :expires_at)
                    ON CONFLICT DO NOTHING
                    RETURNING 1
                    """,
                    {
                        "lock_key": self.lock_key,
                        "instance_id": self.instance_id,
                        "updated_at": loop_time,
                        "expires_at": expires_at,
                    },
                    force_primary=True,
                )

                if inserted == 1:
                    self.acquired = True
                    return True

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

    async def _cleanup_expired_locks(self):
        try:
            current_time = time.time()
            await database.execute(
                "DELETE FROM scrape_locks WHERE expires_at < :current_time",
                {"current_time": current_time},
            )
        except Exception as e:
            logger.log("LOCK", f"❌ Error cleaning up expired locks: {e}")

    async def __aenter__(self):
        success = await self.acquire()
        if not success:
            raise RuntimeError(f"Failed to acquire lock for {self.lock_key}")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.release()
