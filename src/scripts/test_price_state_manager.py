"""
scripts.test_price_state_manager

Runs PriceMonitor + PriceStateManager together.
Prints level events (BOUNCE, REJECTION, BREAK_ABOVE, BREAK_BELOW, FALSE_BREAK)
as they are detected.

Usage:
    ./run_scripts/run_price_state_manager.sh
"""
from __future__ import annotations

import asyncio
import logging
import os

from dotenv import load_dotenv

load_dotenv()

from adapters.events.local_event_bus import LocalEventBus
from core.entities.level_event import LevelEvent, PriceLevelEvent
from data_fetchers.financial_modelling_prep_data_fetcher import FmpDataFetcher
from infrastructure.cache.redis_cache import RedisCache
from services.price_history_service import PriceHistoryService
from services.price_monitor import PriceMonitor
from services.price_state_manager import PriceStateManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("websockets").setLevel(logging.WARNING)
# mute tick-level debug noise from monitor
logging.getLogger("services.price_monitor").setLevel(logging.WARNING)

# ── Levels — set near current prices so the zone is live ─────────────────────
LEVELS = {
    "APLD": [41.5],
}

RUN_SECONDS = 86400  # run all day, Ctrl-C to stop

_EVENT_EMOJI = {
    LevelEvent.BREAK_ABOVE: "▲ BREAK ABOVE",
    LevelEvent.BREAK_BELOW: "▼ BREAK BELOW",
    LevelEvent.BOUNCE:      "↑ BOUNCE     ",
    LevelEvent.REJECTION:   "↓ REJECTION  ",
    LevelEvent.FALSE_BREAK: "✗ FALSE BREAK",
}


def on_level_event(payload: PriceLevelEvent) -> None:
    label = _EVENT_EMOJI.get(payload.event, payload.event.value)
    dwell = f"  dwell={payload.dwell_seconds:.0f}s" if payload.dwell_seconds else ""
    orig  = f"  orig={payload.original_break.value}" if payload.original_break else ""
    conv  = "✓" if payload.convincing else "~"
    print(
        f"{label}  {payload.symbol:<5}  level={payload.level:<8.2f}  "
        f"price={payload.price:<10.4f}  "
        f"zone=[{payload.zone_lo:.3f}–{payload.zone_hi:.3f}]  "
        f"atr={payload.atr:.4f}  {conv}{dwell}{orig}"
    )


async def main() -> None:
    bus = LocalEventBus()

    for evt in LevelEvent:
        bus.subscribe(evt, on_level_event)

    fmp_key  = os.environ.get("FMP_API_KEY", "")
    symbols  = list(LEVELS.keys())
    monitor  = PriceMonitor(symbols=symbols, bus=bus, poll_interval=20)
    fmp      = FmpDataFetcher({"api_key": fmp_key})
    history  = PriceHistoryService(
        fetcher=fmp,
        cache=RedisCache(url=os.environ.get("REDIS_URL", "redis://localhost:6379")),
        fetcher_name="fmp",
    )
    manager = PriceStateManager(levels=LEVELS, bus=bus, history=history)

    print(f"Loading indicators and watching {len(symbols)} symbols for {RUN_SECONDS}s...")
    print("-" * 90)

    await manager.start()

    try:
        async with asyncio.timeout(RUN_SECONDS):
            await monitor.start()
    except (asyncio.TimeoutError, KeyboardInterrupt):
        pass
    finally:
        await monitor.stop()
        await manager.stop()
        print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
