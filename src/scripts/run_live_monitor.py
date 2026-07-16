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
    python run_live_monitor.py --list all
    python run_live_monitor.py --list portfolio,watchlist
    python run_live_monitor.py --list scanners
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from dotenv import load_dotenv

load_dotenv(override=False)  # populate os.environ from .env; config_loader picks it up

# ── logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

# ── imports (after dotenv) ────────────────────────────────────────────────────
from adapters.brokers.scalable_broker import ScalableBroker
from core.config.config_loader import ConfigLoader, config_loader
from data_fetchers.financial_modelling_prep_data_fetcher import FmpDataFetcher
from data_fetchers.finnhub_data_fetcher import FinnhubDataFetcher
from data_fetchers.yahoo_finance_data_fetcher import YahooFinanceDataFetcher
from scripts.scanners.post_mkt_loop import PostMarketScannerLoop
from scripts.scanners.pre_mkt_loop import PreMarketScannerLoop
from scripts.scanners.pre_mkt_scalp_loop import PreMarketScalpScannerLoop
from scripts.scanners.vol_loop import VolumeScannerLoop
from services.key_level_monitor_service import KeyLevelMonitorService
from services.news_monitor_service import NewsMonitorService
from services.news_reaction_analyzer import NewsReactionAnalyzer
from services.price_history_service import PriceHistoryService
from services.service_factory import ServiceFactory
from services.symbol_auto_watcher import SymbolAutoWatcher

logger = logging.getLogger("run_live_monitor")


def _require_config(label: str, value: str) -> str:
    """Exit early if a required config value is empty."""
    if not value.strip():
        logger.error("Missing required config: %s — check .env or base.yaml", label)
        sys.exit(1)
    return value.strip()



def _parse_lists() -> set[str]:
    parser = argparse.ArgumentParser(description="Live Market Monitor")
    parser.add_argument(
        "--list",
        default="all",
        help="Comma-separated symbol sources: portfolio, watchlist, scanners, all (default: all)",
    )
    args = parser.parse_args()
    lists = {s.strip().lower() for s in args.list.split(",")}
    if "all" in lists:
        return {"portfolio", "watchlist", "scanners"}
    valid = {"portfolio", "watchlist", "scanners"}
    unknown = lists - valid
    if unknown:
        parser.error(f"Unknown list(s): {', '.join(unknown)}. Choose from: {', '.join(sorted(valid))}")
    return lists


