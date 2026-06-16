"""
services.price_monitor

Watches a symbol list and emits BrokerEvent.QUOTE_UPDATE on the bus for every
price tick received.

Two sources run concurrently when both keys are present:

    Finnhub WebSocket  — true trade stream, ~sub-second latency.
                         broker_id = "finnhub"

    FMP polling        — FmpPriceService, every `poll_interval` seconds.
                         Extended hours → batch-aftermarket-quote (bid/ask)
                                        → batch-aftermarket-trade (last trade)
                         Regular hours  → batch-quote (last traded price)
                         All via /stable/ endpoints. broker_id = "fmp"

Subscribers identify the source via BrokerEventPayload.broker_id:

    async def on_tick(payload: BrokerEventPayload) -> None:
        tick: PriceTick = payload.data
        print(payload.broker_id, tick.symbol, tick.price)

    bus.subscribe(BrokerEvent.QUOTE_UPDATE, on_tick)

Shared Finnhub WS:
    Pass a running FinnhubWsDataFetcher via shared_fetcher so that multiple
    PriceMonitor instances (e.g. one per /ml monitor) share a single WS
    connection rather than each opening their own.  Dynamic subscribe /
    unsubscribe is handled automatically on start() / stop().

Usage:
    monitor = PriceMonitor(symbols=["AAPL", "TSLA"], bus=bus)
    await monitor.start()   # runs until stop() is called
    await monitor.stop()

    # or with a shared WS:
    fetcher = FinnhubWsDataFetcher(api_key)
    asyncio.create_task(fetcher.start())
    monitor = PriceMonitor(symbols=["AAPL"], bus=bus, shared_fetcher=fetcher)

Environment:
    FINNHUB_API_KEY   — enables WebSocket stream
    FMP_API_KEY       — enables REST polling
"""
from __future__ import annotations

import asyncio
from core.utils.log_helper import getLogger
import os
from typing import List

from adapters.brokers.entities.broker_event import BrokerEvent
from adapters.brokers.entities.broker_event_payload import BrokerEventPayload
from core.adapters.event_bus import IEventBus
from core.entities.market_data import PriceTick

logger = getLogger(__name__)


class PriceMonitor:
    """
    Emits QUOTE_UPDATE events for every tick on the watchlist.

    broker_id on each payload tells subscribers who generated the tick
    ("finnhub" for WebSocket trades, "fmp" for polled batch quotes).
    """

    def __init__(
        self,
        symbols: List[str],
        bus: IEventBus,
        poll_interval: int = 30,
        source: str = "auto",   # "auto" | "fmp" | "finnhub"
    ) -> None:
        self.symbols       = list(symbols)
        self._bus          = bus
        self.poll_interval = poll_interval
        self.source        = source.lower()
        self._fetcher      = None               # the shared singleton once subscribed
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        finnhub_key = os.environ.get("FINNHUB_API_KEY")
        fmp_key     = os.environ.get("FMP_API_KEY") or os.environ.get("FINANCIAL_MODELING_PREP_API_KEY")

        use_finnhub = self.source in ("auto", "finnhub") and finnhub_key
        use_fmp     = self.source in ("auto", "fmp")     and fmp_key

        if use_finnhub:
            from data_fetchers.finnhub_ws_data_fetcher import get_shared_fetcher
            self._fetcher = get_shared_fetcher(finnhub_key)
            for symbol in self.symbols:
                await self._fetcher.subscribe(symbol, self._emit_tick)
            logger.info("PriceMonitor: subscribed %s to shared Finnhub WS", self.symbols)

        if use_fmp:
            self._tasks.append(asyncio.create_task(self._run_fmp_poll(fmp_key)))
            logger.info("PriceMonitor: FMP poll source started (every %ds)", self.poll_interval)

        if self._fetcher is None and not self._tasks:
            logger.warning("PriceMonitor: no source available — set FINNHUB_API_KEY or FMP_API_KEY")
            return

        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        else:
            # Finnhub-only: callbacks are registered but start() has no long-running
            # coroutine to await — without this the caller's _run() would immediately
            # fall through to stop(), unsubscribing before any ticks arrive.
            await asyncio.get_event_loop().create_future()  # blocks until task cancelled

    async def stop(self) -> None:
        if self._fetcher is not None:
            for symbol in self.symbols:
                await self._fetcher.unsubscribe(symbol, self._emit_tick)
            self._fetcher = None
            logger.info("PriceMonitor: unsubscribed %s from Finnhub WS", self.symbols)
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("PriceMonitor: stopped")

    # ── Dynamic subscription ──────────────────────────────────────────────────

    async def subscribe(self, symbol: str) -> None:
        """Add a symbol to the live watchlist."""
        if symbol not in self.symbols:
            self.symbols.append(symbol)
        target = self._fetcher
        if target is not None:
            await target.subscribe(symbol, self._emit_tick)

    async def unsubscribe(self, symbol: str) -> None:
        """Remove a symbol from the live watchlist."""
        if symbol in self.symbols:
            self.symbols.remove(symbol)
        target = self._fetcher
        if target is not None:
            await target.unsubscribe(symbol, self._emit_tick)

    # ── Finnhub WebSocket (own connection) ────────────────────────────────────

    async def _run_finnhub_ws(self, api_key: str) -> None:
        from data_fetchers.finnhub_ws_data_fetcher import FinnhubWsDataFetcher
        self._fetcher = FinnhubWsDataFetcher(api_key=api_key)
        for symbol in self.symbols:
            await self._fetcher.subscribe(symbol, self._emit_tick)
        await self._fetcher.start()

    # ── FMP batch polling ─────────────────────────────────────────────────────

    async def _run_fmp_poll(self, api_key: str) -> None:
        from services.price_service import FmpPriceService
        svc = FmpPriceService(api_key=api_key, symbols=self.symbols)
        await svc.stream(self._emit_tick, interval=self.poll_interval)

    # ── Emit ──────────────────────────────────────────────────────────────────

    async def _emit_tick(self, tick: PriceTick) -> None:
        payload = BrokerEventPayload(
            event=BrokerEvent.QUOTE_UPDATE,
            broker_id=tick.source,
            data=tick,
        )
        await self._bus.emit(payload)
        logger.debug("[%s] %s → %.4f  vol=%.0f", tick.source, tick.symbol, tick.price, tick.volume)
