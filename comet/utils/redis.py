import json
import redis.asyncio as redis
from typing import Optional, Any, Union
from loguru import logger

from comet.utils.models import settings


class RedisClient:
    def __init__(self):
        self.redis = None
        self._connected = False
        self.cache_hits = 0
        self.cache_misses = 0

    async def connect(self):
        if not settings.ENABLE_REDIS:
            return False
        
        try:
            if settings.REDIS_URI:
                self.redis = redis.from_url(
                    settings.REDIS_URI, 
                    encoding='utf-8', 
                    decode_responses=True
                )
            else:
                logger.error("REDIS_URI not configured")
                return False
            
            await self.redis.ping()
            self._connected = True
            info = await self.redis.info('server')
            logger.success(f"🔗 Redis connection established - Server: {info.get('redis_version', 'unknown')}")
            return True
            
        except Exception as e:
            logger.warning(f"Failed to connect to Redis: {e}")
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

    async def get(self, key: str) -> Optional[Any]:
        if not self.is_connected():
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
                
        except Exception as e:
            logger.error(f"Redis GET error for key {key}: {e}")
            return None

    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> bool:
        if not self.is_connected():
            return False
        
        try:
            if isinstance(value, (dict, list, tuple)):
                value = json.dumps(value)
            elif not isinstance(value, str):
                value = str(value)
            
            if ttl:
                await self.redis.setex(key, ttl, value)
            else:
                await self.redis.set(key, value)
            
            return True
            
        except Exception as e:
            logger.error(f"Redis SET error for key {key}: {e}")
            return False

    async def delete(self, key: str) -> bool:
        if not self.is_connected():
            return False
        
        try:
            await self.redis.delete(key)
            return True
            
        except Exception as e:
            logger.error(f"Redis DELETE error for key {key}: {e}")
            return False

    async def exists(self, key: str) -> bool:
        if not self.is_connected():
            return False
        
        try:
            result = await self.redis.exists(key)
            return bool(result)
            
        except Exception as e:
            logger.error(f"Redis EXISTS error for key {key}: {e}")
            return False

    async def expire(self, key: str, ttl: int) -> bool:
        if not self.is_connected():
            return False
        
        try:
            await self.redis.expire(key, ttl)
            return True
            
        except Exception as e:
            logger.error(f"Redis EXPIRE error for key {key}: {e}")
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
        except Exception as e:
            logger.error(f"Redis STATS error: {e}")
            return {"connected": False, "error": str(e)}


redis_client = RedisClient()