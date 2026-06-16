"""
infrastructure.cache.memory_cache — Simple in-process TTL cache.

Lightweight dict-based cache with optional TTL.
Used as a drop-in when Redis is not available.
"""
from __future__ import annotations

import time
from typing import Any, Optional


class MemoryCache:

    def __init__(self) -> None:
        self._store: dict[str, tuple[Any, float]] = {}  # key → (value, expires_at)

    def save(
        self,
        key: str,
        value: Any,
        category: str = "",
        metadata: dict | None = None,
    ) -> None:
        ttl = (metadata or {}).get("ttl", 0)
        expires_at = time.time() + ttl if ttl > 0 else float("inf")
        full_key = f"{category}:{key}" if category else key
        self._store[full_key] = (value, expires_at)

    def load(self, key: str, category: str = "") -> Optional[Any]:
        full_key = f"{category}:{key}" if category else key
        entry = self._store.get(full_key)
        if entry is None:
            return None
        value, expires_at = entry
        if time.time() > expires_at:
            del self._store[full_key]
            return None
        return value

    def delete(self, key: str, category: str = "") -> None:
        full_key = f"{category}:{key}" if category else key
        self._store.pop(full_key, None)

    def clear(self) -> None:
        self._store.clear()
