"""
data_fetchers.finnhub_ws_data_fetcher

Streams real-time trades from Finnhub via WebSocket and calls registered
per-symbol on_tick callbacks for every trade event received.

Protocol:
    connect  → wss://ws.finnhub.io?token=KEY
    subscribe → {"type":"subscribe","symbol":"AAPL"}
    receive  → {"type":"trade","data":[{"s":"AAPL","p":price,"v":volume,"t":ts_ms}]}
    unsubscribe → {"type":"unsubscribe","symbol":"AAPL"}

One shared instance handles all symbols; callers register per-symbol callbacks
via subscribe() / unsubscribe().  On reconnect the existing WS is fully closed
(async-with context manager sends a close frame) before a new one opens, and all
currently registered symbols are re-subscribed automatically.

Reconnects with exponential back-off capped at 60 s.

Symbol cap:
    Finnhub enforces a per-connection subscription limit (50 on the free plan).
    Pass max_symbols= to get_shared_fetcher() or set FINNHUB_MAX_SYMBOLS env var.
    Symbols beyond the cap are logged as warnings; they will still receive FMP
    polling ticks if FMP_API_KEY is set.

Stale-feed monitoring:
    A background task checks every 60 s.  Any subscribed symbol that has not
    produced a tick in STALE_WARN_SECONDS (180 s) gets a WARNING log.  The
    warning is suppressed once ticks resume.

Environment:
    FINNHUB_API_KEY
    FINNHUB_MAX_SYMBOLS   (optional, default 50)
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
from core.utils.log_helper import getLogger
from typing import Callable, Dict, List, Optional

from core.config.config_models import FinnhubDataConfig
from core.entities.market_data import PriceTick

logger = getLogger(__name__)

_WS_BASE             = "wss://ws.finnhub.io"
_BACKOFF_INIT        = 2
_BACKOFF_MAX         = 60
_STALE_WARN_SECONDS  = 180   # warn if no tick for 3 minutes
_STALE_CHECK_INTERVAL = 60   # check interval in seconds

# ── Module-level singleton ────────────────────────────────────────────────────

_instance:      Optional["FinnhubWsDataFetcher"] = None
_instance_task: Optional[asyncio.Task]           = None


def get_shared_fetcher(
    config: "FinnhubDataConfig | None" = None,
    # --- backward-compat kwargs (used when config is not provided) ---
    api_key:     str = "",
    max_symbols: int = 0,
) -> "FinnhubWsDataFetcher":
    """
    Return the process-wide FinnhubWsDataFetcher, creating and starting it on
    the first call.  Subsequent calls return the same instance.
    Must be called from an async context (uses asyncio.create_task).

    Preferred: pass a FinnhubDataConfig from config_loader.load_data_apis().finnhub.
    Legacy: pass api_key / max_symbols kwargs directly (still supported).
    """
    global _instance, _instance_task
    if _instance is None:
        if config is not None:
            api_key     = config.api_key
            max_symbols = config.max_symbols
        else:
            max_symbols = max_symbols if max_symbols > 0 else 50
        _instance      = FinnhubWsDataFetcher(api_key=api_key, max_symbols=max_symbols)
        _instance_task = asyncio.create_task(_instance.start())
        logger.info("Finnhub WS singleton started (max_symbols=%d)", max_symbols)
    return _instance


async def shutdown_shared_fetcher() -> None:
    """Close the singleton WS and cancel its task.  Safe to call if never started."""
    global _instance, _instance_task
    if _instance is not None:
        await _instance.close()
        _instance = None
    if _instance_task is not None:
        _instance_task.cancel()
        await asyncio.gather(_instance_task, return_exceptions=True)
        _instance_task = None
        logger.info("Finnhub WS singleton stopped")


class FinnhubWsDataFetcher:
    """
    Shared WebSocket client for the Finnhub trade stream.

    Multiple callers subscribe per-symbol callbacks; one WS connection serves them
    all.  Dynamic subscribe/unsubscribe is supported while the connection is live.

    Usage:
        fetcher = FinnhubWsDataFetcher(api_key, max_symbols=50)
        task    = asyncio.create_task(fetcher.start())

        await fetcher.subscribe("AAPL", my_tick_handler)
        await fetcher.subscribe("TSLA", other_handler)
        await fetcher.unsubscribe("AAPL", my_tick_handler)

        fetcher.stop()           # signal stop
        await fetcher.close()    # force-close open WS immediately
        task.cancel()
    """

    def __init__(self, api_key: str, max_symbols: int = 50) -> None:
        self.api_key     = api_key
        self.max_symbols = max_symbols
        self._running    = False
        self._ws         = None   # active websockets connection, or None
        # {symbol: [callback, ...]}
        self._callbacks: Dict[str, List[Callable]] = {}
        # last tick epoch-ms per symbol (for stale monitoring)
        self._last_tick:    Dict[str, float] = {}
        # symbols already warned about staleness (suppress repeat warnings)
        self._stale_warned: set[str] = set()
        # rolling message counter — reset each session; logged by stale monitor
        self._msg_count: int = 0

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def symbols(self) -> List[str]:
        return list(self._callbacks)

    async def subscribe(self, symbol: str, on_tick: Callable) -> None:
        """Register on_tick for symbol.  Sends subscribe message if WS is live.

        If the Finnhub symbol cap is already reached, logs a warning and returns
        without registering — the symbol will be served by FMP polling instead.
        """
        # Enforce cap: only count distinct symbols (not callbacks)
        if symbol not in self._callbacks and len(self._callbacks) >= self.max_symbols:
            logger.warning(
                "Finnhub WS: symbol cap %d reached — %s skipped (FMP polling fallback). "
                "Raise FINNHUB_MAX_SYMBOLS if your plan allows more.",
                self.max_symbols, symbol,
            )
            return

        cbs = self._callbacks.setdefault(symbol, [])
        if on_tick not in cbs:
            cbs.append(on_tick)
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"type": "subscribe", "symbol": symbol}))
                logger.debug("Finnhub WS: subscribed %s (live)", symbol)
            except Exception:
                pass  # will be re-subscribed on next reconnect

    async def unsubscribe(self, symbol: str, on_tick: Callable) -> None:
        """Remove on_tick for symbol.  Sends unsubscribe if no callbacks remain."""
        cbs = self._callbacks.get(symbol, [])
        try:
            cbs.remove(on_tick)
        except ValueError:
            pass
        if not cbs:
            self._callbacks.pop(symbol, None)
            self._last_tick.pop(symbol, None)
            self._stale_warned.discard(symbol)
            if self._ws is not None:
                try:
                    await self._ws.send(json.dumps({"type": "unsubscribe", "symbol": symbol}))
                    logger.debug("Finnhub WS: unsubscribed %s (live)", symbol)
                except Exception:
                    pass

    async def start(self) -> None:
        """Run the reconnect loop.  Returns when stop() is called or task cancelled."""
        self._running = True
        backoff = _BACKOFF_INIT
        while self._running:
            try:
                await self._connect()
                backoff = _BACKOFF_INIT   # reset after a successful session
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Finnhub WS dropped (%s) — reconnecting in %ds", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX)

    def stop(self) -> None:
        """Signal the reconnect loop to exit after the current session ends."""
        self._running = False

    async def close(self) -> None:
        """Immediately close the active WS (sends a close frame). stop() first."""
        self._running = False
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _connect(self) -> None:
        import websockets

        url = f"{_WS_BASE}?token={self.api_key}"
        async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
            self._ws       = ws
            self._msg_count = 0
            stale_task = asyncio.create_task(self._stale_monitor())
            try:
                for symbol in list(self._callbacks):
                    await ws.send(json.dumps({"type": "subscribe", "symbol": symbol}))
                logger.info(
                    "Finnhub WS: connected — subscribed to %d symbols: %s",
                    len(self._callbacks), list(self._callbacks),
                )
                async for raw in ws:
                    if not self._running:
                        break
                    await self._handle(raw)
            finally:
                stale_task.cancel()
                await asyncio.gather(stale_task, return_exceptions=True)
                self._ws = None

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
                self._stale_warned.clear()
                continue
            ticks, self._msg_count = self._msg_count, 0
            logger.info(
                "Finnhub WS: %d trade ticks received in last %ds across %d subscribed symbols",
                ticks, _STALE_CHECK_INTERVAL, len(self._callbacks),
            )

    async def _handle(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = msg.get("type")
        if msg_type != "trade":
            if msg_type == "error":
                logger.error("Finnhub WS error from server: %s", msg.get("msg", raw))
            elif msg_type == "ping":
                logger.debug("Finnhub WS: ping received")
            else:
                logger.info("Finnhub WS: non-trade message type=%r  payload=%s", msg_type, raw[:200])
            return

        for trade in msg.get("data", []):
            price  = float(trade.get("p", 0))
            symbol = trade.get("s", "")

            # Update staleness tracker
            self._last_tick[symbol] = float(trade.get("t") or time.time() * 1000)
            self._stale_warned.discard(symbol)
            self._msg_count += 1

            tick = PriceTick(
                symbol=symbol,
                price=price,
                bid=price,
                ask=price,
                volume=float(trade.get("v", 0)),
                timestamp=int(trade.get("t", 0)),
                source="finnhub",
            )
            for cb in list(self._callbacks.get(symbol, [])):
                try:
                    result = cb(tick)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    logger.exception("Finnhub WS: on_tick error for %s", symbol)
