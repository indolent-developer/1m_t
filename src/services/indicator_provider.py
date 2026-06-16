"""
services.indicator_provider

Downloads OHLC bars for a single timeframe and computes any pandas_ta
indicator on demand. Used by LevelTracker and anything else that needs
a technical value for a symbol.

Usage:
    provider = IndicatorProvider(fmp, ["AAPL", "TSLA"], TimeFrame.MINUTE_5)
    await provider.load()

    provider.compute("AAPL", "atr", length=14)   → 0.83
    provider.compute("AAPL", "rsi", length=14)   → 62.4
    provider.compute("AAPL", "ema", length=20)   → 298.7
"""
from __future__ import annotations

import asyncio
import datetime as dt
from core.utils.log_helper import getLogger
from typing import Dict, List, Optional

import pandas as pd
import pandas_ta as ta

from core.entities.ohlc import OHLCData
from core.entities.time_frame import TimeFrame
from data_fetchers.data_fetcher_base import DataFetcherBase

logger = getLogger(__name__)

_WARMUP_BARS = 10  # extra bars so ATR has enough history to stabilise

_TF_MINUTES: dict[TimeFrame, int] = {
    TimeFrame.MINUTE_1:  1,
    TimeFrame.MINUTE_5:  5,
    TimeFrame.MINUTE_15: 15,
    TimeFrame.MINUTE_30: 30,
    TimeFrame.HOUR_1:    60,
}


class IndicatorProvider:

    def __init__(
        self,
        fetcher: DataFetcherBase,
        symbols: List[str],
        timeframe: TimeFrame = TimeFrame.MINUTE_5,
        default_period: int = 14,
        refresh_interval_seconds: int = 3600,
    ) -> None:
        self._fetcher  = fetcher
        self._symbols  = symbols
        self._tf       = timeframe
        self._period   = default_period
        self._refresh  = refresh_interval_seconds
        self._data: Dict[str, pd.DataFrame] = {}

    # ── Public ────────────────────────────────────────────────────────────────

    async def load(self) -> None:
        await asyncio.to_thread(self._load_sync)

    def compute(self, symbol: str, indicator: str, **kwargs) -> Optional[float]:
        """
        Return the latest value of any pandas_ta indicator for `symbol`.
        Tries high/low/close signature first (for ATR, etc.), then close only.
        """
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

    def _load_sync(self) -> None:
        now = dt.datetime.now()
        # Always look back 5 calendar days — covers weekends and pre/post-market
        # gaps so we reliably capture enough bars regardless of time of day.
        start = now - dt.timedelta(days=5)

        for symbol in self._symbols:
            try:
                bars = self._fetcher.get_market_data(
                    symbol=symbol,
                    start=start,
                    end=now,
                    timeframe=self._tf,
                    use_cache=False,
                )
                if bars:
                    self._data[symbol] = _bars_to_df(bars)
                    logger.info("IndicatorProvider: %s — %d %s bars loaded",
                                symbol, len(bars), self._tf.value)
                else:
                    logger.warning("IndicatorProvider: no bars for %s %s",
                                   symbol, self._tf.value)
            except Exception as e:
                logger.warning("IndicatorProvider: fetch failed %s: %s", symbol, e)


def _bars_to_df(bars: List[OHLCData]) -> pd.DataFrame:
    return pd.DataFrame([
        {"open": b.open, "high": b.high, "low": b.low,
         "close": b.close, "volume": b.volume or 0.0}
        for b in bars
    ])