async def main() -> None:
    lists = _parse_lists()
    logger.info("Active symbol sources: %s", ", ".join(sorted(lists)))

    # ── Config ────────────────────────────────────────────────────────────────
    factory      = ServiceFactory(config_loader)
    tg_cfg       = config_loader.load_telegram()
    fmp_cfg      = config_loader.load_data_apis().financialmodelingprep
    finnhub_cfg  = config_loader.load_data_apis().finnhub

    _require_config("telegram.bot_token (TELEGRAM_BOT_TOKEN)", tg_cfg.bot_token)
    _require_config("telegram.chat_id (TELEGRAM_CHAT_ID)",     tg_cfg.chat_id)
    _require_config("data_apis.financialmodelingprep.api_key (FMP_API_KEY)", fmp_cfg.api_key)

    # ── Infrastructure ────────────────────────────────────────────────────────
    bus         = factory.event_bus()
    redis_cache = factory.redis_cache()

    try:
        await redis_cache.ping()
    except Exception as e:
        logger.error("Redis unavailable (%s) — cannot start. Is Redis running?", e)
        sys.exit(1)

    fmp_fetcher = FmpDataFetcher({"api_key": fmp_cfg.api_key})
    history_svc = PriceHistoryService(fetcher=fmp_fetcher, cache=redis_cache, fetcher_name="fmp")

    # ── Core services (start with no symbols; SymbolAutoWatcher populates them) ─
    price_monitor = factory.price_monitor(symbols=[], bus=bus)
    key_level_svc = KeyLevelMonitorService(symbols=[], bus=bus, history_service=history_svc)

    # ── News sources (optional — enabled by API keys) ─────────────────────────
    finnhub_fetcher = FinnhubDataFetcher({"api_key": finnhub_cfg.api_key}) if finnhub_cfg.api_key else None
    try:
        yahoo_fetcher = YahooFinanceDataFetcher()
    except Exception as e:
        logger.warning("Yahoo Finance fetcher unavailable: %s", e)
        yahoo_fetcher = None

    # ── Subscribers ───────────────────────────────────────────────────────────
    watcher   = SymbolAutoWatcher(bus, price_monitor, key_level_svc)
    _telegram = factory.telegram_alert_subscriber(bus=bus, exchange_source=watcher, cache=redis_cache)

    news_monitor = NewsMonitorService(
        bus=bus,
        fmp=fmp_fetcher,
        finnhub=finnhub_fetcher,
        yahoo=yahoo_fetcher,
        cache=redis_cache,
        poll_fmp_seconds=300,
        watchlist=watcher._watched,
    )
    news_analyzer = NewsReactionAnalyzer(
        bus=bus,
        fmp=fmp_fetcher,
        telegram_token=tg_cfg.bot_token,
        chat_id=tg_cfg.chat_id,
        watchlist=watcher._watched,
    )

    # ── Scanner loops ─────────────────────────────────────────────────────────
    pre_mkt       = PreMarketScannerLoop(bus, interval_seconds=120, ttl_seconds=86400, cache=redis_cache)
    pre_mkt_scalp = PreMarketScalpScannerLoop(bus, interval_seconds=120, ttl_seconds=86400, cache=redis_cache)
    post_mkt      = PostMarketScannerLoop(bus, interval_seconds=120, ttl_seconds=86400, cache=redis_cache)
    volume        = VolumeScannerLoop(bus, interval_seconds=300, ttl_seconds=86400, cache=redis_cache)

    # ── Startup ───────────────────────────────────────────────────────────────
    logger.info("Starting KeyLevelMonitorService...")
    await key_level_svc.start()
    logger.info("Starting NewsMonitorService...")
    await news_monitor.start()
    news_analyzer.start()

    if "watchlist" in lists:
        logger.info("Restoring today's watched symbols...")
        await watcher.restore_today()

    # ── Portfolio sync ────────────────────────────────────────────────────────
    if "portfolio" in lists:
        _brokers = {"scalable": ("scalable", ScalableBroker)}
        for broker_id, (cfg_name, BrokerCls) in _brokers.items():
            try:
                cfg    = config_loader.load_broker(cfg_name)
                broker = BrokerCls(cfg)
                await broker.connect()
                await watcher.sync_portfolio(broker_id, broker)
            except Exception as e:
                logger.warning("Portfolio sync [%s] skipped: %s", broker_id, e)

    # ── Run everything ────────────────────────────────────────────────────────
    logger.info("All systems live. Ctrl-C to stop.")

    tasks = [asyncio.create_task(price_monitor.start(), name="price_monitor")]

    if "scanners" in lists:
        tasks += [
            asyncio.create_task(pre_mkt.run(),       name="pre_mkt_scanner"),
            asyncio.create_task(pre_mkt_scalp.run(), name="pre_mkt_scalp_scanner"),
            asyncio.create_task(post_mkt.run(),      name="post_mkt_scanner"),
            asyncio.create_task(volume.run(),        name="vol_scanner"),
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
    if "scanners" in lists:
        pre_mkt.stop()
        pre_mkt_scalp.stop()
        post_mkt.stop()
        volume.stop()

    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    news_analyzer.stop()
    await news_monitor.stop()
    await key_level_svc.stop()
    await price_monitor.stop()
    logger.info("Stopped.")


if __name__ == "__main__":
    asyncio.run(main())
