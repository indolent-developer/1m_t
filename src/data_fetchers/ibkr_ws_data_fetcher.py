"""
data_fetchers.ibkr_ws_data_fetcher

Streams real-time market data from Interactive Brokers via ib_async and fires
registered per-symbol on_tick callbacks on every price update.

Prerequisites:
    IB Gateway running in Docker (live port 4001, paper port 4002):
        cd docker/ibkr_gateway && docker compose up -d
    uv add ib_async

Interface mirrors FinnhubWsDataFetcher — subscribe / unsubscribe / start /
stop / close, plus module-level get_shared_fetcher / shutdown_shared_fetcher.

Uses client_id_data (default 2) — a separate IB session from the order-routing
broker (client_id_broker = 1) so market data and order flow don't share a slot.

Symbol cap:
    Every funded IBKR account may stream real-time data for up to 100 symbols
    simultaneously at no extra charge (Trader Workstation -> Market Data ->
    Market Data Subscriptions).  Pass max_symbols= to get_shared_fetcher() or
    set IBKR_MAX_SYMBOLS env var.

Market data type (IBKR_MARKET_DATA_TYPE):
    1 = live real-time  (requires an active market data subscription)
    3 = delayed ~15 min (no subscription needed — good for dev/testing)

Environment (read by get_shared_fetcher if no explicit config is passed):
    IBKR_HOST               (default 127.0.0.1)
    IBKR_PORT               (default 4002)
    IBKR_CLIENT_ID_DATA     (default 2)
    IBKR_MARKET_DATA_TYPE   (default 1)
    IBKR_MAX_SYMBOLS        (default 100)
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import math
import time
from typing import Any, Callable, Dict, List, Optional

from core.config.config_models import IBKRBrokerConfig
from core.entities.market_data import PriceTick
from core.utils.log_helper import getLogger

logger = getLogger(__name__)

_BACKOFF_INIT        = 5
_BACKOFF_MAX         = 60
_STALE_WARN_SECONDS  = 180
_STALE_CHECK_INTERVAL = 60

# ── Module-level singleton ─────────────────────────────────────────────────────

_instance:      Optional["IbkrWsDataFetcher"] = None
_instance_task: Optional[asyncio.Task]        = None


def get_shared_fetcher(
    config: "IBKRBrokerConfig | None" = None,
    # --- backward-compat kwargs (used when config is not provided) ---
    host:             str = "",
    port:             int = 0,
    client_id_data:   int = 0,
    market_data_type: int = 0,
    max_symbols:      int = 0,
) -> "IbkrWsDataFetcher":
    """
    Return (or create) the process-wide IbkrWsDataFetcher singleton.

    Preferred: pass an IBKRBrokerConfig from config_loader.load_broker("ibkr").
    Legacy: pass individual keyword arguments (still supported for scripts/tests).
    Must be called from an async context (uses asyncio.create_task).
    """
    global _instance, _instance_task
    if _instance is None:
        if config is not None:
            host             = config.host
            port             = config.port
            client_id_data   = config.client_id_data
            market_data_type = config.market_data_type
            max_symbols      = config.max_symbols
        else:
            host             = host             or "127.0.0.1"
            port             = port             or 4002
            client_id_data   = client_id_data   or 2
            market_data_type = market_data_type or 1
            max_symbols      = max_symbols      or 100

        _instance = IbkrWsDataFetcher(
            host=host,
            port=port,
            client_id_data=client_id_data,
            market_data_type=market_data_type,
            max_symbols=max_symbols,
        )
        _instance_task = asyncio.create_task(_instance.start())
        logger.info(
            "IBKR WS singleton started (host=%s port=%d clientId=%d mdType=%d max=%d)",
            host, port, client_id_data, market_data_type, max_symbols,
        )
    return _instance


async def shutdown_shared_fetcher() -> None:
    """Close the singleton and cancel its task.  Safe to call if never started."""
    global _instance, _instance_task
    if _instance is not None:
        await _instance.close()
        _instance = None
    if _instance_task is not None:
        _instance_task.cancel()
        await asyncio.gather(_instance_task, return_exceptions=True)
        _instance_task = None
        logger.info("IBKR WS singleton stopped")


# ── Fetcher class ──────────────────────────────────────────────────────────────

class IbkrWsDataFetcher:
    """
    Shared ib_async market-data client.

    Multiple callers subscribe per-symbol callbacks; one IB session serves them
    all.  Dynamic subscribe / unsubscribe while connected is supported.

    Usage:
        fetcher = IbkrWsDataFetcher(host="127.0.0.1", port=4002, ...)
        task    = asyncio.create_task(fetcher.start())

        await fetcher.subscribe("AAPL", my_tick_handler)
        await fetcher.unsubscribe("AAPL", my_tick_handler)

        fetcher.stop()
        await fetcher.close()
        task.cancel()
    """

    def __init__(
        self,
        host:             str = "127.0.0.1",
        port:             int = 4002,
        client_id_data:   int = 2,
        market_data_type: int = 1,
        max_symbols:      int = 100,
    ) -> None:
        self.host             = host
        self.port             = port
        self.client_id_data   = client_id_data
        self.market_data_type = market_data_type
        self.max_symbols      = max_symbols

        self._running = False
        self._ib: Any = None                         # ib_async.IB instance
        self._callbacks:  Dict[str, List[Callable]] = {}
        self._contracts:  Dict[str, Any] = {}        # symbol → qualified Contract
        self._tickers:    Dict[str, Any] = {}        # symbol → ib_async Ticker
        self._last_tick:  Dict[str, float] = {}      # symbol → epoch-ms of last tick
        self._stale_warned: set[str] = set()
        self._msg_count: int = 0
        self._disconnect_future: Optional[asyncio.Future] = None

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def symbols(self) -> List[str]:
        return list(self._callbacks)

    async def subscribe(self, symbol: str, on_tick: Callable) -> None:
        """Register on_tick for symbol.  Requests live market data if connected."""
        if symbol not in self._callbacks and len(self._callbacks) >= self.max_symbols:
            logger.warning(
                "IBKR WS: symbol cap %d reached — %s skipped. "
                "Raise IBKR_MAX_SYMBOLS if your account allows more.",
                self.max_symbols, symbol,
            )
            return
        cbs = self._callbacks.setdefault(symbol, [])
        if on_tick not in cbs:
            cbs.append(on_tick)
        if self._ib is not None and self._ib.isConnected() and symbol not in self._tickers:
            await self._subscribe_market_data(symbol)

    async def unsubscribe(self, symbol: str, on_tick: Callable) -> None:
        """Remove on_tick.  Cancels market data stream if no callbacks remain."""
        cbs = self._callbacks.get(symbol, [])
        try:
            cbs.remove(on_tick)
        except ValueError:
            pass
        if not cbs:
            self._callbacks.pop(symbol, None)
            self._last_tick.pop(symbol, None)
            self._stale_warned.discard(symbol)
            if self._ib is not None and symbol in self._contracts:
                try:
                    self._ib.cancelMktData(self._contracts[symbol])
                    logger.debug("IBKR WS: cancelled market data for %s", symbol)
                except Exception:
                    pass
                self._contracts.pop(symbol, None)
                self._tickers.pop(symbol, None)

    async def start(self) -> None:
        """Run the reconnect loop.  Returns when stop() is called or task cancelled."""
        self._running = True
        backoff = _BACKOFF_INIT
        while self._running:
            try:
                await self._connect()
                backoff = _BACKOFF_INIT
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("IBKR WS dropped (%s) — reconnecting in %ds", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX)

    def stop(self) -> None:
        """Signal the reconnect loop to exit."""
        self._running = False

    async def close(self) -> None:
        """Immediately disconnect. Call stop() first to prevent reconnect."""
        self._running = False
        if self._disconnect_future and not self._disconnect_future.done():
            self._disconnect_future.set_result(None)
        elif self._ib and self._ib.isConnected():
            self._ib.disconnect()

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _connect(self) -> None:
        try:
            import ib_async
        except ImportError:
            raise ImportError(
                "ib_async is not installed. Run: uv add ib_async\n"
                "Then start IB Gateway: cd docker/ibkr_gateway && docker compose up -d"
            )

        logging.getLogger("ib_async").setLevel(logging.ERROR)
        self._ib = ib_async.IB()
        self._ib.errorEvent += self._on_ib_error

        await self._ib.connectAsync(
            host=self.host,
            port=self.port,
            clientId=self.client_id_data,
            readonly=True,
            timeout=10.0,
        )
        self._ib.reqMarketDataType(self.market_data_type)
        self._ib.pendingTickersEvent += self._on_pending_tickers
        self._msg_count = 0

        # Subscribe market data for all already-registered symbols
        for symbol in list(self._callbacks):
            await self._subscribe_market_data(symbol)

        logger.info(
            "IBKR WS: connected (clientId=%d mdType=%d) — streaming %d symbols: %s",
            self.client_id_data, self.market_data_type,
            len(self._callbacks), list(self._callbacks),
        )

        # Future resolves when IB disconnects or close() is called
        loop = asyncio.get_event_loop()
        self._disconnect_future = loop.create_future()

        def _on_disconnected() -> None:
            if self._disconnect_future and not self._disconnect_future.done():
                self._disconnect_future.set_result(None)

        self._ib.disconnectedEvent += _on_disconnected
        stale_task = asyncio.create_task(self._stale_monitor())

        try:
            await self._disconnect_future
        finally:
            stale_task.cancel()
            await asyncio.gather(stale_task, return_exceptions=True)
            self._ib.pendingTickersEvent -= self._on_pending_tickers
            self._ib.disconnectedEvent   -= _on_disconnected
            self._disconnect_future = None
            if self._ib.isConnected():
                self._ib.disconnect()
            self._ib = None
            self._tickers.clear()
            self._contracts.clear()

    async def _subscribe_market_data(self, symbol: str) -> None:
        try:
            import ib_async
        except ImportError:
            return
        try:
            contract = ib_async.Stock(symbol, "SMART", "USD")
            await self._ib.qualifyContractsAsync(contract)
            # genericTickList="" → standard tick types (bid/ask/last/volume/close)
            ticker = self._ib.reqMktData(contract, "", False, False)
            self._contracts[symbol] = contract
            self._tickers[symbol]   = ticker
            logger.debug("IBKR WS: subscribed market data for %s", symbol)
        except Exception as exc:
            logger.warning("IBKR WS: failed to subscribe %s: %s", symbol, exc)

    def _on_pending_tickers(self, tickers) -> None:
        """Called synchronously by ib_async when tickers have new data."""
        for ticker in tickers:
            if not ticker.contract:
                continue
            sym = ticker.contract.symbol
            if sym not in self._callbacks:
                continue

            # Prefer last trade; fall back to bid/ask mid
            last = ticker.last
            bid  = ticker.bid  if ticker.bid  and not math.isnan(ticker.bid)  else 0.0
            ask  = ticker.ask  if ticker.ask  and not math.isnan(ticker.ask)  else 0.0

            if not last or math.isnan(last):
                if not bid and not ask:
                    continue
                last = (bid + ask) / 2.0 if bid and ask else (bid or ask)

            vol = ticker.volume if ticker.volume and not math.isnan(ticker.volume) else 0.0
            ts  = (
                int(ticker.time.timestamp() * 1000)
                if ticker.time
                else int(time.time() * 1000)
            )

            self._last_tick[sym] = float(ts)
            self._stale_warned.discard(sym)
            self._msg_count += 1

            tick = PriceTick(
                symbol=sym,
                price=float(last),
                bid=float(bid) if bid else float(last),
                ask=float(ask) if ask else float(last),
                volume=float(vol),
                timestamp=ts,
                source="ibkr",
            )

            for cb in list(self._callbacks.get(sym, [])):
                try:
                    result = cb(tick)
                    if asyncio.iscoroutine(result):
                        asyncio.ensure_future(result)
                except Exception:
                    logger.exception("IBKR WS: on_tick error for %s", sym)

    def _on_ib_error(self, req_id: int, code: int, msg: str, contract) -> None:
        _SILENT = {
            202, 399, 404,
            2104, 2106, 2108, 2109, 2119, 2158,
            10167,
        }
        if code in _SILENT:
            logger.debug("IBKR WS %d (reqId=%d): %s", code, req_id, msg)
        else:
            logger.warning("IBKR WS %d (reqId=%d): %s", code, req_id, msg)

    async def _stale_monitor(self) -> None:
        """Log a tick-rate summary every check interval (connection-health only).
        Per-symbol stale warnings are handled by PriceMonitor so the check
        is the same regardless of which WS source is active."""
        import pytz
        _ET = pytz.timezone("America/New_York")

        while True:
            await asyncio.sleep(_STALE_CHECK_INTERVAL)
            now_et = dt.datetime.now(_ET)
            if not (4 <= now_et.hour < 20):
                continue
            ticks, self._msg_count = self._msg_count, 0
            logger.info(
                "IBKR WS: %d tick updates in last %ds across %d subscribed symbols",
                ticks, _STALE_CHECK_INTERVAL, len(self._callbacks),
            )
