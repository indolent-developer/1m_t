"""
services.price_state_manager

Subscribes to QUOTE_UPDATE price ticks and emits PriceLevelEvent on the
bus when price interacts meaningfully with a defined level.

Downstream subscribers listen to specific level events:
    bus.subscribe(LevelEvent.BOUNCE,      my_handler)
    bus.subscribe(LevelEvent.BREAK_ABOVE, my_handler)
    bus.subscribe(LevelEvent.FALSE_BREAK, my_handler)
    bus.subscribe_all(my_handler)  # all events

Usage:
    fmp     = FmpDataFetcher({"api_key": os.environ["FMP_API_KEY"]})
    manager = PriceStateManager(
        levels  = {"AAPL": [195.0, 200.0], "TSLA": [410.0, 420.0]},
        bus     = bus,
        fmp_fetcher = fmp,
    )
    await manager.start()
    ...
    await manager.stop()
"""
from __future__ import annotations

import asyncio
from core.utils.log_helper import getLogger
from typing import Dict, List, Optional

from adapters.brokers.entities.broker_event import BrokerEvent
from adapters.brokers.entities.broker_event_payload import BrokerEventPayload
from core.adapters.base_subscriber import BaseSubscriber
from core.adapters.event_bus import IEventBus
from core.entities.level_event import LevelEvent
from core.entities.market_data import PriceTick
from core.entities.time_frame import TimeFrame
from services.indicator_provider import IndicatorProvider
from services.level_tracker import LevelTracker
from services.price_history_service import PriceHistoryService

logger = getLogger(__name__)


class PriceStateManager(BaseSubscriber):

    def __init__(
        self,
        levels: Dict[str, List[float]],
        bus: IEventBus,
        history: PriceHistoryService,
        atr_period: int = 14,
        band_timeframe: TimeFrame = TimeFrame.MINUTE_5,
        band_mult: float = 0.3,
        dwell_seconds: int = 120,
        break_confirm_seconds: int = 60,
        break_confirm_ticks: int = 3,
        break_max_zone_dwell_seconds: int = 300,
        false_break_window_seconds: int = 180,
        false_break_confirm_seconds: int = 15,
        false_break_confirm_ticks: int = 2,
    ) -> None:
        super().__init__(bus)

        self._provider = IndicatorProvider(
            history=history,
            symbols=list(levels.keys()),
            timeframe=band_timeframe,
            default_period=atr_period,
        )
        self._trackers: Dict[str, List[LevelTracker]] = {
            symbol: [
                LevelTracker(
                    symbol=symbol,
                    level=lvl,
                    indicator_provider=self._provider,
                    band_mult=band_mult,
                    dwell_seconds=dwell_seconds,
                    atr_period=atr_period,
                    break_confirm_seconds=break_confirm_seconds,
                    break_confirm_ticks=break_confirm_ticks,
                    break_max_zone_dwell_seconds=break_max_zone_dwell_seconds,
                    false_break_window_seconds=false_break_window_seconds,
                    false_break_confirm_seconds=false_break_confirm_seconds,
                    false_break_confirm_ticks=false_break_confirm_ticks,
                )
                for lvl in lvls
            ]
            for symbol, lvls in levels.items()
        }
        self._refresh_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        n = sum(len(v) for v in self._trackers.values())
        logger.info("PriceStateManager: loading indicators for %d symbols...", len(self._trackers))
        await self._provider.load()
        logger.info("PriceStateManager: ready — watching %d levels across %d symbols", n, len(self._trackers))
        self._subscribe(BrokerEvent.QUOTE_UPDATE, self._on_tick)
        self._refresh_task = asyncio.create_task(self._provider.refresh_loop())

    async def stop(self) -> None:
        self.detach()
        if self._refresh_task:
            self._refresh_task.cancel()
        logger.info("PriceStateManager: stopped")

    async def _on_tick(self, payload: BrokerEventPayload) -> None:
        if not isinstance(payload.data, PriceTick):
            return
        tick = payload.data
        for tracker in self._trackers.get(tick.symbol, []):
            for evt in tracker.update(tick):
                _log_event(evt)
                await self._bus.emit(evt)


def _log_event(evt) -> None:
    extra = f"  orig={evt.original_break.value}" if evt.original_break else ""
    dwell = f"  dwell={evt.dwell_seconds:.0f}s"  if evt.dwell_seconds  else ""
    logger.info(
        "%-13s  %s  level=%.2f  price=%.4f  zone=[%.4f–%.4f]  "
        "atr=%.4f  convincing=%s%s%s",
        f"[{evt.event.value}]", evt.symbol, evt.level, evt.price,
        evt.zone_lo, evt.zone_hi, evt.atr, evt.convincing, dwell, extra,
    )
