"""
services.price_monitor

Watches a symbol list and emits BrokerEvent.QUOTE_UPDATE on the bus for every
price tick received.

Tick sources (resolved in priority order for "auto"):

    IBKR WS   — ib_async streaming, ~sub-second, up to 100 symbols free.
    Finnhub   — WebSocket trade stream, ~sub-second.
    FMP poll  — REST batch-quote polling every poll_interval seconds.

Source priority and all parameters come from PriceMonitorConfig (see
config/base.yaml services.price_monitor).  No environment variables are read
directly — the ServiceFactory or caller is responsible for passing the right
config objects.

Adding a new WS source in future:
    1. Implement a fetcher with async subscribe(symbol, cb) / unsubscribe(symbol, cb).
    2. Add a branch in _resolve_ws_fetcher() — that's the only change needed here.

Usage (via ServiceFactory — preferred):
    monitor = factory.price_monitor(symbols=["AAPL", "TSLA"], bus=bus)
    await monitor.start()
    await monitor.stop()

Usage (direct — for tests / scripts):
    from core.config.config_models import PriceMonitorConfig
    monitor = PriceMonitor(symbols=["AAPL"], bus=bus, config=PriceMonitorConfig())
"""
from __future__ import annotations

import asyncio
import datetime as dt
import time
from typing import Any, Dict, List, Optional

from adapters.brokers.entities.broker_event import BrokerEvent
from adapters.brokers.entities.broker_event_payload import BrokerEventPayload
from core.adapters.event_bus import IEventBus
from core.config.config_models import (
    FinnhubDataConfig,
    FmpDataConfig,
    IBKRBrokerConfig,
    PriceMonitorConfig,
)
from core.entities.market_data import PriceTick
from core.utils.log_helper import getLogger

logger = getLogger(__name__)

_STREAM_START_ET = 4    # 4:00 AM ET — suppress stale warnings before pre-market
_STREAM_END_ET   = 20   # 8:00 PM ET — suppress after after-hours close


