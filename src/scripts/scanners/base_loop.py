"""
scripts.scanners.base_loop — BaseScannerLoop

Abstract base for all continuous scanner loops. Subclasses implement scan()
and receive per-symbol TTL deduplication and event emission for free.

Flow per interval:
    1. scan() returns all current hits (even previously seen ones)
    2. Base filters out hits within TTL window for the same (scanner, symbol)
    3. New hits are emitted on the bus as ScannerHit payloads

Seen-state is persisted in Redis (when a cache is provided) so restarts
do not re-alert symbols that were already alerted today.
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Dict, Optional

from core.adapters.event_bus import IEventBus
from core.entities.scanner_event import ScannerHit
from core.utils.log_helper import getLogger

if TYPE_CHECKING:
    from infrastructure.cache.redis_cache import RedisCache

logger = getLogger(__name__)

_REDIS_CATEGORY = "scanner_seen"


class BaseScannerLoop(ABC):

    def __init__(
        self,
        bus: IEventBus,
        interval_seconds: int = 60,
        ttl_seconds: int = 3600,
        cache: Optional["RedisCache"] = None,
    ) -> None:
        self._bus      = bus
        self._interval = interval_seconds
        self._ttl      = ttl_seconds
        self._cache    = cache
        self._seen:    Dict[str, datetime] = {}
        self._running  = False

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    async def scan(self) -> list[ScannerHit]:
        """Run one scan cycle. Return all current hits; dedup is handled here."""
        ...

    async def run(self) -> None:
        await self._restore_seen()
        self._running = True
        logger.info("[%s] started  interval=%ds  ttl=%ds", self.name, self._interval, self._ttl)
        while self._running:
            try:
                hits = await self.scan()
                new_count = 0
                for hit in hits:
                    if self._is_new(hit.symbol):
                        self._seen[hit.symbol] = datetime.now(timezone.utc)
                        await self._persist_seen(hit.symbol)
                        await self._bus.emit(hit)
                        new_count += 1
                        logger.info("[%s] NEW  %s  $%.2f  chg=%.2f%%", self.name, hit.symbol, hit.price, hit.change_pct)
                if hits:
                    logger.debug("[%s] %d hits  %d new", self.name, len(hits), new_count)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("[%s] scan error", self.name)
            await asyncio.sleep(self._interval)
        logger.info("[%s] stopped", self.name)

    def stop(self) -> None:
        self._running = False

    def _is_new(self, symbol: str) -> bool:
        last = self._seen.get(symbol)
        if last is None:
            return True
        return (datetime.now(timezone.utc) - last).total_seconds() > self._ttl

    # ── Redis persistence ─────────────────────────────────────────────────────

    async def _restore_seen(self) -> None:
        if self._cache is None:
            return
        try:
            data = await self._cache.load(self.name, category=_REDIS_CATEGORY)
            if isinstance(data, dict):
                for symbol, ts_str in data.items():
                    try:
                        self._seen[symbol] = datetime.fromisoformat(ts_str)
                    except ValueError:
                        pass
                logger.info("[%s] restored %d seen symbols from Redis", self.name, len(self._seen))
        except Exception as e:
            logger.warning("[%s] could not restore seen state: %s", self.name, e)

    async def _persist_seen(self, symbol: str) -> None:
        if self._cache is None:
            return
        try:
            data = {s: t.isoformat() for s, t in self._seen.items()}
            await self._cache.save(self.name, data, category=_REDIS_CATEGORY, ttl=self._ttl)
        except Exception as e:
            logger.warning("[%s] could not persist seen state: %s", self.name, e)
