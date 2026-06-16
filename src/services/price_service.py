"""
services.price_service

FMP live price service using correct /stable/ endpoints.

Routing logic (FMP guidance):
    Regular market hours (09:30–16:00 ET)
        → /stable/batch-quote                last traded price

    Post/pre-market (04:00–09:30, 16:00–20:00 ET)
        → /stable/batch-aftermarket-quote    bid/ask quotes (ATS)
        → /stable/batch-aftermarket-trade    last trade price + volume + ts
        → falls back to batch-quote if both return empty

    Outside all sessions (20:00–04:00 ET)
        → no live data; returns last known price from /stable/batch-quote

FMP note: the regular Quote API stops updating after market close, so
after-hours prices must come from the Aftermarket endpoints. Equities
show no price from 20:00 until the next pre-market open.

Usage:
    svc = FmpPriceService(api_key="...", symbols=["AAPL", "TSLA"])

    # one-shot
    quotes = await svc.get_quotes()            # → dict[symbol, PriceQuote]

    # continuous stream
    async def handler(tick: PriceTick) -> None:
        print(tick.symbol, tick.price)

    await svc.stream(handler, interval=20)    # runs until cancelled
"""
from __future__ import annotations

import asyncio
from core.utils.log_helper import getLogger
from typing import Callable, Dict, List

import requests

from core.entities.market_data import PriceQuote, PriceTick
from core.utils.market import is_extended_market_time

logger = getLogger(__name__)

_FMP_BASE = "https://financialmodelingprep.com"


class FmpPriceService:
    """
    FMP price service — correct /stable/ endpoint routing with fallback.

    During extended hours it tries aftermarket-quote (bid/ask) first,
    then aftermarket-trade (last trade), then falls back to batch-quote.
    During regular hours only batch-quote is used.
    """

    def __init__(self, api_key: str, symbols: List[str]) -> None:
        self._api_key = api_key
        self._symbols = symbols

    # ── Public ────────────────────────────────────────────────────────────────

    async def get_quotes(self) -> Dict[str, PriceQuote]:
        """Async one-shot fetch — selects the right endpoint for the current time."""
        return await asyncio.to_thread(self._fetch)

    async def stream(
        self,
        on_tick: Callable[[PriceTick], None],
        interval: int = 20,
    ) -> None:
        """
        Poll FMP continuously and call on_tick for each symbol with a non-zero
        price. Runs until the caller cancels the task.
        """
        while True:
            try:
                quotes = await self.get_quotes()
                if not quotes:
                    logger.debug(
                        "FmpPriceService: no quotes for %s "
                        "(market closed or symbols unsupported after 20:00 ET)",
                        self._symbols,
                    )
                for sym, q in quotes.items():
                    if not q.price:
                        continue
                    tick = _to_tick(sym, q)
                    result = on_tick(tick)
                    if asyncio.iscoroutine(result):
                        await result
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("FmpPriceService: poll error")
            await asyncio.sleep(interval)

    # ── Routing ───────────────────────────────────────────────────────────────

    def _fetch(self) -> Dict[str, PriceQuote]:
        params = {"symbols": ",".join(self._symbols), "apikey": self._api_key}

        if is_extended_market_time():
            # 1. Try aftermarket-quote (bid/ask from ATS dark-pool)
            data = self._get("/stable/batch-aftermarket-quote", params)
            if data:
                parsed = _parse_aftermarket_quote(data)
                if parsed:
                    logger.debug(
                        "FmpPriceService: batch-aftermarket-quote → %d symbols", len(parsed)
                    )
                    return parsed

            # 2. Try aftermarket-trade (actual last trade price)
            data = self._get("/stable/batch-aftermarket-trade", params)
            if data:
                parsed = _parse_aftermarket_trade(data)
                if parsed:
                    logger.debug(
                        "FmpPriceService: batch-aftermarket-trade → %d symbols", len(parsed)
                    )
                    return parsed

            logger.debug(
                "FmpPriceService: both aftermarket endpoints empty, "
                "falling back to batch-quote"
            )

        # Regular hours or fallback
        data = self._get("/stable/batch-quote", params)
        if data:
            parsed = _parse_batch_quote(data)
            logger.debug("FmpPriceService: batch-quote → %d symbols", len(parsed))
            return parsed

        return {}

    def _get(self, path: str, params: dict) -> list:
        url = f"{_FMP_BASE}{path}"
        try:
            r = requests.get(url, params=params, timeout=10)
            if r.status_code == 200:
                body = r.json()
                return body if isinstance(body, list) and body else []
            logger.warning(
                "FmpPriceService: %s → HTTP %d  %.200s",
                path, r.status_code, r.text,
            )
        except Exception as exc:
            logger.warning("FmpPriceService: request error %s: %s", path, exc)
        return []


# ── Response parsers ──────────────────────────────────────────────────────────

def _parse_aftermarket_quote(data: list) -> Dict[str, PriceQuote]:
    """
    /stable/batch-aftermarket-quote fields:
        symbol, ask, bid, asize, bsize, timestamp
    """
    quotes: Dict[str, PriceQuote] = {}
    for item in data:
        sym = item.get("symbol", "")
        if not sym:
            continue
        ask = float(item.get("ask") or 0)
        bid = float(item.get("bid") or ask)
        if not ask and not bid:
            continue
        quotes[sym] = PriceQuote(
            symbol=sym,
            bid_price=bid,
            ask_price=ask,
            bid_size=int(item.get("bsize") or item.get("bidSize") or 0),
            ask_size=int(item.get("asize") or item.get("askSize") or 0),
            timestamp=int(item.get("timestamp") or 0),
        )
    return quotes


def _parse_aftermarket_trade(data: list) -> Dict[str, PriceQuote]:
    """
    /stable/batch-aftermarket-trade fields:
        symbol, price, size, timestamp
    Uses last trade price as bid=ask proxy (no spread data in trade feed).
    """
    quotes: Dict[str, PriceQuote] = {}
    for item in data:
        sym = item.get("symbol", "")
        if not sym:
            continue
        price = float(item.get("price") or 0)
        if not price:
            continue
        quotes[sym] = PriceQuote(
            symbol=sym,
            bid_price=price,
            ask_price=price,
            volume=int(item.get("size") or 0),
            timestamp=int(item.get("timestamp") or 0),
        )
    return quotes


def _parse_batch_quote(data: list) -> Dict[str, PriceQuote]:
    """
    /stable/batch-quote fields:
        symbol, price, volume, dayHigh, dayLow, changePercentage, timestamp
    """
    quotes: Dict[str, PriceQuote] = {}
    for item in data:
        sym = item.get("symbol", "")
        if not sym:
            continue
        price = float(item.get("price") or 0)
        if not price:
            continue
        quotes[sym] = PriceQuote(
            symbol=sym,
            bid_price=price,
            ask_price=price,
            volume=int(item.get("volume") or 0),
            timestamp=int(item.get("timestamp") or 0),
            change_percentage=float(item.get("changePercentage") or 0),
            day_high=float(item.get("dayHigh") or 0),
            day_low=float(item.get("dayLow") or 0),
        )
    return quotes


def _to_tick(symbol: str, q: PriceQuote) -> PriceTick:
    return PriceTick(
        symbol=symbol,
        price=q.price,
        bid=q.bid_price,
        ask=q.ask_price,
        volume=float(q.volume),
        timestamp=q.timestamp,
        change_pct=q.change_percentage,
        day_high=q.day_high,
        day_low=q.day_low,
        source="fmp",
    )
