"""
services.indicator_provider

Fetches 5-minute bars via PriceHistoryService (FMP + Redis cache) and computes
any pandas_ta indicator on demand. Used by LevelTracker for ATR band width.

Usage:
    provider = IndicatorProvider(history_svc, ["AAPL", "TSLA"])
    await provider.load()

    provider.compute("AAPL", "atr", length=14)   → 0.83
    provider.compute("AAPL", "rsi", length=14)   → 62.4
    provider.compute("AAPL", "ema", length=20)   → 298.7
"""
from __future__ import annotations

import asyncio
import datetime as dt
from typing import Dict, List, Optional

import pandas as pd
import pandas_ta as ta

from core.entities.ohlc import OHLCData
from core.entities.time_frame import TimeFrame
from core.utils.log_helper import getLogger
from services.price_history_service import PriceHistoryService

logger = getLogger(__name__)

_LOOKBACK_DAYS = 1


class IndicatorProvider:

    def __init__(
        self,
        history: PriceHistoryService,
        symbols: List[str],
        timeframe: TimeFrame = TimeFrame.MINUTE_5,
        default_period: int = 14,
        refresh_interval_seconds: int = 300,
    ) -> None:
        self._history  = history
        self._symbols  = symbols
        self._tf       = timeframe
        self._period   = default_period
        self._refresh  = refresh_interval_seconds
        self._data: Dict[str, pd.DataFrame] = {}
        self._loading: set[str] = set()

    # ── Public ────────────────────────────────────────────────────────────────

    async def load(self) -> None:
        await asyncio.gather(*[self.load_one(s) for s in list(self._symbols)])

    async def load_one(self, symbol: str) -> None:
        if symbol in self._loading:
            return
        self._loading.add(symbol)
        try:
            await self._load_one(symbol)
        finally:
            self._loading.discard(symbol)

    def compute(self, symbol: str, indicator: str, **kwargs) -> Optional[float]:
        """Return the latest value of any pandas_ta indicator for `symbol`."""
        df = self._data.get(symbol)
        if df is None or df.empty:
            return None
        fn = getattr(ta, indicator, None)
        if fn is None:
            logger.warning("IndicatorProvider: unknown indicator '%s'", indicator)
            return None
        try:
            result = fn(df["high"], df["low"], df["close"], **kwargs)
        except TypeError:
            try:
                result = fn(df["close"], **kwargs)
            except Exception as e:
                logger.warning("IndicatorProvider: %s(%s) error: %s", indicator, symbol, e)
                return None
        if result is None:
            return None
        series = result.dropna() if hasattr(result, "dropna") else result
        if hasattr(series, "__len__") and len(series) == 0:
            return None
        return float(series.iloc[-1]) if hasattr(series, "iloc") else float(series)

    async def refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(self._refresh)
            await self.load()
            logger.info("IndicatorProvider: refreshed %d symbols", len(self._symbols))

    # ── Private ───────────────────────────────────────────────────────────────

    async def _load_one(self, symbol: str) -> None:
        today = dt.date.today()
        start = today - dt.timedelta(days=_LOOKBACK_DAYS)
        try:
            bars = await self._history.get_bars(symbol, self._tf, start, today)
            if bars:
                self._data[symbol] = _bars_to_df(bars)
                logger.info("IndicatorProvider: %s — %d %s bars loaded",
                            symbol, len(bars), self._tf.value)
            else:
                logger.warning("IndicatorProvider: no bars for %s %s", symbol, self._tf.value)
        except Exception as e:
            logger.warning("IndicatorProvider: fetch failed %s: %s", symbol, e)


def _bars_to_df(bars: List[OHLCData]) -> pd.DataFrame:
    return pd.DataFrame([
        {"open": b.open, "high": b.high, "low": b.low,
         "close": b.close, "volume": b.volume or 0.0}
        for b in bars
    ])
