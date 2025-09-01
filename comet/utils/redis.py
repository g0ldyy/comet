import json
import redis.asyncio as redis
from redis.exceptions import RedisError, ConnectionError, TimeoutError
from typing import Optional, Any, Union
from loguru import logger

from comet.utils.models import settings


class RedisClient:
    def __init__(self):
        self.redis = None
        self._connected = False
        self.cache_hits = 0
        self.cache_misses = 0

    def _sanitize_key_for_logging(self, key: str) -> str:
        """Sanitize key for safe logging without exposing sensitive data"""
        if not key:
            return "<empty_key>"
        
        # For logging, show key structure without sensitive values
        if len(key) > 50:
            return f"{key[:20]}...{key[-10:]}"
        return key

    async def connect(self):
        if not settings.ENABLE_REDIS:
            return False
        
        try:
            if settings.REDIS_URI:
                self.redis = redis.from_url(
                    settings.REDIS_URI, 
                    encoding='utf-8', 
                    decode_responses=True,
                    socket_connect_timeout=2,
                    socket_timeout=3,
                    retry_on_timeout=True,
                    health_check_interval=30
                )
            else:
                logger.error("REDIS_URI not configured")
                return False
            
            await self.redis.ping()
            self._connected = True
            info = await self.redis.info('server')
            logger.success(f"🔗 Redis connection established - Server: {info.get('redis_version', 'unknown')}")
            return True
            
        except (ConnectionError, TimeoutError) as e:
            logger.warning(f"Redis connection failed: {e}")
            self._connected = False
            return False
        except RedisError as e:
            logger.error(f"Redis error during connection: {e}")
            self._connected = False
            return False
        except Exception as e:
            logger.error(f"Unexpected error connecting to Redis: {e}")
            self._connected = False
            return False

    async def disconnect(self):
        if self.redis:
            if self.cache_hits + self.cache_misses > 0:
                hit_rate = (self.cache_hits / (self.cache_hits + self.cache_misses)) * 100
                logger.info(f"📊 Redis cache stats - Hits: {self.cache_hits}, Misses: {self.cache_misses}, Hit Rate: {hit_rate:.1f}%")
            await self.redis.aclose()
            self._connected = False
            logger.info("🔌 Redis connection closed")

    def is_connected(self) -> bool:
        return self._connected

    async def _ensure_connection(self) -> bool:
        """Ensure Redis connection is active, attempt reconnection if needed"""
        if self._connected:
            return True
        
        logger.info("Attempting Redis reconnection...")
        return await self.connect()

    async def get(self, key: str) -> Optional[Any]:
        if not await self._ensure_connection():
            return None
        
        try:
            value = await self.redis.get(key)
            if value is None:
                self.cache_misses += 1
                return None
            
            self.cache_hits += 1
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
                
        except (ConnectionError, TimeoutError) as e:
            logger.warning(f"Redis connection issue during GET for key {self._sanitize_key_for_logging(key)}: {e}")
            self._connected = False
            return None
        except RedisError as e:
            logger.error(f"Redis error during GET for key {self._sanitize_key_for_logging(key)}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error during Redis GET for key {self._sanitize_key_for_logging(key)}: {e}")
            return None

    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> bool:
        if not await self._ensure_connection():
            return False
        
        try:
            if isinstance(value, (dict, list, tuple)):
                value = json.dumps(value)
            elif not isinstance(value, str):
                value = str(value)
            
            if ttl and ttl > 0:
                await self.redis.setex(key, ttl, value)
            else:
                await self.redis.set(key, value)
            
            return True
            
        except (ConnectionError, TimeoutError) as e:
            logger.warning(f"Redis connection issue during SET for key {self._sanitize_key_for_logging(key)}: {e}")
            self._connected = False
            return False
        except RedisError as e:
            logger.error(f"Redis error during SET for key {self._sanitize_key_for_logging(key)}: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error during Redis SET for key {self._sanitize_key_for_logging(key)}: {e}")
            return False

    async def delete(self, key: str) -> bool:
        if not self.is_connected():
            return False
        
        try:
            await self.redis.delete(key)
            return True
            
        except (ConnectionError, TimeoutError) as e:
            logger.warning(f"Redis connection issue during DELETE for key {self._sanitize_key_for_logging(key)}: {e}")
            self._connected = False
            return False
        except RedisError as e:
            logger.error(f"Redis error during DELETE for key {self._sanitize_key_for_logging(key)}: {e}")
            return False

    async def exists(self, key: str) -> bool:
        if not self.is_connected():
            return False
        
        try:
            result = await self.redis.exists(key)
            return bool(result)
            
        except (ConnectionError, TimeoutError) as e:
            logger.warning(f"Redis connection issue during EXISTS for key {self._sanitize_key_for_logging(key)}: {e}")
            self._connected = False
            return False
        except RedisError as e:
            logger.error(f"Redis error during EXISTS for key {self._sanitize_key_for_logging(key)}: {e}")
            return False

    async def expire(self, key: str, ttl: int) -> bool:
        if not self.is_connected():
            return False
        
        try:
            await self.redis.expire(key, ttl)
            return True
            
        except (ConnectionError, TimeoutError) as e:
            logger.warning(f"Redis connection issue during EXPIRE for key {self._sanitize_key_for_logging(key)}: {e}")
            self._connected = False
            return False
        except RedisError as e:
            logger.error(f"Redis error during EXPIRE for key {self._sanitize_key_for_logging(key)}: {e}")
            return False

    async def get_stats(self) -> dict:
        if not self.is_connected():
            return {"connected": False}
        
        try:
            info = await self.redis.info()
            return {
                "connected": True,
                "redis_version": info.get('redis_version', 'unknown'),
                "used_memory": info.get('used_memory_human', 'unknown'),
                "keyspace_hits": info.get('keyspace_hits', 0),
                "keyspace_misses": info.get('keyspace_misses', 0),
                "connected_clients": info.get('connected_clients', 0),
                "total_commands_processed": info.get('total_commands_processed', 0),
                "app_cache_hits": self.cache_hits,
                "app_cache_misses": self.cache_misses,
                "app_hit_rate": (self.cache_hits / (self.cache_hits + self.cache_misses) * 100) if (self.cache_hits + self.cache_misses) > 0 else 0,
            }
        except (ConnectionError, TimeoutError) as e:
            logger.warning(f"Redis connection issue during STATS: {e}")
            self._connected = False
            return {"connected": False, "error": str(e)}
        except RedisError as e:
            logger.error(f"Redis error during STATS: {e}")
            return {"connected": False, "error": str(e)}


redis_client = RedisClient()