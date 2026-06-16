"""
infrastructure.cache.capital_session

Shared Capital.com session cache backed by Redis.

Capital.com sessions expire after 10 min of inactivity (540 s).
We cache with a 480 s TTL so the token is always refreshed before it expires.

Falls back to in-process MemoryCache if Redis is unreachable.

Usage:
    from infrastructure.cache.capital_session import get_capital_session, clear_capital_session

    cst, token = await get_capital_session(api_key, username, password, http_client)
    # cst / token are ready to use as auth headers
"""
from __future__ import annotations

from core.utils.log_helper import getLogger
import os
from typing import Optional, Tuple

import httpx

logger = getLogger(__name__)

_REDIS_URL   = os.environ.get("REDIS_URL", "redis://localhost:6379")
_SESSION_TTL = 480          # seconds — refresh before Capital's 540 s hard expiry
_CACHE_KEY   = "session"
_CATEGORY    = "capital"

# Module-level cache instance — shared across the whole process
_cache = None


def _get_cache():
    global _cache
    if _cache is None:
        try:
            from infrastructure.cache.redis_cache import RedisCache
            _cache = RedisCache(url=_REDIS_URL)
            logger.debug("CapitalSessionCache: using Redis (%s)", _REDIS_URL)
        except Exception as e:
            logger.warning("CapitalSessionCache: Redis unavailable (%s) — using MemoryCache", e)
            from infrastructure.cache.memory_cache import MemoryCache
            _cache = MemoryCache()
    return _cache


async def _load(cache) -> Optional[dict]:
    if hasattr(cache, "__aenter__") or asyncio_method(cache):
        return await cache.load(_CACHE_KEY, category=_CATEGORY)
    return cache.load(_CACHE_KEY, category=_CATEGORY)


async def _save(cache, value: dict) -> None:
    if asyncio_method(cache):
        await cache.save(_CACHE_KEY, value, category=_CATEGORY, ttl=_SESSION_TTL)
    else:
        cache.save(_CACHE_KEY, value, category=_CATEGORY,
                   metadata={"ttl": _SESSION_TTL})


async def _delete(cache) -> None:
    if asyncio_method(cache):
        await cache.delete(_CACHE_KEY, category=_CATEGORY)
    else:
        cache.delete(_CACHE_KEY, category=_CATEGORY)


def asyncio_method(obj) -> bool:
    """True if obj.load/save are coroutine functions (RedisCache), False for MemoryCache."""
    import asyncio, inspect
    return inspect.iscoroutinefunction(getattr(obj, "load", None))


async def get_capital_session(
    api_key: str,
    username: str,
    password: str,
    http: Optional[httpx.AsyncClient] = None,
    base_url: str = "https://api-capital.backend-capital.com",
) -> Tuple[str, str]:
    """
    Return (CST, X-SECURITY-TOKEN) for Capital.com.

    Reads from cache first. On a miss (or after 480 s) creates a fresh session
    and stores it. Raises RuntimeError if authentication fails.
    """
    cache = _get_cache()
    cached = await _load(cache)
    if cached:
        logger.debug("CapitalSessionCache: HIT (TTL ok)")
        return cached["cst"], cached["token"]

    logger.debug("CapitalSessionCache: MISS — creating new session")
    own_http = http is None
    if own_http:
        http = httpx.AsyncClient()
    try:
        resp = await http.post(
            f"{base_url}/api/v1/session",
            headers={"X-CAP-API-KEY": api_key, "Content-Type": "application/json"},
            json={"identifier": username, "password": password, "encryptedPassword": False},
            timeout=15.0,
        )
        if resp.is_error:
            raise RuntimeError(f"Capital.com session failed: HTTP {resp.status_code} — {resp.text[:200]}")

        cst   = resp.headers.get("CST", "")
        token = resp.headers.get("X-SECURITY-TOKEN", "")
        if not cst or not token:
            raise RuntimeError("Capital.com session: missing CST or X-SECURITY-TOKEN in response")

        await _save(cache, {"cst": cst, "token": token})
        logger.info("CapitalSessionCache: new session stored (TTL %ds)", _SESSION_TTL)
        return cst, token
    finally:
        if own_http:
            await http.aclose()


async def clear_capital_session() -> None:
    """Force-expire the cached session (e.g. after a 401 to trigger re-auth)."""
    cache = _get_cache()
    await _delete(cache)
    logger.info("CapitalSessionCache: cleared")
