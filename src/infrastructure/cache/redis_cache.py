"""
infrastructure.cache.redis_cache

Async Redis cache with the same interface as MemoryCache.
Falls back gracefully if Redis is unreachable — callers receive None on load().

Usage:
    cache = RedisCache("redis://localhost:6379")
    await cache.save("my_key", {"a": 1}, ttl=300)
    val = await cache.load("my_key")   # None after 300s or if Redis is down
    await cache.delete("my_key")
    await cache.close()
"""
from __future__ import annotations

import json
from core.utils.log_helper import getLogger
from typing import Any, Optional

logger = getLogger(__name__)


class RedisCache:

    def __init__(self, url: str = "redis://localhost:6379", prefix: str = "1m") -> None:
        self._url    = url
        self._prefix = prefix
        self._client = None

    async def _get_client(self):
        if self._client is None:
            import redis.asyncio as aioredis
            self._client = aioredis.from_url(self._url, decode_responses=True)
        return self._client

    def _key(self, key: str, category: str = "") -> str:
        parts = [self._prefix]
        if category:
            parts.append(category)
        parts.append(key)
        return ":".join(parts)

    async def save(
        self,
        key: str,
        value: Any,
        category: str = "",
        ttl: int = 0,          # seconds; 0 = no expiry
    ) -> None:
        try:
            r = await self._get_client()
            full_key = self._key(key, category)
            serialized = json.dumps(value)
            if ttl > 0:
                await r.setex(full_key, ttl, serialized)
            else:
                await r.set(full_key, serialized)
        except Exception as e:
            logger.warning("RedisCache.save failed (%s) — %s", key, e)

    async def load(self, key: str, category: str = "") -> Optional[Any]:
        try:
            r = await self._get_client()
            raw = await r.get(self._key(key, category))
            return json.loads(raw) if raw is not None else None
        except Exception as e:
            logger.warning("RedisCache.load failed (%s) — %s", key, e)
            return None

    async def delete(self, key: str, category: str = "") -> None:
        try:
            r = await self._get_client()
            await r.delete(self._key(key, category))
        except Exception as e:
            logger.warning("RedisCache.delete failed (%s) — %s", key, e)

    async def ttl(self, key: str, category: str = "") -> int:
        """Returns remaining TTL in seconds, -1 if no expiry, -2 if missing."""
        try:
            r = await self._get_client()
            return await r.ttl(self._key(key, category))
        except Exception:
            return -2

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
