"""
scripts.test_price_monitor

Runs the PriceMonitor for 60 seconds and prints every tick to stdout.
Ctrl-C exits cleanly.

Usage:
    ./run_scripts/run_price_monitor.sh
    ./run_scripts/run_price_monitor.sh AAPL TSLA NVDA   # custom symbols
"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from adapters.brokers.entities.broker_event import BrokerEvent
from adapters.brokers.entities.broker_event_payload import BrokerEventPayload
from adapters.events.local_event_bus import LocalEventBus
from core.entities.market_data import PriceTick
from services.price_monitor import PriceMonitor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("websockets").setLevel(logging.WARNING)

DEFAULT_SYMBOLS = [
    "AAPL", "TSLA", "NVDA", "MSFT", "AMZN",
    "META", "GOOGL", "AMD", "PLTR", "COIN",
]

RUN_SECONDS = 60  # how long to monitor before exiting


HEADER = (
    f"{'source':<10}  {'time':<8}  {'sym':<6}  "
    f"{'bid':>10}  {'ask':>10}  {'last':>10}  "
    f"{'vol':>10}  {'chg%':>7}  {'high':>10}  {'low':>10}"
)


def on_tick(payload: BrokerEventPayload) -> None:
    tick: PriceTick = payload.data
    if tick.timestamp > 1_000_000_000_000:        # ms  (Finnhub trades)
        ts = datetime.fromtimestamp(tick.timestamp / 1000, tz=timezone.utc).strftime("%H:%M:%S")
    elif tick.timestamp > 1_000_000_000:           # s   (FMP batch-quote)
        ts = datetime.fromtimestamp(tick.timestamp, tz=timezone.utc).strftime("%H:%M:%S")
    else:                                          # no timestamp
        ts = datetime.now(tz=timezone.utc).strftime("%H:%M:%S")
    chg  = f"{tick.change_pct:+.2f}%" if tick.change_pct else "      "
    high = f"{tick.day_high:.4f}"      if tick.day_high  else "          "
    low  = f"{tick.day_low:.4f}"       if tick.day_low   else "          "

    print(
        f"[{payload.broker_id:<8}]  {ts}  {tick.symbol:<6}  "
        f"{tick.bid:>10.4f}  {tick.ask:>10.4f}  {tick.price:>10.4f}  "
        f"{tick.volume:>10.0f}  {chg:>7}  {high:>10}  {low:>10}"
    )


async def main() -> None:
    # Parse args: uppercase tokens are symbols, "fmp"/"finnhub" sets the source
    source  = "auto"
    symbols = []
    for arg in sys.argv[1:]:
        if arg.lower() in ("fmp", "finnhub"):
            source = arg.lower()
        else:
            symbols.append(arg.upper())
    if not symbols:
        symbols = DEFAULT_SYMBOLS

    bus = LocalEventBus()
    bus.subscribe(BrokerEvent.QUOTE_UPDATE, on_tick)

    monitor = PriceMonitor(symbols=symbols, bus=bus, poll_interval=15, source=source)

    src_tag = f"  source={source}" if source != "auto" else ""
    print(f"Monitoring {len(symbols)} symbols for {RUN_SECONDS}s: {', '.join(symbols)}{src_tag}")
    print(HEADER)
    print("-" * len(HEADER))

    try:
        async with asyncio.timeout(RUN_SECONDS):
            await monitor.start()
    except (asyncio.TimeoutError, KeyboardInterrupt):
        pass
    finally:
        await monitor.stop()
        print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