class PriceMonitor:
    """
    Emits QUOTE_UPDATE events for every tick on the watchlist.

    Fully agnostic to the underlying WS source — any fetcher exposing
    async subscribe(symbol, cb) / unsubscribe(symbol, cb) is accepted.
    The active source is selected once in start() via _resolve_ws_fetcher().

    Stale-data monitoring runs here, not inside individual fetchers, so
    consumers always see the same warning format regardless of source.
    """

    def __init__(
        self,
        symbols: List[str],
        bus: IEventBus,
        config: PriceMonitorConfig,
        ibkr_config: Optional[IBKRBrokerConfig] = None,
        finnhub_config: Optional[FinnhubDataConfig] = None,
        fmp_config: Optional[FmpDataConfig] = None,
    ) -> None:
        self._config              = config
        self._ibkr_config         = ibkr_config
        self._finnhub_config      = finnhub_config
        self._fmp_config          = fmp_config
        self.symbols              = list(symbols)
        self._bus                 = bus
        self._ws_fetcher: Optional[Any]   = None
        self._last_seen: Dict[str, float] = {}
        self._tasks: list[asyncio.Task]   = []

    async def start(self) -> None:
        ws      = self._resolve_ws_fetcher()
        fmp_key = self._fmp_config.api_key if self._fmp_config else ""

        if ws is not None:
            self._ws_fetcher = ws
            for symbol in self.symbols:
                await ws.subscribe(symbol, self._emit_tick)
            logger.info(
                "PriceMonitor: %d symbols → %s",
                len(self.symbols), type(ws).__name__,
            )

        # FMP poll runs alongside WS as a lower-frequency supplement,
        # unless the caller pinned an explicit WS-only source.
        if fmp_key and self._config.source not in ("ibkr", "finnhub"):
            self._tasks.append(asyncio.create_task(self._run_fmp_poll(fmp_key)))
            logger.info("PriceMonitor: FMP poll started (every %ds)", self._config.poll_interval)

        if self._ws_fetcher is None and not self._tasks:
            logger.warning(
                "PriceMonitor: no tick source configured "
                "(set services.price_monitor.source or supply ibkr/finnhub/fmp config)"
            )
            return

        stale_task = asyncio.create_task(self._stale_monitor())
        try:
            if self._tasks:
                await asyncio.gather(*self._tasks, return_exceptions=True)
            else:
                # WS-only: block until the task is cancelled from outside
                await asyncio.get_event_loop().create_future()
        finally:
            stale_task.cancel()
            await asyncio.gather(stale_task, return_exceptions=True)

    async def stop(self) -> None:
        if self._ws_fetcher is not None:
            for symbol in self.symbols:
                await self._ws_fetcher.unsubscribe(symbol, self._emit_tick)
            self._ws_fetcher = None
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
        if self._ws_fetcher is not None:
            await self._ws_fetcher.subscribe(symbol, self._emit_tick)

    async def unsubscribe(self, symbol: str) -> None:
        """Remove a symbol from the live watchlist."""
        if symbol in self.symbols:
            self.symbols.remove(symbol)
        if self._ws_fetcher is not None:
            await self._ws_fetcher.unsubscribe(symbol, self._emit_tick)

    # ── Source resolution ─────────────────────────────────────────────────────

    def _resolve_ws_fetcher(self) -> Optional[Any]:
        """
        Return the right WS fetcher singleton based on config.source.
        Priority for "auto": IBKR (if configured) → Finnhub (if configured) → None.

        To plug in a new WS source, add a branch here and pass its config via
        the constructor / ServiceFactory.
        """
        source = self._config.source.lower()

        # ── IBKR ─────────────────────────────────────────────────────────────
        if source in ("auto", "ibkr") and self._ibkr_config is not None:
            try:
                from data_fetchers.ibkr_ws_data_fetcher import get_shared_fetcher as _ibkr
                return _ibkr(config=self._ibkr_config)
            except Exception as exc:
                logger.warning("PriceMonitor: IBKR WS unavailable (%s)", exc)
                if source == "ibkr":
                    return None

        # ── Finnhub (fallback for "auto", or explicit) ────────────────────────
        if source in ("auto", "finnhub") and self._finnhub_config is not None:
            if self._finnhub_config.api_key:
                from data_fetchers.finnhub_ws_data_fetcher import get_shared_fetcher as _fhub
                return _fhub(config=self._finnhub_config)

        return None

    # ── Stale-data monitor ────────────────────────────────────────────────────

    async def _stale_monitor(self) -> None:
        """
        Warn once per symbol when no tick arrives within stale_warn_seconds.
        Runs for the lifetime of start() and fires regardless of which
        underlying source is active — callers always see the same log format.
        """
        import pytz
        _ET = pytz.timezone("America/New_York")
        warned: set[str] = set()

        while True:
            await asyncio.sleep(self._config.stale_check_interval)

            now_et = dt.datetime.now(_ET)
            if not (_STREAM_START_ET <= now_et.hour < _STREAM_END_ET):
                warned.clear()
                continue

            now_ms  = time.time() * 1000
            active  = list(self.symbols)
            source  = type(self._ws_fetcher).__name__ if self._ws_fetcher else "fmp"
            logger.info(
                "PriceMonitor: %d symbols watched  source=%s",
                len(active), source,
            )

            for sym in active:
                last  = self._last_seen.get(sym)
                age_s = (now_ms - last) / 1000 if last is not None else float("inf")
                if age_s > self._config.stale_warn_seconds:
                    if sym not in warned:
                        age_str = "never received" if last is None else f"{age_s / 60:.1f}m"
                        logger.warning(
                            "PriceMonitor: no tick for %s in %s — feed stale or symbol inactive",
                            sym, age_str,
                        )
                        warned.add(sym)
                else:
                    warned.discard(sym)

    # ── FMP polling ───────────────────────────────────────────────────────────

    async def _run_fmp_poll(self, api_key: str) -> None:
        from services.price_service import FmpPriceService
        svc = FmpPriceService(api_key=api_key, symbols=self.symbols)
        await svc.stream(self._emit_tick, interval=self._config.poll_interval)

    # ── Emit ──────────────────────────────────────────────────────────────────

    async def _emit_tick(self, tick: PriceTick) -> None:
        self._last_seen[tick.symbol] = float(tick.timestamp)
        payload = BrokerEventPayload(
            event=BrokerEvent.QUOTE_UPDATE,
            broker_id=tick.source,
            data=tick,
        )
        await self._bus.emit(payload)
        logger.debug("[%s] %s → %.4f  vol=%.0f", tick.source, tick.symbol, tick.price, tick.volume)
