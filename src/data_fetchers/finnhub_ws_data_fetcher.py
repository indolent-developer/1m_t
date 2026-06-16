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

Environment:
    FINNHUB_API_KEY
"""
from __future__ import annotations

import asyncio
import json
from core.utils.log_helper import getLogger
from typing import Callable, Dict, List, Optional

from core.entities.market_data import PriceTick

logger = getLogger(__name__)

_WS_BASE      = "wss://ws.finnhub.io"
_BACKOFF_INIT = 2
_BACKOFF_MAX  = 60

# ── Module-level singleton ────────────────────────────────────────────────────

_instance:      Optional["FinnhubWsDataFetcher"] = None
_instance_task: Optional[asyncio.Task]           = None


def get_shared_fetcher(api_key: str) -> "FinnhubWsDataFetcher":
    """
    Return the process-wide FinnhubWsDataFetcher, creating and starting it on
    the first call.  Subsequent calls with any api_key return the same instance.
    Must be called from an async context (uses asyncio.create_task).
    """
    global _instance, _instance_task
    if _instance is None:
        _instance      = FinnhubWsDataFetcher(api_key=api_key)
        _instance_task = asyncio.create_task(_instance.start())
        logger.info("Finnhub WS singleton started")
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
        fetcher = FinnhubWsDataFetcher(api_key)
        task    = asyncio.create_task(fetcher.start())

        await fetcher.subscribe("AAPL", my_tick_handler)
        await fetcher.subscribe("TSLA", other_handler)
        await fetcher.unsubscribe("AAPL", my_tick_handler)

        fetcher.stop()           # signal stop
        await fetcher.close()    # force-close open WS immediately
        task.cancel()
    """

    def __init__(self, api_key: str) -> None:
        self.api_key   = api_key
        self._running  = False
        self._ws       = None   # active websockets connection, or None
        # {symbol: [callback, ...]}
        self._callbacks: Dict[str, List[Callable]] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def symbols(self) -> List[str]:
        return list(self._callbacks)

    async def subscribe(self, symbol: str, on_tick: Callable) -> None:
        """Register on_tick for symbol.  Sends subscribe message if WS is live."""
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
        # async-with sends a close frame on exit (normal, exception, or CancelledError)
        async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
            self._ws = ws
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
                self._ws = None

    async def _handle(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        if msg.get("type") != "trade":
            return

        for trade in msg.get("data", []):
            price  = float(trade.get("p", 0))
            symbol = trade.get("s", "")
            tick   = PriceTick(
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
