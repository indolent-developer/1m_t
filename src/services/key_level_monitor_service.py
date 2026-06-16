"""
services.key_level_monitor_service

Monitors a set of symbols against automatically computed key technical levels
and emits PriceLevelEvent on the bus when price breaks, bounces, or rejects.

Key levels tracked
------------------
  Static (loaded once at start of day):
    • Previous day high / low
    • Floor pivot points: PP, R1, R2, R3, S1, S2, S3
    • 1H support & resistance (swing highs/lows, last 5 days)
    • 1D support & resistance (swing highs/lows, last 60 days)

  Dynamic (refreshed every 5 minutes):
    • HOD-5min-ago  — intraday high excluding the current 5min bar
    • LOD-5min-ago  — intraday low  excluding the current 5min bar

Break detection is handled by LevelTracker (one per level per symbol), the same
state machine used by PriceStateManager. Events emitted on the bus:
    LevelEvent.BREAK_ABOVE, BREAK_BELOW, BOUNCE, REJECTION, FALSE_BREAK

Downstream consumers:
    bus.subscribe(LevelEvent.BREAK_ABOVE, my_handler)
    bus.subscribe_all(my_handler)

Usage
-----
    history = PriceHistoryService(fmp_fetcher, redis_cache)
    monitor = KeyLevelMonitorService(
        symbols=["AAPL", "TSLA", "NVDA"],
        bus=bus,
        history_service=history,
        fetcher=fmp_fetcher,
    )
    await monitor.start()
    ...
    await monitor.stop()
"""
from __future__ import annotations

import asyncio
from core.utils.log_helper import getLogger
from typing import Optional

from adapters.brokers.entities.broker_event import BrokerEvent
from adapters.brokers.entities.broker_event_payload import BrokerEventPayload
from core.adapters.base_subscriber import BaseSubscriber
from core.adapters.event_bus import IEventBus
from core.entities.level_event import PriceLevelEvent
from core.entities.market_data import PriceTick
from core.entities.time_frame import TimeFrame
from data_fetchers.data_fetcher_base import DataFetcherBase
from services.indicator_provider import IndicatorProvider
from services.key_level_service import KeyLevelService, KeyLevels
from services.level_tracker import LevelTracker
from services.price_history_service import PriceHistoryService

logger = getLogger(__name__)


