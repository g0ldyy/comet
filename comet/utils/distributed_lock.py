import asyncio
import time
import uuid
from typing import Optional
from comet.utils.models import database, settings
from comet.utils.logger import logger


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

    async def acquire(self, wait_timeout: Optional[int] = None) -> bool:
        """
        Attempts to acquire the lock.

        Args:
            wait_timeout: Maximum wait time in seconds (None = no waiting)

        Returns:
            True if lock was acquired, False otherwise
        """
        start_time = time.time()
        expires_at = int(time.time() + self.timeout)

        while True:
            try:
                # Clean up expired locks
                await self._cleanup_expired_locks()

                # Attempt to acquire the lock
                result = await database.execute(
                    f"""
                    INSERT {"OR IGNORE " if settings.DATABASE_TYPE == "sqlite" else ""}
                    INTO scrape_locks (lock_key, instance_id, timestamp, expires_at)
                    VALUES (:lock_key, :instance_id, :timestamp, :expires_at)
                    {" ON CONFLICT DO NOTHING" if settings.DATABASE_TYPE == "postgresql" else ""}
                    """,
                    {
                        "lock_key": self.lock_key,
                        "instance_id": self.instance_id,
                        "timestamp": int(time.time()),
                        "expires_at": expires_at,
                    },
                )

                # Check if we successfully acquired the lock
                if settings.DATABASE_TYPE == "sqlite":
                    success = result > 0
                else:  # PostgreSQL
                    # For PostgreSQL, check if our instance owns the lock
                    row = await database.fetch_one(
                        "SELECT instance_id FROM scrape_locks WHERE lock_key = :lock_key",
                        {"lock_key": self.lock_key},
                    )
                    success = row and row["instance_id"] == self.instance_id

                if success:
                    self.acquired = True
                    logger.log(
                        "LOCK",
                        f"🔒 Lock acquired for {self.lock_key} by {self.instance_id[:8]}",
                    )
                    return True

                # If we don't want to wait
                if wait_timeout is None:
                    return False

                # Check if wait timeout is exceeded
                if wait_timeout > 0 and (time.time() - start_time) >= wait_timeout:
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
            logger.log(
                "LOCK",
                f"🔓 Lock released for {self.lock_key} by {self.instance_id[:8]}",
            )
        except Exception as e:
            logger.log("LOCK", f"❌ Error releasing lock for {self.lock_key}: {e}")

    async def _cleanup_expired_locks(self):
        try:
            current_time = int(time.time())
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


async def is_scrape_in_progress(media_id: str) -> bool:
    try:
        # Clean up expired locks first
        current_time = int(time.time())
        await database.execute(
            "DELETE FROM scrape_locks WHERE expires_at < :current_time",
            {"current_time": current_time},
        )

        # Check if a lock exists for this media_id
        row = await database.fetch_one(
            "SELECT instance_id FROM scrape_locks WHERE lock_key = :lock_key",
            {"lock_key": media_id},
        )
        return row is not None
    except Exception as e:
        logger.log("LOCK", f"❌ Error checking scrape status for {media_id}: {e}")
        return False
