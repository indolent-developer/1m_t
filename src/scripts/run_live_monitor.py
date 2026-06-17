"""
scripts.run_live_monitor

Continuous market monitor. Wires all scanners, price feeds, level watchers,
and Telegram alerts into a single long-running process.

What runs:
  Scanners (emit ScannerEvent.SYMBOL_DETECTED):
    SpikeScannerLoop    every 60s   (intraday)
    PreMarketScannerLoop every 120s
    PostMarketScannerLoop every 120s
    VolumeScannerLoop   every 300s  (dynamic rel-vol threshold)

  On SYMBOL_DETECTED:
    SymbolAutoWatcher →
        PriceMonitor.subscribe(symbol)         — live tick stream
        KeyLevelMonitorService.add_symbol()    — level break detection
        Persist to data/watched_symbols.json

  On ticks → level events (BREAK_ABOVE / BREAK_BELOW / BOUNCE / REJECTION / FALSE_BREAK):
    TelegramAlertSubscriber → Telegram message

Required env vars:
    FINNHUB_API_KEY     — primary price feed (WebSocket)
    FMP_API_KEY         — backup price feed + OHLC for level computation
    TELEGRAM_BOT_TOKEN  — Telegram bot token
    TELEGRAM_CHAT_ID    — chat / channel to send alerts to
    TV_CHART_ID         — (optional) TradingView chart layout ID (default: 3UGuuzJ4)
    REDIS_URL           — (optional) defaults to redis://localhost:6379

Usage:
    ./run_scripts/run_live_monitor.sh
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from dotenv import load_dotenv

load_dotenv()

# ── logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

# ── imports (after sys.path / dotenv) ─────────────────────────────────────────
from adapters.brokers.scalable_broker import ScalableBroker
from adapters.events.local_event_bus import LocalEventBus
from core.config.config_loader import ConfigLoader
from data_fetchers.financial_modelling_prep_data_fetcher import FmpDataFetcher
from infrastructure.cache.redis_cache import RedisCache
from interfaces.telegram.alert_subscriber import TelegramAlertSubscriber
from scripts.scanners.post_mkt_loop import PostMarketScannerLoop
from scripts.scanners.pre_mkt_loop import PreMarketScannerLoop
from scripts.scanners.pre_mkt_scalp_loop import PreMarketScalpScannerLoop
from scripts.scanners.vol_loop import VolumeScannerLoop
from services.key_level_monitor_service import KeyLevelMonitorService
from services.price_history_service import PriceHistoryService
from services.price_monitor import PriceMonitor
from services.symbol_auto_watcher import SymbolAutoWatcher

logger = logging.getLogger("run_live_monitor")


def _require(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        logger.error("Missing required env var: %s", name)
        sys.exit(1)
    return val



async def main() -> None:
    telegram_token   = _require("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = _require("TELEGRAM_CHAT_ID")
    fmp_key          = _require("FMP_API_KEY")
    redis_url        = os.environ.get("REDIS_URL", "redis://localhost:6379")

    # ── Infrastructure ────────────────────────────────────────────────────────
    bus          = LocalEventBus()
    fmp_fetcher  = FmpDataFetcher({"api_key": fmp_key})
    redis_cache  = RedisCache(url=redis_url)
    history_svc  = PriceHistoryService(fetcher=fmp_fetcher, cache=redis_cache, fetcher_name="fmp")

    # ── Core services (start with no symbols; SymbolAutoWatcher populates them) ─
    price_monitor = PriceMonitor(symbols=[], bus=bus, poll_interval=30, source="auto")
    key_level_svc = KeyLevelMonitorService(
        symbols=[],
        bus=bus,
        history_service=history_svc,
    )

    # ── Subscribers ───────────────────────────────────────────────────────────
    watcher  = SymbolAutoWatcher(bus, price_monitor, key_level_svc)
    _telegram = TelegramAlertSubscriber(
        bus=bus,
        token=telegram_token,
        chat_id=telegram_chat_id,
        exchange_source=watcher,
        cache=redis_cache,
    )

    # ── Scanner loops ─────────────────────────────────────────────────────────
    pre_mkt       = PreMarketScannerLoop(bus, interval_seconds=120, ttl_seconds=86400, cache=redis_cache)
    pre_mkt_scalp = PreMarketScalpScannerLoop(bus, interval_seconds=120, ttl_seconds=86400, cache=redis_cache)
    post_mkt      = PostMarketScannerLoop(bus, interval_seconds=120, ttl_seconds=86400, cache=redis_cache)
    volume        = VolumeScannerLoop(bus, interval_seconds=300, ttl_seconds=86400, cache=redis_cache)

    # ── Startup ───────────────────────────────────────────────────────────────
    logger.info("Starting KeyLevelMonitorService...")
    await key_level_svc.start()

    logger.info("Restoring today's watched symbols...")
    await watcher.restore_today()

    # ── Portfolio sync ────────────────────────────────────────────────────────
    # Sync open positions from each broker; falls back to last file state on failure
    _brokers = {"scalable": ("scalable", ScalableBroker)}
    for broker_id, (cfg_name, BrokerCls) in _brokers.items():
        try:
            cfg    = ConfigLoader().load_broker(cfg_name)
            broker = BrokerCls(cfg)
            await broker.connect()
            await watcher.sync_portfolio(broker_id, broker)
        except Exception as e:
            logger.warning("Portfolio sync [%s] skipped: %s", broker_id, e)

    # ── Run everything ────────────────────────────────────────────────────────
    logger.info("All systems live. Ctrl-C to stop.")

    tasks = [
        asyncio.create_task(price_monitor.start(), name="price_monitor"),
        asyncio.create_task(pre_mkt.run(),            name="pre_mkt_scanner"),
        asyncio.create_task(pre_mkt_scalp.run(),     name="pre_mkt_scalp_scanner"),
        asyncio.create_task(post_mkt.run(),          name="post_mkt_scanner"),
        asyncio.create_task(volume.run(),           name="vol_scanner"),
    ]

    # Graceful shutdown on SIGINT / SIGTERM
    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    def _handle_signal():
        logger.info("Shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    await stop_event.wait()

    logger.info("Shutting down...")
    pre_mkt.stop()
    pre_mkt_scalp.stop()
    post_mkt.stop()
    volume.stop()

    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    await key_level_svc.stop()
    await price_monitor.stop()
    logger.info("Stopped.")


if __name__ == "__main__":
    asyncio.run(main())