class KeyLevelMonitorService(BaseSubscriber):
    """
    Subscribes to QUOTE_UPDATE ticks and emits PriceLevelEvent when price
    meaningfully interacts with any computed key level.

    Static levels (prev day H/L, pivots, 1H/1D S&R) are loaded once at startup.
    HOD/LOD-5min-ago trackers are replaced every `hod_lod_refresh_seconds`.
    """

    def __init__(
        self,
        symbols: list[str],
        bus: IEventBus,
        history_service: PriceHistoryService,
        fetcher: DataFetcherBase,
        # ATR band timeframe for LevelTracker zone width
        band_timeframe: TimeFrame = TimeFrame.MINUTE_5,
        band_mult: float = 0.3,
        atr_period: int = 14,
        # LevelTracker confirmation params
        dwell_seconds: int = 120,
        break_confirm_seconds: int = 60,
        break_confirm_ticks: int = 3,
        break_max_zone_dwell_seconds: int = 300,
        false_break_window_seconds: int = 180,
        false_break_confirm_seconds: int = 15,
        false_break_confirm_ticks: int = 2,
        # HOD/LOD refresh cadence
        hod_lod_refresh_seconds: int = 300,
    ) -> None:
        super().__init__(bus)
        self._symbols = list(symbols)
        self._kl_service = KeyLevelService(history_service)
        self._provider   = IndicatorProvider(
            fetcher=fetcher,
            symbols=self._symbols,
            timeframe=band_timeframe,
            default_period=atr_period,
        )
        self._tracker_cfg = dict(
            band_mult=band_mult,
            atr_period=atr_period,
            dwell_seconds=dwell_seconds,
            break_confirm_seconds=break_confirm_seconds,
            break_confirm_ticks=break_confirm_ticks,
            break_max_zone_dwell_seconds=break_max_zone_dwell_seconds,
            false_break_window_seconds=false_break_window_seconds,
            false_break_confirm_seconds=false_break_confirm_seconds,
            false_break_confirm_ticks=false_break_confirm_ticks,
        )
        self._hod_lod_refresh = hod_lod_refresh_seconds

        # Populated in start()
        self._static_trackers:  dict[str, list[LevelTracker]] = {}
        self._hod_lod_trackers: dict[str, list[LevelTracker]] = {}
        self._key_levels:       dict[str, KeyLevels]          = {}
        self._label_map:        dict[str, dict[float, str]]   = {}

        self._hod_lod_task:   Optional[asyncio.Task] = None
        self._provider_task:  Optional[asyncio.Task] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        logger.info(
            "KeyLevelMonitorService: loading ATR data + key levels for %d symbols...",
            len(self._symbols),
        )
        await self._provider.load()
        await self._load_all_levels()

        n_static  = sum(len(v) for v in self._static_trackers.values())
        n_hod_lod = sum(len(v) for v in self._hod_lod_trackers.values())
        logger.info(
            "KeyLevelMonitorService: ready — %d static + %d HOD/LOD trackers across %d symbols",
            n_static, n_hod_lod, len(self._symbols),
        )

        self._subscribe(BrokerEvent.QUOTE_UPDATE, self._on_tick)
        self._hod_lod_task  = asyncio.create_task(self._hod_lod_refresh_loop())
        self._provider_task = asyncio.create_task(self._provider.refresh_loop())

    async def stop(self) -> None:
        self.detach()
        for task in (self._hod_lod_task, self._provider_task):
            if task:
                task.cancel()
        logger.info("KeyLevelMonitorService: stopped")

    # ── Dynamic symbol management ─────────────────────────────────────────────

    async def add_symbol(self, symbol: str) -> None:
        if symbol in self._symbols:
            return
        levels = await self._kl_service.compute_levels(symbol)
        self._key_levels[symbol]       = levels
        self._static_trackers[symbol]  = self._make_trackers(symbol, levels.static_levels())
        self._hod_lod_trackers[symbol] = self._make_trackers(symbol, levels.hod_lod_levels())
        self._label_map[symbol]        = levels.labels()
        self._provider._symbols.append(symbol)
        await self._provider.load()
        self._symbols.append(symbol)
        logger.info("KeyLevelMonitorService: added %s (%d static levels)", symbol, len(levels.static_levels()))

    async def remove_symbol(self, symbol: str) -> None:
        if symbol not in self._symbols:
            return
        self._symbols.remove(symbol)
        self._static_trackers.pop(symbol, None)
        self._hod_lod_trackers.pop(symbol, None)
        self._key_levels.pop(symbol, None)
        self._label_map.pop(symbol, None)
        if symbol in self._provider._symbols:
            self._provider._symbols.remove(symbol)
        logger.info("KeyLevelMonitorService: removed %s", symbol)

    # ── Accessors ─────────────────────────────────────────────────────────────

    def get_levels(self, symbol: str) -> Optional[KeyLevels]:
        return self._key_levels.get(symbol)

    def get_label(self, symbol: str, level_value: float) -> str:
        return self._label_map.get(symbol, {}).get(round(level_value, 4), "unknown")

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _load_all_levels(self) -> None:
        levels_map = await self._kl_service.compute_levels_multi(self._symbols)
        for symbol, levels in levels_map.items():
            self._key_levels[symbol]       = levels
            self._static_trackers[symbol]  = self._make_trackers(symbol, levels.static_levels())
            self._hod_lod_trackers[symbol] = self._make_trackers(symbol, levels.hod_lod_levels())
            self._label_map[symbol]        = levels.labels()

    def _make_trackers(self, symbol: str, levels: list[float]) -> list[LevelTracker]:
        return [
            LevelTracker(
                symbol=symbol,
                level=lvl,
                indicator_provider=self._provider,
                **self._tracker_cfg,
            )
            for lvl in levels
        ]

    async def _on_tick(self, payload: BrokerEventPayload) -> None:
        if not isinstance(payload.data, PriceTick):
            return
        tick   = payload.data
        labels = self._label_map.get(tick.symbol, {})
        trackers = (
            self._static_trackers.get(tick.symbol, []) +
            self._hod_lod_trackers.get(tick.symbol, [])
        )
        for tracker in trackers:
            for evt in tracker.update(tick):
                _log_event(evt, labels)
                await self._bus.emit(evt)

    async def _hod_lod_refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(self._hod_lod_refresh)
            for symbol in list(self._symbols):
                try:
                    existing = self._key_levels.get(symbol)
                    if existing is None:
                        continue

                    old_hod = existing.hod_5min_ago
                    old_lod = existing.lod_5min_ago

                    updated = await self._kl_service.refresh_hod_lod(symbol, existing)

                    hod_moved = abs(updated.hod_5min_ago - old_hod) > 0.001
                    lod_moved = abs(updated.lod_5min_ago - old_lod) > 0.001

                    if not hod_moved and not lod_moved:
                        # Level unchanged — keep existing trackers so in-progress
                        # dwell/confirmation state is not thrown away.
                        logger.debug(
                            "KeyLevelMonitorService: %s HOD/LOD unchanged (%.4f / %.4f)",
                            symbol, updated.hod_5min_ago, updated.lod_5min_ago,
                        )
                        continue

                    self._hod_lod_trackers[symbol] = self._make_trackers(
                        symbol, updated.hod_lod_levels()
                    )
                    self._label_map[symbol] = updated.labels()
                    hod_str = f"{old_hod:.4f} → {updated.hod_5min_ago:.4f}" if hod_moved else f"{old_hod:.4f}"
                    lod_str = f"{old_lod:.4f} → {updated.lod_5min_ago:.4f}" if lod_moved else f"{old_lod:.4f}"
                    logger.info(
                        "KeyLevelMonitorService: %s HOD/LOD updated  HOD=%s  LOD=%s",
                        symbol, hod_str, lod_str,
                    )
                except Exception as exc:
                    logger.warning(
                        "KeyLevelMonitorService: HOD/LOD refresh failed for %s: %s",
                        symbol, exc,
                    )


# ── Logging helper ────────────────────────────────────────────────────────────

def _log_event(evt: PriceLevelEvent, labels: dict[float, str]) -> None:
    label = labels.get(round(evt.level, 4), "")
    label_str = f"  [{label}]" if label else ""
    extra = f"  orig={evt.original_break.value}" if evt.original_break else ""
    dwell = f"  dwell={evt.dwell_seconds:.0f}s"  if evt.dwell_seconds  else ""
    logger.info(
        "%-13s  %s  level=%.4f%s  price=%.4f  zone=[%.4f–%.4f]  "
        "atr=%.4f  convincing=%s%s%s",
        f"[{evt.event.value}]", evt.symbol, evt.level, label_str,
        evt.price, evt.zone_lo, evt.zone_hi,
        evt.atr, evt.convincing, dwell, extra,
    )
